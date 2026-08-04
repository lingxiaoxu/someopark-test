"""ops/decide_all._aaa_information_gate (§27.4) — do not trade what the other side
can already read.

KXAAAGASW settles on AAA's daily national average. Our AAA_DAILY scrape starts
2026-07-31 and cannot be backfilled, so whenever energy.py falls back to the weekly
GASREGW proxy we are quoting at a known information disadvantage — the #743 loss, where
the model was honestly calibrated and lost anyway. The gate turns that state into a
forced PASS instead of a trade.

Everything here is offline: the gate only reads a pred row and the AAA_DAILY table.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.ops.decide_all import _aaa_information_gate

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def conn(tmp_path):
    return init_db(tmp_path / "t.db")


def _pred(conn, series: str, mode: str | None, asof: str = "2026-08-04T09:00:00+00:00"):
    inputs = json.dumps({"mode": mode} if mode is not None else {})
    conn.execute(
        "INSERT OR REPLACE INTO preds(series, period, asof, model_version, dist_json,"
        " inputs_json, data_horizon, created_ts) VALUES(?,?,?,?,?,?,?,?)",
        (series, "2026-08-07", asof, "energy/0.6.0", "{}", inputs, asof, asof))
    conn.commit()
    return conn.execute("SELECT * FROM preds WHERE series=? AND asof=?",
                        (series, asof)).fetchone()


def _aaa(conn, day: str, kt: str | None = None):
    kt = kt or f"{day}T13:00:00+00:00"
    conn.execute("INSERT OR REPLACE INTO fred_obs VALUES(?,?,?,?,?,?)",
                 ("AAA_DAILY", day, 4.1, day, kt, kt))
    conn.commit()


# ── the pass-through cases ──────────────────────────────────────────────────
def test_other_series_are_untouched(conn):
    pr = _pred(conn, "KXNATGASW", None)
    assert _aaa_information_gate(conn, pr, NOW) is None


def test_fresh_daily_anchor_trades(conn):
    _aaa(conn, "2026-08-03")
    pr = _pred(conn, "KXAAAGASW", "aaa_daily_anchor")
    assert _aaa_information_gate(conn, pr, NOW) is None


def test_two_day_old_anchor_still_trades(conn):
    """AAA publishes 7 days a week, so the age check is meant to fire on OUR collector
    being down — two days is the documented tolerance, not one."""
    _aaa(conn, "2026-08-02")
    pr = _pred(conn, "KXAAAGASW", "aaa_daily_anchor")
    assert _aaa_information_gate(conn, pr, NOW) is None


# ── the blocked cases ───────────────────────────────────────────────────────
def test_proxy_branch_is_blocked_however_confident_it_looks(conn):
    _aaa(conn, "2026-08-03")                     # feed is fine; the PRED took the proxy
    pr = _pred(conn, "KXAAAGASW", "drift_regression(n=28)")
    assert "aaa_proxy_only" in _aaa_information_gate(conn, pr, NOW)


def test_cold_start_fallback_is_blocked_too(conn):
    pr = _pred(conn, "KXAAAGASW", "damped_trend_fallback")
    assert "aaa_proxy_only" in _aaa_information_gate(conn, pr, NOW)


def test_stalled_collector_blocks_even_an_anchor_pred(conn):
    """The pred can claim the anchor branch while the feed has since gone quiet — the
    staleness gate upstream only ages the PRED, not the source behind it."""
    _aaa(conn, "2026-07-31")
    pr = _pred(conn, "KXAAAGASW", "aaa_daily_anchor")
    assert _aaa_information_gate(conn, pr, NOW) == "aaa_daily_stale 4d > 2d"


def test_empty_feed_blocks(conn):
    pr = _pred(conn, "KXAAAGASW", "aaa_daily_anchor")
    assert _aaa_information_gate(conn, pr, NOW) == "aaa_daily_missing"


def test_age_is_measured_pit_not_off_the_whole_table(conn):
    """A row we have not learned yet must not count as freshness."""
    _aaa(conn, "2026-08-03", kt="2026-08-04T23:00:00+00:00")     # after NOW
    _aaa(conn, "2026-07-31")
    pr = _pred(conn, "KXAAAGASW", "aaa_daily_anchor")
    assert _aaa_information_gate(conn, pr, NOW) == "aaa_daily_stale 4d > 2d"


def test_unreadable_inputs_block_rather_than_crash(conn):
    asof = "2026-08-04T09:00:00+00:00"
    conn.execute(
        "INSERT OR REPLACE INTO preds(series, period, asof, model_version, dist_json,"
        " inputs_json, data_horizon, created_ts) VALUES(?,?,?,?,?,?,?,?)",
        ("KXAAAGASW", "2026-08-07", asof, "energy/0.6.0", "{}", "{not json",
         asof, asof))
    conn.commit()
    pr = conn.execute("SELECT * FROM preds WHERE series='KXAAAGASW'").fetchone()
    assert _aaa_information_gate(conn, pr, NOW) == "aaa_inputs_unreadable"
