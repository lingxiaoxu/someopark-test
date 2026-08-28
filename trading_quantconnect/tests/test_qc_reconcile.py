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
from reconcile import qc_reconcile as qr             # noqa: E402

_ET = ZoneInfo("America/New_York")


class _NetBan:
    def __init__(self, *a, **k):
        raise AssertionError("TEST NETWORK BAN: QcClient touched in unit test")


@pytest.fixture(autouse=True)
def _ban_network(monkeypatch):
    monkeypatch.setattr(qc_api, "QcClient", _NetBan)
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
    monkeypatch.setattr(rolloff, "official_eod",
                        lambda: ("2026-08-27", {"mrpt": 1_200_000.0,
                                                "mtfs": 1_200_000.0,
                                                "ssrs": 1_200_000.0,
                                                "aiss": 1_200_000.0,
                                                "bdc": 1_200_000.0}))
    monkeypatch.setattr(qr, "_ledger_accounts", lambda s: (
        {st: {"as_of": s, "equity": 1_000_000.0, "cumulative_dividends": 0.0}
         for st in ("mrpt", "mtfs", "ssrs", "aiss", "bdc")}, []))
    return tmp_path


def _prev(D, cash, frac=3.0):
    return {"equity_check": {"session": "2026-08-26", "D_usd": D,
                             "qc_cash": cash,
                             "fractional_residual": {"value_usd": frac},
                             "cumulative_dividends": {
                                 st: 0.0 for st in ("mrpt", "mtfs", "ssrs",
                                                    "aiss", "bdc")}}}


def _qc(cash):
    return {"equity": 5_900_000.0, "gross": 1_000_000.0, "cash": cash,
            "holdings_mv": 5_900_000.0 - cash, "quiet_gap": 0.0,
            "prices": {"X": 10.0}}


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


def test_equity_pending_when_official_eod_is_a_different_day(monkeypatch, eq_env):
    monkeypatch.setattr(rolloff, "official_eod",
                        lambda: ("2026-08-26", {"mrpt": 1.0}))
    row = qr.equity_plane("2026-08-27", _qc(388.0), [], {}, {}, {})
    assert row["status"] == "pending" and row["official_date"] == "2026-08-26"
    assert "D_usd" not in row


def test_equity_pending_when_qc_not_quiet(monkeypatch, eq_env):
    monkeypatch.setattr(qr, "prev_report", lambda s: _prev(100_000.0, 388.0))
    qc = _qc(388.0)
    qc["quiet_gap"] = qr.QUIET_TOL + 1.0
    row = qr.equity_plane("2026-08-27", qc, [], {}, {}, {})
    assert row["status"] == "pending" and "没静下来" in row["note"]


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
        qr.qc_orders(_C(), 1, page=100, hard_cap=300)
