"""Tests for the backtest framework (plan 05 §6)."""
from __future__ import annotations

import sqlite3

from prediction_market.backtest.metrics import rolling_brier
from prediction_market.backtest.replay import walk_forward_replay
from prediction_market.ingest import store


def test_rolling_brier_window():
    probs = [[1.0, 0.0, 0.0]] * 4 + [[0.0, 1.0, 0.0]] * 4
    outcomes = [0, 0, 0, 0, 0, 0, 0, 0]   # last 4 are wrong-confident
    rb = rolling_brier(probs, outcomes, window=4)
    assert len(rb) == 8
    assert rb[0] == 0.0                    # early window perfect
    assert rb[-1] > rb[0]                  # later window degraded


def test_walk_forward_no_future_function():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row; store.init_db(c)
    for api_id, cid in ((10, "spain"), (20, "saudi_arabia"), (30, "brazil"), (40, "haiti")):
        store.upsert(c, "team_meta", {"api_id": api_id, "group_code": "G", "fifa_rank": None,
                     "canonical_team_id": cid, "updated_at": store.utcnow()}, pk=["api_id"])
    for i, (h, a, gh, ga, ts) in enumerate([
            (10, 20, 3, 0, "2026-06-11T12:00:00+00:00"),
            (30, 40, 4, 0, "2026-06-12T12:00:00+00:00")]):
        store.upsert(c, "fixture", {"api_id": 100 + i, "league_id": 1, "season": 2026,
            "round": "Group Stage - 1", "status_short": "FT", "home_api_id": h, "away_api_id": a,
            "home_goals": gh, "away_goals": ga, "kickoff_ts": ts, "updated_at": store.utcnow()},
            pk=["api_id"])
    res = walk_forward_replay(conn=c)
    assert res.n_matches == 2
    assert 0.0 <= res.static_brier <= 2.0
    assert res.baseline_brier > 0      # uniform baseline computed
    assert len(res.rolling_brier_sequential) == 2
