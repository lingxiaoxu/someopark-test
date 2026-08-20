"""tests/test_tick_quote_topup.py — the weekly-close blind spot (§8.2-5).

`_exec_task` snapshots the triggering run's own series, then calls `decide_all.run()`,
which scans every registered series. Everything else feeding that scan was already
global (`predict_all.run()`); quotes were not, so every other series tripped the
staleness hard gate before its edge was ever computed. Measured on the live book:
08-13 103 force-passes, 08-14 204, 08-17 55, all `stale_inputs pred=0h quotes=8.6..15.9h`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from prediction_market_macro.config.registry import REGISTRY
from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.jobs.tick import _top_up_stale_quotes
from prediction_market_macro.ops.decide_all import QUOTE_STALE_H

NOW = datetime(2026, 8, 21, 18, 30, tzinfo=timezone.utc)     # a weekly_close arm


class FakeMD:
    """Records which series were re-snapshotted; `boom` raises for one of them."""

    def __init__(self, boom: str | None = None):
        self.calls: list[str] = []
        self.boom = boom

    def snapshot_series(self, series: str) -> int:
        self.calls.append(series)
        if series == self.boom:
            raise RuntimeError("venue 503")
        return 7


def _seed(conn, series: str, quote_age_h: float | None, status: str = "active"):
    """One active contract for `series`, with a quote `quote_age_h` old (None = no quote)."""
    tkr = f"{series}-26AUG21-T1"
    conn.execute(
        "INSERT OR REPLACE INTO contracts(ticker, series, event_ticker, period, status,"
        " first_seen_ts) VALUES(?,?,?,?,?,?)",
        (tkr, series, f"{series}-26AUG21", "26AUG21", status, NOW.isoformat()))
    if quote_age_h is not None:
        conn.execute(
            "INSERT OR REPLACE INTO quotes(ts, ticker, yes_bid, yes_ask, bid_depth,"
            " ask_depth) VALUES(?,?,?,?,?,?)",
            ((NOW - timedelta(hours=quote_age_h)).isoformat(), tkr, 0.4, 0.6, 50, 50))
    conn.commit()


def test_stale_series_are_refreshed_and_fresh_ones_are_not(tmp_path):
    conn = init_db(tmp_path / "t.db")
    _seed(conn, "KXNATGASW", 0.05)          # the run's own series — just snapshotted
    _seed(conn, "KXCPI", 9.6)               # the observed 08-14 age
    _seed(conn, "KXPAYROLLS", 15.9)         # the observed 08-13 age
    md = FakeMD()
    out = _top_up_stale_quotes(conn, md, NOW)
    assert set(md.calls) == {"KXCPI", "KXPAYROLLS"}
    assert out == {"KXCPI": 7, "KXPAYROLLS": 7}


def test_boundary_is_decide_alls_own_constant(tmp_path):
    """Refresh exactly when decide_all would have force-passed, not a hair before."""
    conn = init_db(tmp_path / "t.db")
    _seed(conn, "KXCPI", QUOTE_STALE_H - 0.01)       # gate passes it -> leave alone
    _seed(conn, "KXU3", QUOTE_STALE_H + 0.01)        # gate force-passes it -> refresh
    md = FakeMD()
    _top_up_stale_quotes(conn, md, NOW)
    assert md.calls == ["KXU3"]


def test_series_with_no_active_contract_costs_no_api_call(tmp_path):
    """decide_all never reaches a series with no active period; neither should we."""
    conn = init_db(tmp_path / "t.db")
    _seed(conn, "KXCPI", 99.0, status="settled")
    _seed(conn, "KXU3", None)                        # active, but never quoted
    md = FakeMD()
    assert _top_up_stale_quotes(conn, md, NOW) == {}
    assert md.calls == []


def test_a_venue_error_degrades_to_todays_behaviour(tmp_path):
    """One failing series must not abort the task or block the others."""
    conn = init_db(tmp_path / "t.db")
    _seed(conn, "KXCPI", 9.6)
    _seed(conn, "KXU3", 9.6)
    md = FakeMD(boom="KXCPI")
    out = _top_up_stale_quotes(conn, md, NOW)
    assert set(md.calls) == {"KXCPI", "KXU3"}        # both attempted
    assert out == {"KXU3": 7}                        # only the good one reported


def test_no_registry_series_is_silently_skipped(tmp_path):
    """Guards against a registry addition quietly falling out of the top-up."""
    conn = init_db(tmp_path / "t.db")
    for spec in REGISTRY.values():
        _seed(conn, spec.ticker, 12.0)
    md = FakeMD()
    _top_up_stale_quotes(conn, md, NOW)
    assert set(md.calls) == {spec.ticker for spec in REGISTRY.values()}
