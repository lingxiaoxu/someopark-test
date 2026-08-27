"""Frozen bet ledger (ops/settle_bets.py).

A settled match's bet is computed ONCE (point-in-time strength + point-in-time calibration)
and persisted, then never recomputed. This is the fix for the reported day-to-day drift in
the Accuracy/PnL, PriceTrack and PnL-report views (the calibration was global → look-ahead).

The invariants: freezing is append-only + idempotent, a LATER match never rewrites an
EARLIER bet, and rebuilding the report is byte-stable.

Club scope (TRANSFORM_PLAN R8): the ledger records OUR live track record from launch, so
freezing is now gated on an enabled competition, a kickoff inside the 14-day window, AND a
PRE milestone row — a settled match with no pre-match entry quotes has no bet to freeze.
Every fixture below therefore carries a PRE snapshot; without one the ledger stays empty by
design, not by accident.
"""
from __future__ import annotations

from prediction_market_soccer.ingest import store
from prediction_market_soccer.ops import performance_report
from prediction_market_soccer.ops.settle_bets import freeze_settled_bets, frozen_pick
from prediction_market_soccer.tests import clubctx


def _settled(c, api, home, away, hg, ag, days_ago, *, pre=(0.55, 0.25, 0.30)):
    clubctx.seed_fixture(c, api, home, away, hg=hg, ag=ag, days_ago=days_ago)
    ph, pd_, pa = pre
    store.upsert(c, "milestone_snapshot", {
        "fixture_api_id": api, "milestone": "PRE", "elapsed": 0, "status_short": "NS",
        "home_goals": 0, "away_goals": 0,
        "poly_home_ask": ph, "poly_draw_ask": pd_, "poly_away_ask": pa,
        "price_source": "candlestick"}, pk=["fixture_api_id", "milestone"])


def test_freeze_is_append_only_and_idempotent():
    c = clubctx.mem_db()
    clubctx.seed_teams(c, clubctx.ARSENAL, clubctx.IPSWICH)
    _settled(c, 1, clubctx.ARSENAL, clubctx.IPSWICH, 2, 0, days_ago=6)
    assert freeze_settled_bets(c) == 1     # first run freezes the one settled match
    assert freeze_settled_bets(c) == 0     # second run adds nothing — history is not recomputed


def test_later_match_does_not_change_earlier_bet():
    """The core guarantee: a bet, once placed, never changes as later matches settle."""
    c = clubctx.mem_db()
    clubctx.seed_teams(c, clubctx.ARSENAL, clubctx.IPSWICH, clubctx.BRIGHTON, clubctx.BRENTFORD)
    _settled(c, 1, clubctx.ARSENAL, clubctx.IPSWICH, 2, 0, days_ago=9)      # earlier match A
    freeze_settled_bets(c)
    a_before = frozen_pick(c, {"api_id": 1}, "arsenal", "ipswich")
    assert a_before is not None

    _settled(c, 2, clubctx.BRIGHTON, clubctx.BRENTFORD, 1, 1, days_ago=2)   # a LATER match B
    freeze_settled_bets(c)
    a_after = frozen_pick(c, {"api_id": 1}, "arsenal", "ipswich")
    assert a_after == a_before              # match A's frozen bet is unchanged by match B


def test_frozen_pick_self_heals_when_not_yet_frozen():
    c = clubctx.mem_db()
    clubctx.seed_teams(c, clubctx.ARSENAL, clubctx.IPSWICH)
    _settled(c, 1, clubctx.ARSENAL, clubctx.IPSWICH, 2, 0, days_ago=6)
    # No explicit freeze — the first frozen_pick() must freeze it, then return the payload.
    mr = frozen_pick(c, {"api_id": 1}, "arsenal", "ipswich")
    assert mr is not None and "pick" in mr
    assert c.execute("SELECT COUNT(*) FROM settled_bet").fetchone()[0] == 1


def test_no_pre_quotes_means_nothing_to_freeze():
    """The cold-start short-circuit: a settled match whose PRE entry quotes were never
    captured has no bet to record, so the ledger stays empty instead of inventing one."""
    c = clubctx.mem_db()
    clubctx.seed_teams(c, clubctx.ARSENAL, clubctx.IPSWICH)
    clubctx.seed_fixture(c, 1, clubctx.ARSENAL, clubctx.IPSWICH, hg=2, ag=0, days_ago=6)
    assert freeze_settled_bets(c) == 0
    assert c.execute("SELECT COUNT(*) FROM settled_bet").fetchone()[0] == 0


def test_report_is_stable_across_builds():
    c = clubctx.mem_db()
    clubctx.seed_teams(c, clubctx.ARSENAL, clubctx.IPSWICH)
    _settled(c, 1, clubctx.ARSENAL, clubctx.IPSWICH, 2, 0, days_ago=6)
    r1 = performance_report.build(conn=c)
    r2 = performance_report.build(conn=c)
    assert r1.n_settled == r2.n_settled == 1
    assert r1.realized_pnl_cents_total == r2.realized_pnl_cents_total
    assert r1.argmax_record == r2.argmax_record
    assert r1.realized_record == r2.realized_record
