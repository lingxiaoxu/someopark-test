"""Tests for the backtest framework (plan 05 §6)."""
from __future__ import annotations

from prediction_market_soccer.backtest.metrics import rolling_brier
from prediction_market_soccer.backtest.replay import walk_forward_replay
from prediction_market_soccer.tests import clubctx


def test_rolling_brier_window():
    probs = [[1.0, 0.0, 0.0]] * 4 + [[0.0, 1.0, 0.0]] * 4
    outcomes = [0, 0, 0, 0, 0, 0, 0, 0]   # last 4 are wrong-confident
    rb = rolling_brier(probs, outcomes, window=4)
    assert len(rb) == 8
    assert rb[0] == 0.0                    # early window perfect
    assert rb[-1] > rb[0]                  # later window degraded


def test_walk_forward_no_future_function():
    # The replay prices with the MERGED cross-league prior (clubs_all.json), so the
    # seeded clubs have to be real ids from that snapshot — a per-league model would
    # not rate them at all.
    c = clubctx.mem_db()
    clubctx.seed_teams(c, clubctx.ARSENAL, clubctx.IPSWICH, clubctx.BRIGHTON, clubctx.BRENTFORD)
    clubctx.seed_fixture(c, 100, clubctx.ARSENAL, clubctx.IPSWICH, hg=3, ag=0, days_ago=5)
    clubctx.seed_fixture(c, 101, clubctx.BRIGHTON, clubctx.BRENTFORD, hg=4, ag=0, days_ago=4)
    res = walk_forward_replay(conn=c)
    assert res.n_matches == 2
    assert 0.0 <= res.static_brier <= 2.0
    assert res.baseline_brier > 0      # uniform baseline computed
    assert len(res.rolling_brier_sequential) == 2
