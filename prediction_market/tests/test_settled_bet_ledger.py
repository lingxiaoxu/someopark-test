"""Frozen bet ledger (ops/settle_bets.py).

A settled match's bet is computed ONCE (point-in-time strength + point-in-time calibration)
and persisted, then never recomputed. This is the fix for the reported day-to-day drift in
the Accuracy/PnL, PriceTrack and PnL-report views (the calibration was global → look-ahead).

The invariants: freezing is append-only + idempotent, a LATER match never rewrites an
EARLIER bet, and rebuilding the report is byte-stable.
"""
from __future__ import annotations

import sqlite3

from prediction_market.ingest import store
from prediction_market.ops import performance_report
from prediction_market.ops.settle_bets import freeze_settled_bets, frozen_pick


def _mem_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    store.init_db(c)
    return c


def _teams(c, pairs):
    for api, cid in pairs:
        store.upsert(c, "team_meta",
                     {"api_id": api, "canonical_team_id": cid, "updated_at": store.utcnow()},
                     pk=["api_id"])


def _settled(c, api, h, a, hg, ag, kickoff):
    store.upsert(c, "fixture",
                 {"api_id": api, "league_id": 1, "season": 2026, "status_short": "FT",
                  "home_api_id": h, "away_api_id": a, "home_goals": hg, "away_goals": ag,
                  "round": "Group Stage - 1", "kickoff_ts": kickoff, "updated_at": store.utcnow()},
                 pk=["api_id"])


def test_freeze_is_append_only_and_idempotent():
    c = _mem_db()
    _teams(c, [(10, "france"), (20, "senegal")])
    _settled(c, 1, 10, 20, 2, 0, "2026-06-15T18:00:00Z")
    assert freeze_settled_bets(c) == 1     # first run freezes the one settled match
    assert freeze_settled_bets(c) == 0     # second run adds nothing — history is not recomputed


def test_later_match_does_not_change_earlier_bet():
    """The core guarantee: a bet, once placed, never changes as later matches settle."""
    c = _mem_db()
    _teams(c, [(10, "france"), (20, "senegal"), (30, "brazil"), (40, "japan")])
    _settled(c, 1, 10, 20, 2, 0, "2026-06-15T18:00:00Z")     # earlier match A
    freeze_settled_bets(c)
    a_before = frozen_pick(c, {"api_id": 1}, "france", "senegal")
    assert a_before is not None

    _settled(c, 2, 30, 40, 1, 1, "2026-06-20T18:00:00Z")     # a LATER match B settles
    freeze_settled_bets(c)
    a_after = frozen_pick(c, {"api_id": 1}, "france", "senegal")
    assert a_after == a_before              # match A's frozen bet is unchanged by match B


def test_frozen_pick_self_heals_when_not_yet_frozen():
    c = _mem_db()
    _teams(c, [(10, "france"), (20, "senegal")])
    _settled(c, 1, 10, 20, 2, 0, "2026-06-15T18:00:00Z")
    # No explicit freeze — the first frozen_pick() must freeze it, then return the payload.
    mr = frozen_pick(c, {"api_id": 1}, "france", "senegal")
    assert mr is not None and "pick" in mr
    assert c.execute("SELECT COUNT(*) FROM settled_bet").fetchone()[0] == 1


def test_report_is_stable_across_builds():
    c = _mem_db()
    _teams(c, [(10, "france"), (20, "senegal")])
    _settled(c, 1, 10, 20, 2, 0, "2026-06-15T18:00:00Z")
    r1 = performance_report.build(conn=c)
    r2 = performance_report.build(conn=c)
    assert r1.n_settled == r2.n_settled == 1
    assert r1.realized_pnl_cents_total == r2.realized_pnl_cents_total
    assert r1.argmax_record == r2.argmax_record
    assert r1.realized_record == r2.realized_record
