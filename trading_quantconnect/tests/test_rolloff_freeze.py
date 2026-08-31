"""--freeze 的闸门(全 tmp 沙箱 + 网络禁令)。

K 冻下来就是**永久常数**:错一次,之后每一天的面板净值都带着这个错,而且没有
任何东西会报错。所以这里钉的不是"能不能跑通",是每一道闸门单独拉下来都必须拦住:

  - Q 必须是"现金 + Σ 股数 × 官方收盘价",绝不能是 payload 自算的盘中净值
    (2026-08-27 实测两者差 27,979 = 48.5bp);
  - QC 自报净值这条独立路径对不上 → 不冻;
  - M4 那天没出 ok/baseline 裁决 → 不冻(没被复核过的 Q 不配当常数);
  - M4 判过的 Q 与此刻复算的 Q 不同 → 两趟之间账户变过,冻哪个都是错的。
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
    """收盘快照。Q = 1000 + 1000×10 + 400×1 = 11,400。

    payload 自算净值(equity = holdings_mv + cash = 11,000)故意与之差 400 ——
    那正是现场那份"逐票价停在收盘前 15 分钟"的形状。任何一处仍拿 equity 定 K,
    算出来的就是 100,400 而不是 100,000,会被下面第一个测试逮住。
    """
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
    """把 --freeze 的每一路外部输入都接到 tmp/桩上;生产 state/ 一个不碰。"""
    monkeypatch.setattr(official_close, "grouped_closes", lambda s: dict(_CLOSES))
    monkeypatch.setattr(rolloff, "cmd_check", lambda: True)      # L/S 已清空
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


def _m4(tmp_path, status="ok", Q=11_400.0, session=_D):
    p = tmp_path / "reconcile" / f"qc_reconcile_{session}.json"
    p.write_text(json.dumps({"session": session,
                             "equity_check": {"status": status,
                                              "qc_equity_Q": Q}}))
    return p


# ── K 的算法本身 ───────────────────────────────────────────────────────────

def test_freeze_prices_q_at_official_closes_not_payload_equity(env, capsys):
    """K = P − (现金 + Σ 股数×官方收盘价)。用 payload 自算净值会得 100,400。"""
    _m4(env)
    assert rolloff.cmd_measure(freeze=True) == 0
    doc = json.loads((env / "rolloff.json").read_text())
    assert doc["k_equity"] == 100_000.0
    assert doc["qc_equity"] == 11_400.0
    assert doc["qc_equity_payload_selfcalc"] == 11_000.0   # 留痕,不参与 K
    assert doc["official_closes"] == {"X": 10.0, "Y": 1.0}
    assert doc["qc_shares"] == {"X": 1000, "Y": 400}
    assert doc["cross_check_usd"] == 0.0


def test_official_q_refuses_when_a_close_is_missing(monkeypatch):
    """少一只票的收盘价 → 少算的市值会被 K 全额吸收且不报错,故必须抛。"""
    monkeypatch.setattr(official_close, "grouped_closes", lambda s: {"X": 10.0})
    with pytest.raises(SourceError, match="收盘价缺"):
        rolloff.official_q(_D, _qc())


def test_official_q_refuses_without_shares():
    with pytest.raises(SourceError, match="股数"):
        rolloff.official_q(_D, _qc(shares={}))


# ── 闸门:每一道单独拉下来都要拦住 ─────────────────────────────────────────

def test_freeze_refuses_when_cross_check_breaches(env, monkeypatch):
    """gross = 10,400,3bp = $3.12;差 $50 必须拦。"""
    _m4(env)
    monkeypatch.setattr(rolloff, "qc_snapshot",
                        lambda: _qc(equity_reported=11_350.0))
    with pytest.raises(SourceError, match="两条独立路径对不上"):
        rolloff.cmd_measure(freeze=True)
    assert not (env / "rolloff.json").exists()


def test_freeze_refuses_without_qc_self_report(env, monkeypatch):
    """没有第二条路径可校验时,K 就是个没人复核过的数 —— 不冻。"""
    _m4(env)
    monkeypatch.setattr(rolloff, "qc_snapshot",
                        lambda: _qc(equity_reported=None))
    with pytest.raises(SourceError, match="没有第二条路径"):
        rolloff.cmd_measure(freeze=True)


def test_freeze_refuses_when_m4_report_missing(env):
    with pytest.raises(SourceError, match="缺失"):
        rolloff.cmd_measure(freeze=True)
    assert not (env / "rolloff.json").exists()


@pytest.mark.parametrize("st", ["pending", "partial", "breach", "incomplete"])
def test_freeze_refuses_when_m4_equity_not_terminal_ok(env, st):
    """partial / breach 也不行:partial 有漏项、breach 本身就是红的。"""
    _m4(env, status=st)
    with pytest.raises(SourceError, match=f"是 {st}"):
        rolloff.cmd_measure(freeze=True)


def test_freeze_accepts_m4_baseline(env):
    """首日基准也算判过 —— 它同样走完了官方 EOD 一致性与交叉校验。"""
    _m4(env, status="baseline")
    assert rolloff.cmd_measure(freeze=True) == 0


def test_freeze_refuses_when_m4_q_differs_from_now(env):
    """M4 判的是收盘那一刻;此刻 Q 不同 = 两趟之间账户变过。"""
    _m4(env, Q=11_500.0)
    with pytest.raises(SourceError, match="对不上"):
        rolloff.cmd_measure(freeze=True)


def test_freeze_refuses_intraday(env, monkeypatch):
    _m4(env)
    monkeypatch.setattr(rolloff, "_et_now",
                        lambda: datetime(2026, 8, 27, 11, 0, tzinfo=_ET))
    with pytest.raises(SourceError, match="盘中/盘后波动时段"):
        rolloff.cmd_measure(freeze=True)


def test_freeze_refuses_when_already_frozen(env):
    _m4(env)
    (env / "rolloff.json").write_text(json.dumps({"k_equity": 1.0}))
    with pytest.raises(SourceError, match="只冻一次"):
        rolloff.cmd_measure(freeze=True)


def test_freeze_refuses_when_queues_not_empty(env, monkeypatch):
    _m4(env)
    monkeypatch.setattr(rolloff, "cmd_check", lambda: False)
    with pytest.raises(SourceError, match="未清空"):
        rolloff.cmd_measure(freeze=True)


def test_freeze_refuses_when_shares_not_converged(env, monkeypatch):
    _m4(env)
    monkeypatch.setattr(rolloff, "convergence",
                        lambda sh: ([("X", 1000, 999)], 29))
    with pytest.raises(SourceError, match="未收敛"):
        rolloff.cmd_measure(freeze=True)


# ── --measure 只读,永不落盘 ────────────────────────────────────────────────

def test_measure_never_writes(env, capsys):
    assert rolloff.cmd_measure(freeze=False) == 0
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


def _perf(tmp_path, monkeypatch, files):
    monkeypatch.setattr(rolloff, "DATA", tmp_path)
    for fn, rows in files.items():
        (tmp_path / fn).write_text(json.dumps(rows))


def test_official_eod_by_session_reads_that_row_not_the_last(tmp_path,
                                                             monkeypatch):
    _perf(tmp_path, monkeypatch,
          _series(["2026-08-26", "2026-08-27", "2026-08-28"]))
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
