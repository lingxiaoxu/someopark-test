"""--freeze 的闸门(全 tmp 沙箱 + 网络禁令)。

2026-08-31 起 --freeze 为**报告锚定制**(方案 A,QUANTCONNECT_MIRROR_PLAN 附录
A-4):K = 锚点场次 M4 报告里**判过的** D_usd。K 冻下来就是永久锚点:错一次,
之后每一天的 K_eff 都带着这个错。所以这里钉的是:

  - K 的值必须逐位取自报告的 D_usd(报告已被三平面全套闸门复核过);
  - 冻结路径**零 QC API、零 Polygon** —— "此刻"的任何东西都不参与;
  - 锚点报告缺失 / 三段任何一段不是 ok(equity 可 baseline)→ 不冻;
  - 已冻结 / L·S 队列未清空 → 不冻;
  - 默认锚点 = 最新一份三段全 ok 的报告,跳过坏报告要出声。

旧三闸("此刻实测 Q ±$1"/"17:00–04:00 窗口"/"此刻收敛")已随 k_effective 退役,
对应测试同日移除 —— 它们保护的东西由报告生成时的闸门(equity_plane/holdings_
plane)负责,测试在 test_qc_reconcile.py。
"""
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_DIR))

import qc_api                                          # noqa: E402
from inventory_source import SourceError               # noqa: E402
from ops import rolloff                                # noqa: E402
from reconcile import official_close                   # noqa: E402

_ET = ZoneInfo("America/New_York")
_D = "2026-08-27"
_CLOSES = {"X": 10.0, "Y": 1.0}
# 官方五策略 EOD,合计 P = 111,400 ⇒ 对上 Q = 11,400 时 K 恰为 100,000
_OFF = {"mrpt": 40_000.0, "mtfs": 30_000.0, "ssrs": 20_000.0,
        "aiss": 20_000.0, "bdc": 1_400.0}


def _qc(**over):
    """收盘快照(仅 --measure 用)。Q = 1000 + 1000×10 + 400×1 = 11,400。"""
    qc = {"shares": {"X": 1000, "Y": 400}, "cash": 1000.0,
          "holdings_mv": 10_000.0, "equity": 11_000.0, "gross": 10_400.0,
          "equity_reported": 11_400.0, "price_staleness_usd": 400.0,
          "prices": {"X": 10.0, "Y": 1.0}, "deploy_id": "L-cur"}
    qc.update(over)
    return qc


@pytest.fixture(autouse=True)
def _ban_network(monkeypatch):
    def _no_qc(*a, **k):
        raise AssertionError("TEST NETWORK BAN: QcClient touched in unit test")

    def _no_poly(session):
        raise AssertionError("TEST NETWORK BAN: Polygon touched in unit test")
    monkeypatch.setattr(qc_api, "QcClient", _no_qc)
    monkeypatch.setattr(official_close, "grouped_closes", _no_poly)
    yield


@pytest.fixture()
def env(monkeypatch, tmp_path):
    """把每一路外部输入都接到 tmp/桩上;生产 state/ 一个不碰。"""
    monkeypatch.setattr(official_close, "grouped_closes", lambda s: dict(_CLOSES))
    monkeypatch.setattr(rolloff, "cmd_check", lambda: True)      # --measure 用
    monkeypatch.setattr(rolloff, "alive_queues",
                        lambda: {"mrpt": [], "mtfs": []})        # --freeze 用
    monkeypatch.setattr(rolloff, "convergence", lambda sh: ([], 29))
    monkeypatch.setattr(rolloff, "official_eod", lambda: (_D, dict(_OFF)))
    monkeypatch.setattr(rolloff, "qc_snapshot", lambda: _qc())
    monkeypatch.setattr(rolloff, "_et_now",
                        lambda: datetime(2026, 8, 27, 18, 0, tzinfo=_ET))
    monkeypatch.setattr(rolloff, "ROLLOFF_PATH", tmp_path / "rolloff.json")
    # M4 报告路径是 _THIS_DIR/reconcile/qc_reconcile_<d>.json
    monkeypatch.setattr(rolloff, "_THIS_DIR", tmp_path)
    (tmp_path / "reconcile").mkdir()
    return tmp_path


def _m4(tmp_path, session=_D, holdings="ok", target="ok", equity="ok",
        D=100_000.0, P=111_400.0, Q=11_400.0):
    """一份三段俱全的 M4 报告(报告锚定制的唯一输入)。"""
    p = tmp_path / "reconcile" / f"qc_reconcile_{session}.json"
    p.write_text(json.dumps({
        "session": session,
        "holdings_check": {"status": holdings, "n_matched": 29, "n_tickers": 29},
        "target_check": {"status": target},
        "equity_check": {"status": equity, "session": session,
                         "D_usd": D, "official_total_P": P, "qc_equity_Q": Q,
                         "official_eod": dict(_OFF),
                         "q_basis": "cash + Σ 逐票股数 × 官方收盘价(Polygon 日 K)",
                         "cross_check_usd": 0.0, "cross_check_bp": 0.0}}))
    return p


# ── K 的值与出处 ───────────────────────────────────────────────────────────

def test_freeze_anchors_k_from_report_d_usd(env):
    """K 逐位 = 报告 D_usd;来源字段齐全,可追溯到那份报告。"""
    _m4(env, D=100_000.0)
    assert rolloff.cmd_freeze_anchored(_D) == 0
    doc = json.loads((env / "rolloff.json").read_text())
    assert doc["k_equity"] == 100_000.0
    assert doc["measured_on"] == _D
    assert doc["provenance"] == "m4_report_anchored"
    assert doc["anchor_report"] == f"qc_reconcile_{_D}.json"
    assert doc["qc_equity"] == 11_400.0
    assert doc["panel_official_total"] == 111_400.0


def test_freeze_touches_neither_qc_nor_polygon_nor_clock(env, monkeypatch):
    """锚定制的语义核心:值取自历史已判报告,"此刻"的任何东西都不参与。
    QC 快照 / 官方 EOD / 收敛检查 / 时钟全部炸掉,冻结必须照样成功。"""
    _m4(env)
    def _boom(*a, **k):
        raise AssertionError("anchored freeze must not touch live state")
    for fn in ("qc_snapshot", "official_eod", "convergence", "official_q",
               "_et_now", "cmd_check"):
        monkeypatch.setattr(rolloff, fn, _boom)
    assert rolloff.cmd_freeze_anchored(_D) == 0
    assert json.loads((env / "rolloff.json").read_text())["k_equity"] == 100_000.0


# ── 闸门:每一道单独拉下来都要拦住 ─────────────────────────────────────────

def test_freeze_refuses_when_report_missing(env):
    with pytest.raises(SourceError, match="缺失"):
        rolloff.cmd_freeze_anchored(_D)
    assert not (env / "rolloff.json").exists()


@pytest.mark.parametrize("st", ["pending", "partial", "breach", "incomplete"])
def test_freeze_refuses_when_equity_not_terminal_ok(env, st):
    _m4(env, equity=st)
    with pytest.raises(SourceError, match=st):
        rolloff.cmd_freeze_anchored(_D)
    assert not (env / "rolloff.json").exists()


def test_freeze_accepts_equity_baseline(env):
    """首日基准也算判过 —— 它同样走完了官方 EOD 一致性与交叉校验。"""
    _m4(env, equity="baseline")
    assert rolloff.cmd_freeze_anchored(_D) == 0


def test_freeze_refuses_when_holdings_not_ok(env):
    """那天持仓没逐票配平 → P−Q 里混着未镜像差,不配当锚点。"""
    _m4(env, holdings="breach")
    with pytest.raises(SourceError, match="holdings"):
        rolloff.cmd_freeze_anchored(_D)


def test_freeze_refuses_when_target_not_ok(env):
    _m4(env, target="breach")
    with pytest.raises(SourceError, match="target"):
        rolloff.cmd_freeze_anchored(_D)


def test_freeze_refuses_without_d_usd(env):
    """equity 段 ok 但 D_usd 缺失(损坏/旧版报告)→ 不猜。"""
    _m4(env, D=None)
    with pytest.raises(SourceError, match="D_usd"):
        rolloff.cmd_freeze_anchored(_D)


def test_freeze_refuses_when_already_frozen(env):
    _m4(env)
    (env / "rolloff.json").write_text(json.dumps({"k_equity": 1.0}))
    with pytest.raises(SourceError, match="只冻一次"):
        rolloff.cmd_freeze_anchored(_D)


def test_freeze_refuses_when_queues_not_empty(env, monkeypatch):
    _m4(env)
    monkeypatch.setattr(rolloff, "alive_queues",
                        lambda: {"mtfs": [{"queue": "L", "pair": "A/B"}]})
    with pytest.raises(SourceError, match="未清空"):
        rolloff.cmd_freeze_anchored(_D)


def test_freeze_default_session_picks_latest_all_ok(env, capsys):
    """默认锚点 = 最新一份三段全 ok;更新的坏报告要**出声**跳过,不许静默。"""
    _m4(env, session="2026-08-26", D=88_000.0)
    _m4(env, session="2026-08-27", equity="pending", D=99_000.0)   # 更新但没判完
    assert rolloff.cmd_freeze_anchored(None) == 0
    doc = json.loads((env / "rolloff.json").read_text())
    assert doc["measured_on"] == "2026-08-26"
    assert doc["k_equity"] == 88_000.0
    assert "跳过 qc_reconcile_2026-08-27.json" in capsys.readouterr().out


def test_freeze_default_refuses_when_no_clean_report(env):
    _m4(env, equity="pending")
    with pytest.raises(SourceError, match="没有任何一份"):
        rolloff.cmd_freeze_anchored(None)


# ── official_q(--measure 仍用)──────────────────────────────────────────────

def test_official_q_refuses_when_a_close_is_missing(monkeypatch):
    """少一只票的收盘价 → 少算的市值会被 K 全额吸收且不报错,故必须抛。"""
    monkeypatch.setattr(official_close, "grouped_closes", lambda s: {"X": 10.0})
    with pytest.raises(SourceError, match="收盘价缺"):
        rolloff.official_q(_D, _qc())


def test_official_q_refuses_without_shares():
    with pytest.raises(SourceError, match="股数"):
        rolloff.official_q(_D, _qc(shares={}))


# ── --measure 只读,永不落盘 ────────────────────────────────────────────────

def test_measure_never_writes(env, capsys):
    assert rolloff.cmd_measure() == 0
    assert not (env / "rolloff.json").exists()
    out = capsys.readouterr().out
    assert "11,400.00" in out and "100,000.00" in out


# ── 官方 EOD:按日期取行,不取末行 ─────────────────────────────────────────
# 取末行的话,session D 的 P 只在 "P(D) 落地 → P(D+1) 落地" 之间取得到(实测约
# D+1 10:15 到 D+1 21:30)。M4 的补算窗口被压到不足一天,连着两晚 pipeline 出
# 问题,那两天的 equity 段就永久补不回来。--measure/--freeze 仍走末行:它们问的
# 是"现在发布到哪天了"。

def _series(dates):
    """三份官方文件,逐日齐全。第 i 天的值是 base + i,好逐位验到底取了哪行。"""
    return {
        "strategy_performance.json": [
            {"date": d, "mrpt_equity": 1.0 + i, "mtfs_equity": 2.0 + i}
            for i, d in enumerate(dates)],
        "master_portfolio_performance.json": [
            {"date": d, "sr_equity": 3.0 + i, "aiss_equity": 4.0 + i, "aeus_equity": 5.0 + i}
            for i, d in enumerate(dates)],
        "private_credit_bdc_performance.json": [
            {"date": d, "bdc_equity": 5.0 + i} for i, d in enumerate(dates)],
    }


def _perf(tmp_path, monkeypatch, files, aeus_onboarded=False):
    monkeypatch.setattr(rolloff, "DATA", tmp_path)
    # EXPORTER_STATE 一律隔离到 tmp:aeus 是否入 P 由"exporter 挂没挂 scalar"决定
    # (P 必须镜像 QC 正在镜像的书),测试不许读生产 state。
    st = tmp_path / "exporter_state.json"
    scal = {"mrpt": 1.0}
    if aeus_onboarded:
        scal["aeus"] = 1.0
    st.write_text(json.dumps({"scalars": scal}))
    monkeypatch.setattr(rolloff, "EXPORTER_STATE", st)
    for fn, rows in files.items():
        (tmp_path / fn).write_text(json.dumps(rows))


def test_official_eod_by_session_reads_that_row_not_the_last(tmp_path,
                                                             monkeypatch):
    _perf(tmp_path, monkeypatch,
          _series(["2026-08-26", "2026-08-27", "2026-08-28"]),
          aeus_onboarded=True)
    d, off = rolloff.official_eod("2026-08-27")
    assert d == "2026-08-27"
    assert off == {"mrpt": 2.0, "mtfs": 3.0, "ssrs": 4.0, "aiss": 5.0,
                   "aeus": 6.0, "bdc": 6.0}
    # 不传 session 仍是"发布到哪天了" —— 冻 K 问的正是这个,不能被顺手改掉
    assert rolloff.official_eod()[0] == "2026-08-28"


def test_official_eod_by_session_refuses_when_one_file_lacks_the_row(
        tmp_path, monkeypatch):
    """一份文件还没写到那天 = 夜间 pipeline 没跑齐。少一策略的 P 会被 D 全额吸收。"""
    files = _series(["2026-08-26", "2026-08-27"])
    files["master_portfolio_performance.json"] = \
        files["master_portfolio_performance.json"][:1]      # 只到 8/26
    _perf(tmp_path, monkeypatch, files)
    with pytest.raises(SourceError, match="没有 2026-08-27 那一行"):
        rolloff.official_eod("2026-08-27")


def test_official_eod_refuses_duplicate_rows_for_a_session(tmp_path,
                                                           monkeypatch):
    """同一天两行 = merge 逻辑坏了。挑一行读下去就是在猜哪行是真的。"""
    files = _series(["2026-08-26", "2026-08-27"])
    files["strategy_performance.json"].append(
        {"date": "2026-08-27", "mrpt_equity": 99.0, "mtfs_equity": 99.0})
    _perf(tmp_path, monkeypatch, files)
    with pytest.raises(SourceError, match="有 2 行重复"):
        rolloff.official_eod("2026-08-27")


def test_official_eod_by_session_still_requires_the_field(tmp_path, monkeypatch):
    files = _series(["2026-08-27"])
    del files["private_credit_bdc_performance.json"][0]["bdc_equity"]
    _perf(tmp_path, monkeypatch, files)
    with pytest.raises(SourceError, match="缺字段 bdc_equity"):
        rolloff.official_eod("2026-08-27")


def test_official_rows_refuses_half_written_json(tmp_path):
    """夜间 pipeline 非原子覆写,对账正撞在写的当口会读到截断的 JSON。

    那是"这趟先别对",不是"程序崩了" —— 裸的 JSONDecodeError 会穿过
    equity_plane 的 SourceError 捕获,把已经算好的 ①② 一起打掉。
    """
    p = tmp_path / "strategy_performance.json"
    p.write_text('[{"date": "2026-08-27", "mrpt_eq')      # 写了一半
    with pytest.raises(SourceError, match="不是完整 JSON"):
        rolloff.official_rows(p)


def test_official_rows_refuses_empty_and_missing(tmp_path):
    (tmp_path / "empty.json").write_text("[]")
    with pytest.raises(SourceError, match="读不到"):
        rolloff.official_rows(tmp_path / "empty.json")
    with pytest.raises(SourceError, match="读不到"):
        rolloff.official_rows(tmp_path / "nope.json")


def test_official_eod_excludes_aeus_until_qc_onboarded(tmp_path, monkeypatch):
    """P 必须镜像 QC 正在镜像的书:aeus 官方序列先于挂载存在(实测 ~$1.16M
    全历史已入 master json),挂载前计入会让 ΔD 凭空跳一整个策略的净值。
    判据 = exporter scalars 有无 aeus;挂载(onboard_aeus append scalar)后自动纳入。"""
    _perf(tmp_path, monkeypatch,
          _series(["2026-08-27"]), aeus_onboarded=False)
    d, off = rolloff.official_eod("2026-08-27")
    assert "aeus" not in off
    assert off == {"mrpt": 1.0, "mtfs": 2.0, "ssrs": 3.0, "aiss": 4.0,
                   "bdc": 5.0}
