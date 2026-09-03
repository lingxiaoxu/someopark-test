"""M4 对账平面单测(全 tmp 沙箱 + 网络禁令 + 生产文件只读)。

钉死的是**判定语义**,不是能不能跑通:
  - 两趟合并绝不把前一趟的真裁决抹成 pending;
  - 已推未执行(applied < pushed)不出 0 差裁决,也不误判 breach;
  - ΔD 分解的每一项符号(哪一项推高 D、哪一项压低)与阈值选择;
  - 取不到读数的项目进 blocked_terms 且不给绿灯 —— 漏项绝不能被小残差掩盖。
"""
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_DIR))

import qc_api                                        # noqa: E402
from ops import rolloff                              # noqa: E402
from reconcile import official_close                 # noqa: E402
from reconcile import qc_reconcile as qr             # noqa: E402

_ET = ZoneInfo("America/New_York")


class _NetBan:
    def __init__(self, *a, **k):
        raise AssertionError("TEST NETWORK BAN: QcClient touched in unit test")


def _polygon_ban(session):
    raise AssertionError("TEST NETWORK BAN: Polygon grouped bars in unit test")


@pytest.fixture(autouse=True)
def _ban_network(monkeypatch):
    monkeypatch.setattr(qc_api, "QcClient", _NetBan)
    # Q 现在要官方收盘价,取数走 Polygon —— 同样禁网。需要价的单测由 eq_env
    # 覆盖成固定表;没覆盖却走到这里的,是漏掉了桩,必须炸而不是偷偷联网。
    monkeypatch.setattr(official_close, "grouped_closes", _polygon_ban)
    yield


# ── merge_section:两趟合并 ─────────────────────────────────────────────────

def test_merge_keeps_earlier_terminal_when_later_is_pending():
    prior = {"status": "ok", "n_tickers": 29}
    fresh = {"status": "pending_apply", "note": "已推未执行"}
    out = qr.merge_section(prior, fresh)
    assert out["status"] == "ok" and out["n_tickers"] == 29
    assert out["later_pass"]["status"] == "pending_apply"


def test_merge_later_terminal_replaces_and_records_earlier():
    out = qr.merge_section({"status": "partial", "note": "x"},
                           {"status": "ok"})
    assert out["status"] == "ok"
    assert out["earlier_pass"]["status"] == "partial"


def test_merge_no_prior_and_both_pending():
    assert qr.merge_section(None, {"status": "pending"})["status"] == "pending"
    out = qr.merge_section({"status": "pending", "note": "a"},
                           {"status": "incomplete", "note": "b"})
    assert out["status"] == "incomplete" and "earlier_pass" not in out


# ── last_session:NYSE 日历,不做"周末→周五"近似 ──────────────────────────

@pytest.mark.parametrize("when,expect", [
    # 2026-01-01 元旦休市 → 上一个交易日 2025-12-31
    (datetime(2026, 1, 1, 20, 0, tzinfo=_ET), "2025-12-31"),
    # 周六 → 周五
    (datetime(2026, 8, 29, 10, 0, tzinfo=_ET), "2026-08-28"),
    # 交易日盘中 10:00,今天还没收盘 → 前一个交易日
    (datetime(2026, 8, 27, 10, 0, tzinfo=_ET), "2026-08-26"),
    # 交易日收盘后 → 当天
    (datetime(2026, 8, 27, 16, 30, tzinfo=_ET), "2026-08-27"),
])
def test_last_session(when, expect):
    assert qr.last_session(when) == expect


def test_last_session_half_day_before_close_is_previous():
    """2025-11-28 感恩节次日 13:00 收市:12:30 时"最近已收盘"仍是 11-26。"""
    assert qr.last_session(datetime(2025, 11, 28, 12, 30, tzinfo=_ET)) \
        == "2025-11-26"
    assert qr.last_session(datetime(2025, 11, 28, 13, 30, tzinfo=_ET)) \
        == "2025-11-28"


# ── fills_of_session:事件展开 / ET 归日 / tag 解析 ─────────────────────────

def _order(oid, tkr, qty, px, epoch, tag, last_px=None, events=None):
    return {"id": oid, "symbol": {"value": tkr}, "tag": tag,
            "orderSubmissionData": ({"lastPrice": last_px, "bidPrice": None,
                                     "askPrice": None} if last_px else {}),
            "events": events or [
                {"status": "submitted", "fillQuantity": 0.0, "time": epoch},
                {"status": "filled", "fillQuantity": qty, "fillPrice": px,
                 "time": epoch, "orderFeeAmount": 0.0}]}


def test_fills_parse_tag_and_filter_by_et_date():
    # 2026-08-27 13:31 UTC = 09:31 ET;2026-08-28 00:30 UTC = 08-27 20:30 ET
    same_day = datetime(2026, 8, 27, 13, 31, tzinfo=ZoneInfo("UTC")).timestamp()
    next_utc = datetime(2026, 8, 28, 0, 30, tzinfo=ZoneInfo("UTC")).timestamp()
    other = datetime(2026, 8, 26, 13, 31, tzinfo=ZoneInfo("UTC")).timestamp()
    orders = [
        _order(1, "SRE", -1156.0, 83.95, same_day,
               '[MIRROR] v11 adjust {"mtfs": -1156.0}', last_px=84.285),
        _order(2, "MLM", 54.0, 535.5, next_utc, "[MIRROR] v5 open {}"),
        _order(3, "OLD", 10.0, 1.0, other, "[MIRROR] v4 open {}"),
    ]
    f = qr.fills_of_session(orders, "2026-08-27", _ET)
    assert [r["ticker"] for r in f] == ["SRE", "MLM"]
    assert f[0]["target_version"] == 11
    assert f[0]["attribution"] == {"mtfs": -1156.0}
    assert f[0]["submit_last_px"] == 84.285
    assert f[1]["submit_last_px"] is None      # 缺 orderSubmissionData → None


def test_fills_expand_partial_fills_as_separate_records():
    ep = datetime(2026, 8, 27, 14, 0, tzinfo=ZoneInfo("UTC")).timestamp()
    o = _order(9, "AAA", 0, 0, ep, "[MIRROR] v2 open {}", events=[
        {"status": "partiallyFilled", "fillQuantity": 40.0, "fillPrice": 10.0,
         "time": ep},
        {"status": "filled", "fillQuantity": 60.0, "fillPrice": 10.2,
         "time": ep}])
    f = qr.fills_of_session([o], "2026-08-27", _ET)
    assert len(f) == 2 and sum(r["qty"] for r in f) == 100.0


# ── _queue_pnl:L 全额未镜像 / S 只差 (1−k) / 缺读数进 unresolved ──────────

def _snap_pair(pair, log):
    return {"mtfs": {"pairs": {pair: {"monitor_log": log}}}}


def test_queue_pnl_legacy_full_scaled_partial():
    built = {"legacy_alive": {"mtfs": [{"pair": "A/B", "open_date": "2026-08-05"}]},
             "scaled_alive": {"mtfs": [{"pair": "C/D", "open_date": "2026-08-18"}]}}
    snap = {"mtfs": {"pairs": {
        "A/B": {"monitor_log": [{"date": "2026-08-26", "unrealized_pnl": 100.0},
                                {"date": "2026-08-27", "unrealized_pnl": 150.0}]},
        "C/D": {"monitor_log": [{"date": "2026-08-26", "unrealized_pnl": 0.0},
                                {"date": "2026-08-27", "unrealized_pnl": 200.0}]},
    }}}
    q = qr._queue_pnl("2026-08-27", "2026-08-26", built, snap, {"mtfs": 0.25})
    assert q["legacy_usd"] == 50.0                 # m=0 → 整份未镜像
    assert q["scaled_usd"] == 150.0                # 200 × (1 − 0.25)
    assert q["total_usd"] == 200.0
    assert q["unresolved"] == []


def test_queue_pnl_missing_reading_goes_unresolved_not_zero():
    built = {"legacy_alive": {"mtfs": [{"pair": "A/B", "open_date": "2026-08-05"}]},
             "scaled_alive": {}}
    snap = _snap_pair("A/B", [{"date": "2026-08-27", "unrealized_pnl": 150.0}])
    q = qr._queue_pnl("2026-08-27", "2026-08-26", built, snap, {})
    assert q["total_usd"] == 0.0
    assert len(q["unresolved"]) == 1
    assert q["unresolved"][0]["have_prev"] is False
    assert q["unresolved"][0]["have_session"] is True


# ── holdings_plane ─────────────────────────────────────────────────────────

@pytest.fixture()
def target_file(monkeypatch, tmp_path):
    p = tmp_path / "target_portfolio.json"

    def _write(version, targets):
        p.write_text(json.dumps({"schema": 1, "version": version,
                                 "content_hash": "deadbeef",
                                 "exported_at": "2026-08-27T14:14:23+00:00",
                                 "targets": targets}))
        return p
    monkeypatch.setattr(qr, "TARGET_COPY", p)
    monkeypatch.setattr(rolloff, "TARGET_COPY", p)
    return _write


def test_holdings_ok_on_exact_match(target_file):
    target_file(13, {"AAPL": 100, "MSFT": -50})
    row = qr.holdings_plane({"shares": {"AAPL": 100, "MSFT": -50}}, 13)
    assert row["status"] == "ok" and row["diffs"] == []
    assert row["n_matched"] == row["n_tickers"] == 2


def test_holdings_breach_on_one_share(target_file):
    target_file(13, {"AAPL": 100})
    row = qr.holdings_plane({"shares": {"AAPL": 99}}, 13)
    assert row["status"] == "breach"
    assert row["diffs"] == [{"ticker": "AAPL", "qc": 99, "target": 100,
                             "diff": -1}]


def test_holdings_pending_when_pushed_ahead_of_applied(target_file):
    """夜间换书后的常态:新 target 已推、QC 要等明早开盘 —— 不出裁决而非 breach。"""
    target_file(14, {"AAPL": 100})
    row = qr.holdings_plane({"shares": {"AAPL": 40}}, 13)
    assert row["status"] == "pending_apply"
    assert "diffs" not in row


def test_holdings_still_judges_when_applied_unknown(target_file):
    """日志里读不到 applied 版本 → 不能因此放行,照常出 0 差裁决。"""
    target_file(13, {"AAPL": 100})
    assert qr.holdings_plane({"shares": {"AAPL": 100}}, None)["status"] == "ok"


# ── equity_plane:ΔD 分解的符号与阈值 ───────────────────────────────────────

@pytest.fixture()
def eq_env(monkeypatch, tmp_path):
    """把 equity_plane 的所有外部输入接到 tmp/桩上(生产文件一个不碰)。"""
    res = tmp_path / "fractional_residual.json"
    res.write_text(json.dumps({"residual": {"X": 0.5}}))
    monkeypatch.setattr(qr, "RESIDUAL_PATH", res)
    monkeypatch.setattr(qr, "ROLLOFF_PATH", tmp_path / "no_rolloff.json")
    # 官方 EOD 桩成"只有 8/27 那一行"。**按日期查**:问别的日子就抛,和真的
    # official_eod(session) 一个脾气 —— 桩成"问什么都给 8/27"会把 equity_plane
    # 是否真把 session 传下去这件事一起桩没了。
    def _official_eod(session=None):
        if session not in (None, "2026-08-27"):
            raise rolloff.SourceError(
                f"strategy_performance.json 里没有 {session} 那一行"
                f"(末行 2026-08-27)")
        return ("2026-08-27", {"mrpt": 1_200_000.0, "mtfs": 1_200_000.0,
                               "ssrs": 1_200_000.0, "aiss": 1_200_000.0,
                               "bdc": 1_200_000.0})
    monkeypatch.setattr(rolloff, "official_eod", _official_eod)
    monkeypatch.setattr(qr, "_ledger_accounts", lambda s: (
        {st: {"as_of": s, "equity": 1_000_000.0, "cumulative_dividends": 0.0}
         for st in ("mrpt", "mtfs", "ssrs", "aiss", "bdc")}, []))
    # 官方 EOD 文件目录也要接到 tmp:盘中定稿闸门会去 stat 这些文件的 mtime,
    # 不接的话单测会读生产目录 —— 既违反"生产文件只读"的底线,又会让判定
    # 随生产文件什么时候被写而漂移(2026-08-27 就是这么把 7 个测试拦成 pending 的)。
    _perf_files(tmp_path, monkeypatch)
    # 官方收盘价:只桩掉**取数**那一层,closes_for 的缺价判定照常真跑 ——
    # "缺一只就不出裁决"是这层最要紧的一条,桩掉 closes_for 就把它一起桩没了。
    monkeypatch.setattr(official_close, "grouped_closes", lambda s: dict(_CLOSES))
    return tmp_path


def _perf_files(tmp_path, monkeypatch):
    """三份 official EOD 文件,默认 mtime = 收盘后 5 小时(即"正常夜间写出")。"""
    import os
    monkeypatch.setattr(rolloff, "DATA", tmp_path)
    close = datetime(2026, 8, 27, 16, 0, tzinfo=_ET).timestamp()

    def _write(fn, hours_from_close=5.0):
        p = tmp_path / fn
        p.write_text("[]")
        t = close + hours_from_close * 3600
        os.utime(p, (t, t))
        return p
    for fn in ("strategy_performance.json", "master_portfolio_performance.json",
               "private_credit_bdc_performance.json"):
        _write(fn)
    return _write


def _prev(D, cash, frac=3.0):
    return {"equity_check": {"session": "2026-08-26", "D_usd": D,
                             "qc_cash": cash,
                             "fractional_residual": {"value_usd": frac},
                             "cumulative_dividends": {
                                 st: 0.0 for st in ("mrpt", "mtfs", "ssrs",
                                                    "aiss", "bdc")}}}


# 该 session 的官方收盘价表(eq_env 里替掉 Polygon 取数)。X 同时是小数残差
# 那只票 —— 残差必须按**这张表**定值,不是按 QC payload 里的价。
_CLOSES = {"X": 10.0, "Y": 1.0}


def _qc(cash, **over):
    """一份收盘快照。Q = cash + Σ 股数×官方收盘价 恒等于 5,900,000。

    Y 的股数随现金反向配平,让所有既有断言(D_usd=100,000 等)在 Q 换口径后
    仍然逐位不变 —— 换的是 Q 怎么来的,不是 Q 是多少。
    prices 是 QC payload 里那份**盘中陈价**,故意与收盘价不同:任何一处仍拿它
    定值(例如小数残差)都会立刻算出别的数,被单测抓住。
    """
    qc = {"equity": 5_900_000.0, "gross": 1_000_000.0, "cash": cash,
          "holdings_mv": 5_900_000.0 - cash, "equity_reported": 5_900_000.0,
          "shares": {"X": 1000, "Y": int(5_890_000 - cash)},
          "prices": {"X": 7.0, "Y": 0.5}, "deploy_id": "L-cur"}
    qc.update(over)
    return qc


def test_equity_decomposition_signs_and_rebalance_threshold(monkeypatch, eq_env):
    monkeypatch.setattr(qr, "prev_report", lambda s: _prev(99_000.0, 1000.0))
    built = {"legacy_alive": {"mtfs": [{"pair": "A/B",
                                        "open_date": "2026-08-05"}]},
             "scaled_alive": {}}
    snap = _snap_pair("A/B", [{"date": "2026-08-26", "unrealized_pnl": 0.0},
                              {"date": "2026-08-27", "unrealized_pnl": 600.0}])
    ep = datetime(2026, 8, 27, 14, 0, tzinfo=ZoneInfo("UTC")).timestamp()
    fills = qr.fills_of_session(
        [_order(1, "X", 100.0, 10.05, ep, "[MIRROR] v9 open {}", last_px=10.0)],
        "2026-08-27", _ET)
    # cash: 1000 − 100×10.05(成交)+ 393(非成交现金=QC 股息) = 388
    row = qr.equity_plane("2026-08-27", _qc(388.0), fills, built, snap, {})
    assert row["D_usd"] == 100_000.0 and row["delta_D_usd"] == 1_000.0
    a = row["attribution"]
    assert a["bootstrap_unmirrored_pnl"]["total_usd"] == 600.0   # 推高 D
    assert a["slippage"]["usd"] == 5.0                           # 买贵 → 推高 D
    assert a["fractional_residual_delta"]["usd"] == 2.0          # 5.0 − 3.0
    assert a["dividend_timing"]["qc_nonfill_cash_usd"] == 393.0
    assert a["dividend_timing"]["usd"] == -393.0                 # 本地 0 − QC 393
    assert row["attributed_usd"] == 214.0
    assert row["unattributed_usd"] == 786.0
    assert row["tolerance_bp"] == qr.REBAL_TOL_BP                # 有成交日
    assert row["judged_bp_gross"] == 7.86
    assert row["status"] == "breach"


def test_equity_ok_when_fully_attributed_and_quiet(monkeypatch, eq_env):
    """无成交日、分解把 ΔD 吃干净 → ok。"""
    monkeypatch.setattr(qr, "prev_report",
                        lambda s: _prev(100_000.0 - 602.0, 388.0, frac=5.0))
    built = {"legacy_alive": {"mtfs": [{"pair": "A/B",
                                        "open_date": "2026-08-05"}]},
             "scaled_alive": {}}
    snap = _snap_pair("A/B", [{"date": "2026-08-26", "unrealized_pnl": 0.0},
                              {"date": "2026-08-27", "unrealized_pnl": 602.0}])
    row = qr.equity_plane("2026-08-27", _qc(388.0), [], built, snap, {})
    assert row["n_fills"] == 0 and row["tolerance_bp"] == qr.STEADY_TOL_BP
    assert row["unattributed_usd"] == 0.0
    assert row["status"] == "ok"


def test_equity_partial_when_a_term_cannot_be_computed(monkeypatch, eq_env):
    """L 队盈亏取不到读数:阈值内也不给 ok —— 漏项可能正好抵消真误差。"""
    monkeypatch.setattr(qr, "prev_report",
                        lambda s: _prev(100_000.0, 388.0, frac=5.0))
    built = {"legacy_alive": {"mtfs": [{"pair": "A/B",
                                        "open_date": "2026-08-05"}]},
             "scaled_alive": {}}
    snap = _snap_pair("A/B", [{"date": "2026-08-27", "unrealized_pnl": 600.0}])
    row = qr.equity_plane("2026-08-27", _qc(388.0), [], built, snap, {})
    assert row["unattributed_usd"] == 0.0
    assert row["status"] == "partial"
    assert any("L/S" in b for b in row["blocked_terms"])


def test_equity_pending_when_official_eod_lacks_that_session(eq_env):
    """官方 EOD 里没有 session 那一行(夜间 pipeline 未跑完)→ 不出裁决。

    eq_env 的桩只有 8/27,所以问 8/28 必然落空 —— 这同时钉住了
    equity_plane 确实把 session **传了下去**:传空的话会拿到 8/27 的 P
    去对 8/28 收盘的 Q,量出来的是一整天行情,而且一个字都不会报错。
    """
    row = qr.equity_plane("2026-08-28", _qc(388.0), [], {}, {}, {})
    assert row["status"] == "pending"
    assert "没有 2026-08-28 那一行" in row["note"]
    assert "D_usd" not in row


def test_equity_pending_when_two_paths_disagree(monkeypatch, eq_env):
    """收盘价复算的 Q 与 QC 自报净值对不上 → 不挑一个信,直接不出裁决。

    gross = 1,000,000,CROSS_TOL_BP = 5 → 容差 $500。差 $600 必须拦下。
    """
    monkeypatch.setattr(qr, "prev_report", lambda s: _prev(100_000.0, 388.0))
    row = qr.equity_plane("2026-08-27",
                          _qc(388.0, equity_reported=5_900_600.0),
                          [], {}, {}, {})
    assert row["status"] == "pending"
    assert row["cross_check_usd"] == -600.0
    assert row["cross_check_bp"] == 6.0
    assert "两条独立" in row["note"] and "D_usd" not in row


def test_equity_cross_check_passes_just_inside_tolerance(monkeypatch, eq_env):
    """容差内照常出裁决 —— 闸门不能宽到形同虚设,也不能严到永远拦着。

    紧贴新边界(4.99bp < 5.0):阈值若被误调回 3bp,这个用例立刻变红,
    不会出现"改了常数但没人发现测试其实没在守边界"的情况。
    """
    monkeypatch.setattr(qr, "prev_report", lambda s: None)
    row = qr.equity_plane("2026-08-27",
                          _qc(388.0, equity_reported=5_900_499.0),
                          [], {}, {}, {})
    assert row["status"] == "baseline"
    assert row["cross_check_bp"] == 4.99 and row["D_usd"] == 100_000.0


def test_equity_pending_when_snapshot_has_no_shares(monkeypatch, eq_env):
    """旧版收盘存档只有总数没有股数 —— 拿盘中自算净值当 Q 会错 48.5bp。"""
    monkeypatch.setattr(qr, "prev_report", lambda s: _prev(100_000.0, 388.0))
    qc = _qc(388.0)
    del qc["shares"]
    row = qr.equity_plane("2026-08-27", qc, [], {}, {}, {})
    assert row["status"] == "pending" and "股数" in row["note"]
    assert "D_usd" not in row


def test_equity_pending_when_a_close_is_missing(monkeypatch, eq_env):
    """少一只票的收盘价 → 那只票的市值会被 D 全额吸收且不报错,故拒绝出裁决。"""
    monkeypatch.setattr(official_close, "grouped_closes",
                        lambda s: {"X": 10.0})          # Y 没有收盘价
    monkeypatch.setattr(qr, "prev_report", lambda s: _prev(100_000.0, 388.0))
    row = qr.equity_plane("2026-08-27", _qc(388.0), [], {}, {}, {})
    assert row["status"] == "pending" and "收盘价" in row["note"]
    assert "D_usd" not in row


def test_fractional_residual_uses_official_close_not_payload_price(
        monkeypatch, eq_env):
    """残差必须与 Q 同一套价。X 收盘 10.0、payload 陈价 7.0:

    0.5 股 × 10.0 = 5.0(对);按 payload 价则是 3.5 —— 那 1.5 会一分不差地
    漏进 unattributed,而且没有任何东西会报错。
    """
    monkeypatch.setattr(qr, "prev_report", lambda s: None)
    row = qr.equity_plane("2026-08-27", _qc(388.0), [], {}, {}, {})
    assert row["fractional_residual"]["value_usd"] == 5.0
    assert row["fractional_residual"]["unpriced"] == []


def test_equity_baseline_on_first_ever_run(monkeypatch, eq_env):
    monkeypatch.setattr(qr, "prev_report", lambda s: None)
    row = qr.equity_plane("2026-08-27", _qc(388.0), [], {}, {}, {})
    assert row["status"] == "baseline" and row["D_usd"] == 100_000.0
    assert "delta_D_usd" not in row


# ── prev_report:只认已出过基准的报告 ───────────────────────────────────────

def test_prev_report_skips_reports_without_baseline(monkeypatch, tmp_path):
    monkeypatch.setattr(qr, "REPORT_DIR", tmp_path)
    (tmp_path / "qc_reconcile_2026-08-24.json").write_text(json.dumps(
        {"session": "2026-08-24", "equity_check": {"session": "2026-08-24",
                                                   "D_usd": 42.0}}))
    (tmp_path / "qc_reconcile_2026-08-26.json").write_text(json.dumps(
        {"session": "2026-08-26", "equity_check": {"status": "pending"}}))
    (tmp_path / "qc_reconcile_2026-08-27.json").write_text(json.dumps(
        {"session": "2026-08-27", "equity_check": {"D_usd": 1.0}}))
    got = qr.prev_report("2026-08-27")
    assert got["session"] == "2026-08-24"      # 跳过 pending 的 8/26,不取当日


# ── qc_applied_version:250 行窗口翻页 ──────────────────────────────────────

class _FakeLogs:
    """照抄真 API 的两条硬约束:窗口 >250 行直接报错;start/end 是绝对行号。

    2026-08-27 实测,tail=300 就是这么炸的 —— 单测里不复刻这个上限,
    改回超宽窗口也不会有人发现。
    """

    def __init__(self, lines):
        self.lines = lines
        self.calls = []

    def live_logs(self, pid, start, end):
        if end - start > qr.LOG_WINDOW:
            raise qc_api.QcApiError(
                "live/logs/read: ['Lines requested are greater than "
                "the limit of 250']")
        self.calls.append((start, end))
        return {"length": len(self.lines), "logs": self.lines[start:end]}


def _log_lines(n, applied_at=None):
    out = [f"2026-08-27 10:0{i % 10}:00 [MIRROR] poll noop {i}" for i in range(n)]
    for idx, ver in (applied_at or {}).items():
        out[idx] = f"2026-08-27 10:21:00 [MIRROR] applied v{ver} CONVERGED (29 target tickers)"
    return out


def test_applied_version_reads_last_window_only():
    c = _FakeLogs(_log_lines(381, {150: 11, 380: 13}))
    assert qr.qc_applied_version(c, 1) == 13
    # 一次探长度 + 一次尾窗;命中即停,不该把整段日志翻完
    assert c.calls == [(0, 1), (131, 381)]


def test_applied_version_pages_back_when_tail_is_noisy():
    c = _FakeLogs(_log_lines(700, {10: 4}))
    assert qr.qc_applied_version(c, 1) == 4
    assert c.calls[-1] == (0, 200)          # 一路回扫到行首才命中


def test_applied_version_none_when_never_converged():
    c = _FakeLogs(_log_lines(120))
    assert qr.qc_applied_version(c, 1) is None


def test_applied_version_raises_rather_than_faking_when_scan_exhausted(monkeypatch):
    monkeypatch.setattr(qr, "MAX_LOG_WINDOWS", 2)
    c = _FakeLogs(_log_lines(5000, {0: 1}))
    with pytest.raises(qr.SourceError, match="判不出 QC 应用到哪一版"):
        qr.qc_applied_version(c, 1)


# ── 审阅期补的四个缺口 ────────────────────────────────────────────────────

def test_fees_do_not_masquerade_as_dividends(monkeypatch, eq_env):
    """手续费必须从"非成交现金"里扣掉,否则整笔冒充股息落进时点项。

    实测目前费率为 0,这条测的是费率模型一旦变化不会静默串项。
    """
    monkeypatch.setattr(qr, "prev_report", lambda s: _prev(99_000.0, 1000.0))
    ep = datetime(2026, 8, 27, 14, 0, tzinfo=ZoneInfo("UTC")).timestamp()
    o = _order(1, "X", 100.0, 10.05, ep, "[MIRROR] v9 open {}", last_px=10.0)
    o["events"][1]["orderFeeAmount"] = 7.0          # 收了 7 块手续费
    fills = qr.fills_of_session([o], "2026-08-27", _ET)
    # cash = 1000 − 1005(成交) − 7(费) + 393(真股息) = 381
    row = qr.equity_plane("2026-08-27", _qc(381.0), fills,
                          {"legacy_alive": {}, "scaled_alive": {}}, {}, {})
    dt = row["attribution"]["dividend_timing"]
    assert dt["fees_usd"] == 7.0
    assert dt["qc_nonfill_cash_usd"] == 393.0       # 扣了费才还原出真股息


def test_zero_gross_cannot_produce_ok(monkeypatch, eq_env):
    """分母退化不许换来绿灯 —— 账户空了是最该报警的一天。"""
    monkeypatch.setattr(qr, "prev_report", lambda s: _prev(99_000.0, 1000.0))
    qc = dict(_qc(1000.0), gross=0.0)
    row = qr.equity_plane("2026-08-27", qc, [],
                          {"legacy_alive": {}, "scaled_alive": {}}, {}, {})
    assert row["judged_bp_gross"] == 0.0
    assert row["status"] != "ok"
    assert any("gross exposure = 0" in b for b in row["blocked_terms"])


def test_applied_newer_than_local_target_is_breach(target_file):
    """QC 应用的版本比本地文件新 = 本地不知道 QC 在镜像什么,不能当 0 差过。

    持股与本地 target 恰好逐票相符也不行 —— 相符的是一本**过期**的书。
    """
    target_file(13, {"AAPL": 100})
    row = qr.holdings_plane({"shares": {"AAPL": 100}}, applied_version=99)
    assert row["status"] == "breach"
    assert "被回滚或覆盖" in row["note"]


def test_orders_never_silently_truncate(monkeypatch):
    """超过 hard_cap 要抛,不能截断 —— 漏掉当日成交会用错阈值。"""
    class _C:
        def call(self, path, payload):
            return {"orders": [{"id": i} for i in range(100)], "length": 10_000}
    with pytest.raises(qr.SourceError, match="hard_cap"):
        qr.qc_orders(_C(), 1, "L-cur", page=100, hard_cap=300)


# ── 订单流按部署隔离(2026-08-28 实测:端点返回项目下所有历史部署的单)────────

def _ord(oid, algo, tkr="AAPL", q=10.0, px=5.0):
    return {"id": oid, "symbol": {"value": tkr}, "tag": "",
            "events": [{"id": f"{algo}-{oid}-1", "algorithmId": algo,
                        "status": "submitted", "fillQuantity": 0.0},
                       {"id": f"{algo}-{oid}-2", "algorithmId": algo,
                        "status": "filled", "fillQuantity": q,
                        "fillPrice": px, "time": 1786973820.0}]}


def test_orders_drop_other_deployments():
    """order id 在每个部署里都从 1 重编 —— 只能按 events 的 algorithmId 归属。

    生产现场(2026-08-28):111 条记录只有 67 个不同 id,id 1..N 各出现三次。
    单看 id 去重会保留错的那条;不过滤则每票股数三倍。
    """
    raw = [_ord(1, "L-dead1"), _ord(1, "L-dead2"), _ord(1, "L-cur"),
           _ord(2, "L-cur"), _ord(3, "L-dead1")]
    keep = qr._of_deploy(raw, "L-cur")
    assert [o["id"] for o in keep] == [1, 2]
    assert all(e["algorithmId"] == "L-cur"
               for o in keep for e in o["events"])


def test_orders_of_dead_deployment_cannot_inflate_todays_fills():
    """死部署当天的单不得进当日成交 —— 换手额翻倍会同时选错 3bp/5bp 阈值。"""
    raw = [_ord(1, "L-cur", q=10.0), _ord(1, "L-dead", q=10.0)]
    fills = qr.fills_of_session(qr._of_deploy(raw, "L-cur"),
                                "2026-08-17", _ET)
    assert len(fills) == 1
    assert sum(f["qty"] for f in fills) == 10.0
    # 不过滤会是两笔 —— 把这条反过来钉住,防止哪天有人把过滤挪走
    assert len(qr.fills_of_session(raw, "2026-08-17", _ET)) == 2


def test_orders_without_events_are_dropped_not_kept():
    """无 event 的记录判不出归属:对 fills 无贡献,留下只会污染归属判断。"""
    assert qr._of_deploy([{"id": 9, "events": []}, {"id": 8}], "L-cur") == []


def test_qc_orders_refuses_without_deploy_id():
    """缺 deploy_id 宁可不出裁决,也不能混着算。"""
    with pytest.raises(qr.SourceError, match="deploy_id"):
        qr.qc_orders(object(), 1, "")


# ── 盘中定稿的官方 EOD(日期对、值是陈的)──────────────────────────────────

@pytest.fixture()
def perf_dir(monkeypatch, tmp_path):
    """official EOD 文件目录接到 tmp,mtime 可控(与 eq_env 共用同一 tmp_path)。"""
    return _perf_files(tmp_path, monkeypatch)


def test_intraday_guard_clean_when_all_written_after_close(perf_dir):
    assert qr.intraday_official_files("2026-08-27") == []


def test_intraday_guard_catches_file_finalized_before_close(perf_dir):
    """BDC 2026-08-27 实况:末行是当天,但文件 10:17 就定稿了。"""
    perf_dir("private_credit_bdc_performance.json", -5.7)   # 约 10:18
    got = qr.intraday_official_files("2026-08-27")
    assert len(got) == 1 and "private_credit_bdc" in got[0]
    assert "早于" in got[0]


def test_intraday_guard_skips_when_session_is_no_longer_the_last_row(perf_dir):
    """文件里 session 之后还有行 ⇒ 已被之后的收盘后跑批全段重写过。

    整份文件的 mtime 说的是**末行**那天的成色;拿它判一行更早的,判的是别人家
    的时刻,只会把补得回来的天白白拦掉 —— 而"补得回来"正是按日期取 P 的全部
    意义。这里刻意把 mtime 留在 8/27 盘前:忘了这道跳过,8/27 就会被拦下。
    """
    import os
    p = perf_dir("private_credit_bdc_performance.json", -5.7)   # 约 10:18 定稿
    mt = p.stat().st_mtime
    p.write_text(json.dumps([{"date": "2026-08-27", "bdc_equity": 1.0},
                             {"date": "2026-08-28", "bdc_equity": 2.0}]))
    os.utime(p, (mt, mt))                       # 改内容不许动 mtime
    assert qr.intraday_official_files("2026-08-27") == []
    # 但对**末行**那天,mtime 判据照常有效 —— 别把闸门整个拆了
    assert any("private_credit_bdc" in s
               for s in qr.intraday_official_files("2026-08-28"))


def test_equity_plane_refuses_when_official_is_intraday(monkeypatch, eq_env,
                                                        perf_dir):
    """日期全对也不许出裁决 —— 否则错的 P 会被焊成基准 D。"""
    perf_dir("private_credit_bdc_performance.json", -5.7)
    monkeypatch.setattr(qr, "prev_report", lambda s: _prev(99_000.0, 1000.0))
    row = qr.equity_plane("2026-08-27", _qc(1000.0), [],
                          {"legacy_alive": {}, "scaled_alive": {}}, {}, {})
    assert row["status"] == "pending"
    assert "D_usd" not in row                      # 绝不能留下可被当基准的 D
    assert any("private_credit_bdc" in s for s in row["intraday_official"])


# ── 值级佐证:mtime 陈 ≠ 值陈 ──────────────────────────────────────────────
# 光看 mtime 会误杀:2026-08-27 的 BDC perf 文件 10:17 定稿,但 16:07 收盘后写出的
# daily_report 里 bdc_equity 与 perf 末行分毫不差 —— 那行本来就是收盘值,16:07 那趟
# 只是"值没变、不重写"。所以补一层值级佐证。
#
# 这一组测试的要害不是"BDC 能放行",而是**另外三条拒绝分支**:佐证文件不存在 /
# 佐证文件自己也是盘前写的 / 值对不上。它们只要有一条写反(比如 mt < close 写成
# mt > close),这层佐证就退化成无条件放行 —— 盘中陈值闸门被悄悄拆掉,且不报错。

@pytest.fixture()
def corrob(perf_dir, monkeypatch, tmp_path):
    """复刻 BDC 2026-08-27 的现场,四个变量可控。

    perf 文件固定为**盘前定稿**(10:18),放不放行完全取决于佐证。
    """
    import os
    monkeypatch.setattr(qr, "REPO", tmp_path)
    close = datetime(2026, 8, 27, 16, 0, tzinfo=_ET).timestamp()

    def setup(perf_value=943_460.12, corrob_value=943_460.12,
              corrob_hours=0.12, write_corrob=True, perf_date="2026-08-27"):
        p = perf_dir("private_credit_bdc_performance.json", -5.7)   # 10:18
        p.write_text(json.dumps([{"date": perf_date,
                                  "bdc_equity": perf_value}]))
        t = close - 5.7 * 3600         # write_text 会刷新 mtime,写完再压回去
        os.utime(p, (t, t))
        if write_corrob:
            d = tmp_path / "portfolio_of_private_credit_deals" / "bdc_results"
            d.mkdir(parents=True, exist_ok=True)
            c = d / "daily_report_2026-08-27.json"
            c.write_text(json.dumps(
                {"date": "2026-08-27",
                 "stock_layer": {"bdc_equity": corrob_value}}))
            tc = close + corrob_hours * 3600
            os.utime(c, (tc, tc))
    return setup


def test_corroborated_value_match_clears_stale_mtime(corrob):
    """收盘后独立产物的值与 perf 末行一致 → 那行是真收盘值,放行。"""
    corrob()
    assert qr.intraday_official_files("2026-08-27") == []


def test_corroborator_value_mismatch_still_blocks(corrob):
    """差 $50 就不算佐证 —— perf 里那行确实是陈的。"""
    corrob(corrob_value=943_410.12)
    got = qr.intraday_official_files("2026-08-27")
    assert len(got) == 1 and "不符" in got[0]


def test_corroborator_written_before_close_still_blocks(corrob):
    """佐证文件自己也是盘前写的 → 两份都是盘中值,互证等于没证。"""
    corrob(corrob_hours=-0.5)                       # 15:30 写出
    got = qr.intraday_official_files("2026-08-27")
    assert len(got) == 1 and "也是收盘前" in got[0]


def test_missing_corroborator_still_blocks(corrob):
    """当天那份独立产物压根没写出来 → 无从佐证,照拦。"""
    corrob(write_corrob=False)
    got = qr.intraday_official_files("2026-08-27")
    assert len(got) == 1 and "佐证文件不存在" in got[0]


def test_corroborator_wont_vouch_for_yesterdays_row(corrob):
    """perf 末行还停在昨天 → 就算今天的佐证值恰好相同也不许放行。

    值相等在净值上完全可能(周末、或一天下来净变动为零),不能凭"值一样"
    就认下一行不存在的今日行。
    """
    corrob(perf_date="2026-08-26")
    got = qr.intraday_official_files("2026-08-27")
    assert len(got) == 1 and "2026-08-26" in got[0]


def test_unregistered_file_has_no_corroboration_path(corrob):
    """登记表里没有的文件(master/strategy)永远走不到放行分支。"""
    corrob()                                        # BDC 已可佐证
    perf = qr.rolloff.DATA / "master_portfolio_performance.json"
    import os
    t = datetime(2026, 8, 27, 10, 16, tzinfo=_ET).timestamp()
    os.utime(perf, (t, t))
    got = qr.intraday_official_files("2026-08-27")
    assert len(got) == 1 and "master_portfolio" in got[0]
    assert "无收盘后独立产物可佐证" in got[0]


def test_equity_plane_proceeds_once_bdc_corroborated(monkeypatch, eq_env,
                                                     corrob):
    """整条链:mtime 陈但值被证实 → 闸门放行,equity 段照常出裁决。"""
    corrob()
    monkeypatch.setattr(qr, "prev_report", lambda s: _prev(99_000.0, 1000.0))
    row = qr.equity_plane("2026-08-27", _qc(1000.0), [],
                          {"legacy_alive": {}, "scaled_alive": {}}, {}, {})
    assert "intraday_official" not in row
    assert "D_usd" in row


# ── 收盘窗口:Q 只在 [session 收盘, 次交易日开盘) 内等于该 session 的收盘净值 ─
# P 与 Q 的可得窗口不重叠(P 要等次日 ~10:15 的 pipeline),所以"同时读两边"
# 在结构上做不到。窗口判据是把这件事挡在门外的唯一一道闸门:一旦放过去,
# 算出来的 D 外表完全正常,没有任何东西会报错。

def _at(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=_ET)


@pytest.mark.parametrize("when", [(2026, 8, 27, 16, 20),      # 收盘后 20 分钟
                                  (2026, 8, 27, 23, 15),      # 夜里那趟
                                  (2026, 8, 28, 9, 29)])      # 次日开盘前一分钟
def test_close_window_accepts_the_three_scheduled_times(when):
    ok, why = qr.in_close_window("2026-08-27", _at(*when))
    assert ok, why


def test_close_window_rejects_before_the_close():
    ok, why = qr.in_close_window("2026-08-27", _at(2026, 8, 27, 15, 59))
    assert not ok and "还没到" in why


def test_close_window_rejects_after_next_open():
    """8/28 中午手工补跑 8/27 —— 读到的是 8/28 的盘中净值,必须拒绝。

    这正是 pipeline 延迟那天我差点建议用户去做的事。
    """
    ok, why = qr.in_close_window("2026-08-27", _at(2026, 8, 28, 12, 0))
    assert not ok and "已越过下一交易日开盘" in why


def test_close_window_spans_the_weekend():
    """周五 session:周六整天仍在窗口内(下一开盘是周一 9:30),周一开盘后失效。

    周五 20:30 的 pipeline 周六上午才收工 —— 这一条决定了周五那场能不能补。
    """
    assert qr.in_close_window("2026-08-28", _at(2026, 8, 29, 11, 0))[0]
    assert not qr.in_close_window("2026-08-28", _at(2026, 8, 31, 9, 31))[0]


# ── settle:次日用收盘存档补算,全程不碰 QC ────────────────────────────────

def _archived(rd, session="2026-08-27", equity_status="pending", **over):
    """一份 D 日报告:holdings/target 已终态,equity 待补,QC 侧已存档。"""
    rep = {"session": session,
           "holdings_check": {"status": "ok", "n_tickers": 29, "n_matched": 29},
           "target_check": {"status": "ok"},
           "equity_check": {"status": equity_status, "note": "等官方 EOD"},
           "bootstrap": {"legacy_remaining": 0, "scaled_remaining": 0},
           "verdict": "partial",
           "close_snapshot": {
               "taken_at": "2026-08-27T23:15:02-04:00",
               # 存档必须带逐票股数:次日 settle 时 QC 那边已经是新一天的账户,
               # 拿不回 D 日收盘的股数,没股数就用不了官方收盘价定 Q。
               # 存档里的残差股数(2.0)与 state/ 里现存的(0.5)故意不同,
               # 用来验证 settle 用的是存档、不是次日已被覆盖的那份。
               "qc": _qc(1000.0),
               "fills": [], "scalars": {},
               "residual": {"residual": {"X": 2.0}}}}
    rep.update(over)
    (rd / f"qc_reconcile_{session}.json").write_text(json.dumps(rep))
    return rep


@pytest.fixture()
def settle_env(monkeypatch, tmp_path, eq_env):
    """REPORT_DIR 与持仓读取全接到 tmp/桩;时钟固定在 8/28 11:00(已开盘)。"""
    import exporter as exp
    import inventory_source as isrc
    rd = tmp_path / "reports"
    rd.mkdir()
    monkeypatch.setattr(qr, "REPORT_DIR", rd)
    monkeypatch.setattr(rolloff, "_et_now", lambda: _at(2026, 8, 28, 11, 0))
    monkeypatch.setattr(isrc, "read_snapshot", lambda: {})
    monkeypatch.setattr(exp, "compose",
                        lambda s: {"built": {"legacy_alive": {},
                                             "scaled_alive": {}}})
    return rd


def test_settle_uses_archived_q_and_never_creates_a_qc_client(settle_env):
    """全程零 QC 调用 —— 自动生效的 _ban_network 会在任何一次触碰时炸掉。"""
    _archived(settle_env)
    rep = qr.settle(dry=True)
    eq = rep["equity_check"]
    assert eq["status"] == "baseline"
    assert eq["qc_equity_Q"] == 5_900_000.0          # 存档的 Q,不是此刻的
    assert eq["q_source"]["taken_at"].startswith("2026-08-27T23:15")


def test_settle_prefers_archived_residual_over_current_state(settle_env):
    """次日 state/fractional_residual.json 可能已被新一轮推送覆盖。

    存档是 2.0 股 × $10 = $20;eq_env 在 state/ 里放的是 0.5 股 = $5。
    取到 $5 就说明读了次日那份 —— 那是另一个 target 的残差。
    """
    _archived(settle_env)
    rep = qr.settle(dry=True)
    assert rep["equity_check"]["fractional_residual"]["value_usd"] == 20.0


def test_settle_refuses_when_no_report_exists(settle_env):
    with pytest.raises(qr.SourceError, match="没有 2026-08-27 的报告"):
        qr.settle(dry=True)


def test_settle_refuses_when_snapshot_missing(settle_env):
    """收盘那趟没跑成 → Q 已永久错过,只能拒绝,绝不拿次日的数凑。"""
    _archived(settle_env, close_snapshot=None)
    with pytest.raises(qr.SourceError, match="close_snapshot"):
        qr.settle(dry=True)


def test_settle_is_idempotent_once_equity_is_terminal(settle_env):
    _archived(settle_env, equity_status="ok")
    rep = qr.settle(dry=True)
    assert rep["equity_check"]["status"] == "ok"
    assert "q_source" not in rep["equity_check"]      # 根本没重算
    assert "settled_at" not in rep


# ── ΔD 的跨度必须是"一天" ──────────────────────────────────────────────────

def test_equity_pending_when_prev_report_is_not_the_adjacent_session(
        monkeypatch, eq_env):
    """前一份出过 D 的报告不是紧邻交易日 → 不出裁决。

    prev_report 会跳过没出 D 的报告,所以中间漏一天就变成跨两天的 ΔD,而滑点
    只累当天 fills、股息由 Δcash 反解 —— 漏掉那天成交的整笔名义现金流会冒充
    成股息。这是**错项**不是漏项,给 partial 都算抬举。
    """
    p = _prev(100_000.0, 388.0)
    p["equity_check"]["session"] = "2026-08-25"       # 中间的 8/26 没出过 D
    monkeypatch.setattr(qr, "prev_report", lambda s: p)
    row = qr.equity_plane("2026-08-27", _qc(388.0), [], {}, {}, {})
    assert row["status"] == "pending"
    assert row["expected_prev_session"] == "2026-08-26"
    assert "delta_D_usd" not in row and "attribution" not in row
    # D 是可信锚点(P/Q 两侧闸门都过了),必须留在报告里 —— 否则 prev_report
    # 明天也会跳过这一份,一次断链会一路断下去。
    assert row["D_usd"] == 100_000.0


def test_equity_judges_normally_when_prev_is_adjacent(monkeypatch, eq_env):
    """紧邻就照常出裁决 —— 闸门不能顺手把正常的日子也拦了。"""
    monkeypatch.setattr(qr, "prev_report", lambda s: _prev(100_000.0, 388.0))
    row = qr.equity_plane("2026-08-27", _qc(388.0), [],
                          {"legacy_alive": {}, "scaled_alive": {}}, {}, {})
    assert row["status"] in ("ok", "partial")
    assert row["delta_D_usd"] == 0.0


# ── 派活按"还欠着什么",不按"今天是哪天" ──────────────────────────────────

def test_unsettled_sessions_only_lists_recoverable_days_oldest_first(
        monkeypatch, tmp_path):
    rd = tmp_path / "reports"
    rd.mkdir()
    monkeypatch.setattr(qr, "REPORT_DIR", rd)

    def _w(session, status, snap=True):
        doc = {"session": session, "equity_check": {"status": status}}
        if snap:
            doc["close_snapshot"] = {"taken_at": f"{session}T23:15:00-04:00"}
        (rd / f"qc_reconcile_{session}.json").write_text(json.dumps(doc))
    _w("2026-08-26", "pending")
    _w("2026-08-25", "ok")                       # 已终态 → 不欠
    _w("2026-08-27", "pending")
    _w("2026-08-24", "pending", snap=False)      # 没存档 → 补不回来,不列
    _w("2026-07-01", "pending")                  # 超出 14 天回看窗
    got = qr.unsettled_sessions(_at(2026, 8, 28, 11, 0))
    assert got == ["2026-08-26", "2026-08-27"]


def test_settle_all_settles_every_owed_day_in_order(monkeypatch, settle_env):
    """欠了两天就补两天。只补 last_session 的话 8/26 会永久停在 pending。"""
    _archived(settle_env, session="2026-08-26")
    _archived(settle_env, session="2026-08-27")
    seen = []

    def _fake(session, dry=False):
        seen.append(session)
        return {"verdict": "ok"}
    monkeypatch.setattr(qr, "settle", _fake)
    assert qr.settle_all(dry=True) == 0
    assert seen == ["2026-08-26", "2026-08-27"]


def test_settle_all_still_runs_current_session_when_it_has_no_report(
        monkeypatch, settle_env):
    """当天报告缺失进不了欠账清单 —— 那种情况必须炸出来,不能变成"今天没活"。"""
    monkeypatch.setattr(qr, "settle_all", qr.settle_all)   # 用真的
    rc = qr.settle_all(dry=True)
    assert rc == 1                                # settle() 抛"没有报告可补算"


def test_settle_all_backlog_only_skips_current_session(monkeypatch, settle_env):
    """live 那趟的收尾:当天现场刚由 reconcile() 取过,别拿存档覆盖。"""
    _archived(settle_env, session="2026-08-26")
    _archived(settle_env, session="2026-08-27")
    seen = []
    monkeypatch.setattr(qr, "settle",
                        lambda s, dry=False: (seen.append(s), {"verdict": "ok"})[1])
    assert qr.settle_all(dry=True, include_current=False) == 0
    assert seen == ["2026-08-26"]


def test_settle_all_reports_breach_over_failure(monkeypatch, settle_env):
    _archived(settle_env, session="2026-08-26")
    _archived(settle_env, session="2026-08-27")

    def _fake(session, dry=False):
        if session == "2026-08-26":
            raise qr.SourceError("补不回来了")
        return {"verdict": "breach"}
    monkeypatch.setattr(qr, "settle", _fake)
    assert qr.settle_all(dry=True) == 2


# ── 换仓镜像滞后归因 + K_eff(2026-08-30 补:8/28 实测 −12.9k 无处可挂)────
#
# 面板按决策日收盘入账、QC 次日 target 应用后才成交。[决策收盘 → 下单瞬间]
# 只有面板在场,是 ΔD 的永久台阶;[下单 → 成交] 归 slippage。两段合计恰为
# qty×(fill − 决策收盘),不重不漏。

def _closes_by_session(monkeypatch, tables: dict):
    """按 session 分表的收盘价桩(eq_env 默认桩是"问哪天都一样",测不出
    新项拿没拿**对的那天**的价 —— 8/28 的教训恰恰是两天价差)。"""
    monkeypatch.setattr(official_close, "grouped_closes",
                        lambda s: dict(tables[s]))


def test_mirror_lag_attributed_makes_rebalance_gap_ok(monkeypatch, eq_env):
    """8/28 场景的最小复刻:隔夜跳空全额落进镜像滞后项,残差归零 → ok。"""
    _closes_by_session(monkeypatch, {
        "2026-08-27": {"X": 10.0, "Y": 1.0},     # 本日:定 Q 用
        "2026-08-26": {"X": 9.0, "Y": 1.0},      # 决策日:X 收 9,隔夜涨到 10
    })
    # 滞后 = 100×(10.0−9.0) = +100;滑点 = 100×(10.05−10.0) = +5;
    # 小数残差 delta = 0(frac=5 = 0.5×10);股息 0(现金恒等式配平)。
    monkeypatch.setattr(qr, "prev_report",
                        lambda s: _prev(100_000.0 - 105.0, 2000.0, frac=5.0))
    ep = datetime(2026, 8, 27, 14, 0, tzinfo=ZoneInfo("UTC")).timestamp()
    fills = qr.fills_of_session(
        [_order(1, "X", 100.0, 10.05, ep, "[MIRROR] v9 open {}", last_px=10.0)],
        "2026-08-27", _ET)
    # cash: 2000 − 1005(成交) = 995
    row = qr.equity_plane("2026-08-27", _qc(995.0), fills,
                          {"legacy_alive": {}, "scaled_alive": {}}, {}, {})
    ml = row["attribution"]["rebalance_mirror_lag"]
    assert ml["usd"] == 100.0 and ml["n_legs"] == 1
    assert ml["per_leg"][0]["decision_close"] == 9.0
    assert ml["per_leg"][0]["end_px"] == 10.0            # 到下单瞬间,不到成交
    assert ml["per_leg"][0]["full_window"] is False
    assert row["attribution"]["slippage"]["usd"] == 5.0  # 尾段仍归滑点,没双计
    assert row["attributed_usd"] == 105.0
    assert row["unattributed_usd"] == 0.0
    assert row["status"] == "ok"


def test_mirror_lag_sell_leg_sign(monkeypatch, eq_env):
    """卖腿:QC 迟迟没卖掉,期间涨价是 QC 多吃的 ⇒ 压低 D,符号必须为负。"""
    _closes_by_session(monkeypatch, {
        "2026-08-27": {"X": 10.0, "Y": 1.0},
        "2026-08-26": {"X": 9.0, "Y": 1.0},
    })
    # 滞后 = −100×(10.0−9.0) = −100;滑点 = (10.05−10.0)×(−100) = −5
    monkeypatch.setattr(qr, "prev_report",
                        lambda s: _prev(100_000.0 + 105.0, 2000.0, frac=5.0))
    ep = datetime(2026, 8, 27, 14, 0, tzinfo=ZoneInfo("UTC")).timestamp()
    fills = qr.fills_of_session(
        [_order(1, "X", -100.0, 10.05, ep, "[MIRROR] v9 close {}",
                last_px=10.0)],
        "2026-08-27", _ET)
    # cash: 2000 + 1005(卖出回款) = 3005
    row = qr.equity_plane("2026-08-27", _qc(3005.0), fills,
                          {"legacy_alive": {}, "scaled_alive": {}}, {}, {})
    assert row["attribution"]["rebalance_mirror_lag"]["usd"] == -100.0
    assert row["attribution"]["slippage"]["usd"] == -5.0
    assert row["unattributed_usd"] == 0.0
    assert row["status"] == "ok"


def test_mirror_lag_full_window_fallback_when_no_submit_ref(monkeypatch,
                                                            eq_env):
    """缺下单参考价的腿整窗归滞后项;slippage 算不了它,但**不再拦裁决**——
    残差里不缺东西,拦是"检查做过但白做"。"""
    _closes_by_session(monkeypatch, {
        "2026-08-27": {"X": 10.0, "Y": 1.0},
        "2026-08-26": {"X": 9.0, "Y": 1.0},
    })
    # 整窗 = 100×(10.05−9.0) = +105;滑点 0
    monkeypatch.setattr(qr, "prev_report",
                        lambda s: _prev(100_000.0 - 105.0, 2000.0, frac=5.0))
    ep = datetime(2026, 8, 27, 14, 0, tzinfo=ZoneInfo("UTC")).timestamp()
    fills = qr.fills_of_session(
        [_order(1, "X", 100.0, 10.05, ep, "[MIRROR] v9 open {}")],  # 无 last_px
        "2026-08-27", _ET)
    row = qr.equity_plane("2026-08-27", _qc(995.0), fills,
                          {"legacy_alive": {}, "scaled_alive": {}}, {}, {})
    ml = row["attribution"]["rebalance_mirror_lag"]
    assert ml["usd"] == 105.0
    assert ml["per_leg"][0]["full_window"] is True
    assert row["attribution"]["slippage"]["usd"] == 0.0
    assert row["attribution"]["slippage"]["unreferenced"] == ["X"]
    assert row["unattributed_usd"] == 0.0
    assert "blocked_terms" not in row
    assert row["status"] == "ok"


def test_mirror_lag_blocked_when_decision_close_missing(monkeypatch, eq_env):
    """决策日收盘价缺了才是真算不出 —— 拦成 partial,不许静默给 ok。"""
    _closes_by_session(monkeypatch, {
        "2026-08-27": {"X": 10.0, "Y": 1.0},
        "2026-08-26": {"Y": 1.0},                # X 在决策日没有收盘价
    })
    monkeypatch.setattr(qr, "prev_report",
                        lambda s: _prev(100_000.0 - 5.0, 2000.0, frac=5.0))
    ep = datetime(2026, 8, 27, 14, 0, tzinfo=ZoneInfo("UTC")).timestamp()
    fills = qr.fills_of_session(
        [_order(1, "X", 100.0, 10.05, ep, "[MIRROR] v9 open {}", last_px=10.0)],
        "2026-08-27", _ET)
    row = qr.equity_plane("2026-08-27", _qc(995.0), fills,
                          {"legacy_alive": {}, "scaled_alive": {}}, {}, {})
    ml = row["attribution"]["rebalance_mirror_lag"]
    assert ml["usd"] == 0.0 and ml["unpriced"] == ["X"]
    assert any("镜像滞后算不全" in b for b in row["blocked_terms"])
    assert row["status"] == "partial"


def test_k_effective_rolls_permanent_steps_into_constant(monkeypatch, tmp_path,
                                                         eq_env):
    """冻结后:K_eff = 冻结值 + 盘上历史台阶 + 当天内存台阶,逐项可对。"""
    _closes_by_session(monkeypatch, {
        "2026-08-27": {"X": 10.0, "Y": 1.0},
        "2026-08-26": {"X": 9.0, "Y": 1.0},
    })
    roll = tmp_path / "rolloff.json"
    roll.write_text(json.dumps({"measured_on": "2026-08-25",
                                "k_equity": 99_000.0}))
    monkeypatch.setattr(qr, "ROLLOFF_PATH", roll)
    rep_dir = tmp_path / "reports"
    rep_dir.mkdir()
    (rep_dir / "qc_reconcile_2026-08-26.json").write_text(json.dumps({
        "equity_check": {"attribution": {
            "rebalance_mirror_lag": {"usd": 90.0},
            "slippage": {"usd": 10.0}}}}))
    # 毒饵:当天自己的旧报告(上一趟留的)。当天台阶只能从内存加 ——
    # k_effective 的上界若误放宽到 <=,这份就会被再计一次,K_eff 凭空多 77。
    (rep_dir / "qc_reconcile_2026-08-27.json").write_text(json.dumps({
        "equity_check": {"attribution": {
            "rebalance_mirror_lag": {"usd": 70.0},
            "slippage": {"usd": 7.0}}}}))
    monkeypatch.setattr(qr, "REPORT_DIR", rep_dir)
    monkeypatch.setattr(qr, "prev_report",
                        lambda s: _prev(100_000.0 - 105.0, 2000.0, frac=5.0))
    ep = datetime(2026, 8, 27, 14, 0, tzinfo=ZoneInfo("UTC")).timestamp()
    fills = qr.fills_of_session(
        [_order(1, "X", 100.0, 10.05, ep, "[MIRROR] v9 open {}", last_px=10.0)],
        "2026-08-27", _ET)
    row = qr.equity_plane("2026-08-27", _qc(995.0), fills,
                          {"legacy_alive": {}, "scaled_alive": {}}, {}, {})
    # 99,000(冻结) + 100(8/26 台阶 90+10) + 105(当天 100+5) = 99,205
    assert row["k_effective_usd"] == 99_205.0
    assert row["k_steps_counted"] == 2
    assert row["D_minus_K_effective_usd"] == round(100_000.0 - 99_205.0, 2)
    assert "k_steps_missing" not in row
    assert row["status"] == "ok"


def test_k_effective_missing_step_blocks_verdict(monkeypatch, tmp_path,
                                                 eq_env):
    """中间某天归因没出 → K_eff 有洞。洞必须**点名**并拦住 ok,静默跳过
    会把洞演成"漂移",下一步就是有人去追一个不存在的错。"""
    _closes_by_session(monkeypatch, {
        "2026-08-27": {"X": 10.0, "Y": 1.0},
        "2026-08-26": {"X": 10.0, "Y": 1.0},     # 无滞后,免得脏了断言
    })
    roll = tmp_path / "rolloff.json"
    roll.write_text(json.dumps({"measured_on": "2026-08-25",
                                "k_equity": 99_000.0}))
    monkeypatch.setattr(qr, "ROLLOFF_PATH", roll)
    rep_dir = tmp_path / "reports"
    rep_dir.mkdir()
    (rep_dir / "qc_reconcile_2026-08-26.json").write_text(json.dumps({
        "equity_check": {"status": "pending"}}))     # 那天没出归因
    monkeypatch.setattr(qr, "REPORT_DIR", rep_dir)
    monkeypatch.setattr(qr, "prev_report",
                        lambda s: _prev(100_000.0, 2000.0, frac=5.0))
    row = qr.equity_plane("2026-08-27", _qc(2000.0), [],
                          {"legacy_alive": {}, "scaled_alive": {}}, {}, {})
    assert row["k_steps_missing"] == ["2026-08-26"]
    assert any("k_effective 缺" in b for b in row["blocked_terms"])
    assert row["status"] == "partial"


def test_dividend_basis_only_required_for_strategies_in_P(monkeypatch, eq_env):
    """在册但未入 P 的账本(挂载前的 aeus)不许把裁决拦成 partial;
    但它的累计股息照记 —— 明天它入 P 时才有前日基准可比。"""
    monkeypatch.setattr(qr, "prev_report",
                        lambda s: _prev(100_000.0 - 602.0, 388.0, frac=5.0))
    monkeypatch.setattr(qr, "_ledger_accounts", lambda s: (
        {st: {"as_of": s, "equity": 1_000_000.0, "cumulative_dividends": 0.0}
         for st in ("mrpt", "mtfs", "ssrs", "aiss", "bdc", "aeus")}, []))
    built = {"legacy_alive": {"mtfs": [{"pair": "A/B",
                                        "open_date": "2026-08-05"}]},
             "scaled_alive": {}}
    snap = _snap_pair("A/B", [{"date": "2026-08-26", "unrealized_pnl": 0.0},
                              {"date": "2026-08-27", "unrealized_pnl": 602.0}])
    row = qr.equity_plane("2026-08-27", _qc(388.0), [], built, snap, {})
    assert "aeus" in row["cumulative_dividends"]          # 照记
    assert not any("aeus" in b for b in row.get("blocked_terms", []))
    assert row["status"] == "ok"


def test_k_effective_rolls_onboarding_deposit_as_step(monkeypatch, tmp_path,
                                                      eq_env):
    """策略挂载(QC 保证金建仓,无物理入金)= P 多一个策略净值而 Q 不变,
    差额是 K 的定义域:onboard_log.deposit_K 按"落在哪个场次"滚入 K_eff。
    挂载时刻 8/26 21:00Z(= 8/26 收盘后)→ 归 8/27 场次;当天内存台阶要算,
    盘上走读(其他天)不能重复算。"""
    _closes_by_session(monkeypatch, {
        "2026-08-27": {"X": 10.0, "Y": 1.0},
        "2026-08-26": {"X": 10.0, "Y": 1.0},
    })
    roll = tmp_path / "rolloff.json"
    roll.write_text(json.dumps({"measured_on": "2026-08-25",
                                "k_equity": 99_000.0}))
    monkeypatch.setattr(qr, "ROLLOFF_PATH", roll)
    rep_dir = tmp_path / "reports"; rep_dir.mkdir()
    (rep_dir / "qc_reconcile_2026-08-26.json").write_text(json.dumps({
        "equity_check": {"attribution": {
            "rebalance_mirror_lag": {"usd": 0.0}, "slippage": {"usd": 0.0}}}}))
    monkeypatch.setattr(qr, "REPORT_DIR", rep_dir)
    exst = tmp_path / "exporter_state.json"
    exst.write_text(json.dumps({"scalars": {"aeus": 1.0}, "onboard_log": [
        {"strategy": "aeus", "at": "2026-08-26T21:00:00+00:00",
         "deposit_K": 1_000.0}]}))
    monkeypatch.setattr(rolloff, "EXPORTER_STATE", exst)
    # 前一日 D 必须比今日**低一个台阶**:策略上车那天 P 多出它的净值而 Q 不变,
    # ΔD 必然跳 +deposit_K。原夹具写 100,000(ΔD=0 却又挂载了)物理上自相矛盾,
    # 正是那个矛盾让"台阶没进 known"在测试里无声通过。
    monkeypatch.setattr(qr, "prev_report",
                        lambda s: _prev(99_000.0, 2000.0, frac=5.0))
    row = qr.equity_plane("2026-08-27", _qc(2000.0), [],
                          {"legacy_alive": {}, "scaled_alive": {}}, {}, {})
    assert row["k_onboard_step_usd"] == 1_000.0
    assert row["k_effective_usd"] == 100_000.0          # 99,000 + 0(8/26) + 1,000
    # 归属唯一:同一挂载不能既算进 8/26 又算进 8/27
    assert qr.onboard_step("2026-08-26") == 0.0
    assert qr.onboard_step("2026-08-27") == 1_000.0
    # —— 台阶必须**同时**进 k_effective 与 known(成对入账)——
    # 只进前者:ΔD 跳了一整个 deposit_K 而判据无人认领 → unattributed 吞下整笔,
    # 在一本干净的账上报假 breach(2026-09-02 AEUS 上车实测 ~1,490bp)。
    # 只进后者:D − k_eff 会跳一个台阶,把"真漂移"这个读数废掉。
    assert row["attribution"]["onboarding_step"]["usd"] == 1_000.0
    assert row["unattributed_usd"] == 0.0
    assert row["D_minus_K_effective_usd"] == 0.0
    assert row["status"] == "ok"        # ← 旧版缺这行,bug 就是从这里溜过去的


def test_wrong_security_mapping_is_named_not_silently_priced(monkeypatch,
                                                             eq_env):
    """QC 历史首名撞上 Polygon 上另一家真公司 → 必须指名报错,不能静默用错价。

    2026-09-03 真实案例:Revvity 在 QC 显示为 EGG(EG&G→PerkinElmer→Revvity),
    而 Polygon 的 EGG 是 Enigmatig Limited(9/2 收 2.76 vs Revvity 130.94)。
    前八例历史首名在 Polygon 上不存在、会当场抛"收盘价缺";这一例查得到,
    868 股会让 Q 少算 $111,260 = 129bp,且只表现为"交叉校验失败",无线索。
    """
    # X 的官方收盘 10.0,但 QC 自己给它标 0.5 —— 20 倍差,只可能是换了证券
    monkeypatch.setattr(qr, "prev_report", lambda s: None)
    qc = _qc(388.0)
    qc["prices"] = dict(qc["prices"], X=0.5)
    row = qr.equity_plane("2026-08-27", qc, [], {}, {}, {})
    assert row["status"] == "pending"
    assert "别的" in row["note"] and "X" in row["note"]
    assert "D_usd" not in row              # 不出裁决,不拿错价算 Q


def test_price_sanity_guard_tolerates_ordinary_staleness(monkeypatch, eq_env):
    """守卫不能误伤:payload 价停在 ~15:45,几十分钟的陈旧度必须放行。"""
    monkeypatch.setattr(qr, "prev_report", lambda s: None)
    qc = _qc(388.0)
    qc["prices"] = dict(qc["prices"], X=9.0)     # 官方 10.0,差 10% —— 正常
    row = qr.equity_plane("2026-08-27", qc, [], {}, {}, {})
    assert row["status"] == "baseline" and row["D_usd"] == 100_000.0
