"""Tests for the live corner-total model + signal — focus on NO FALSE TRIGGERS.
Run: python -m pytest prediction_market_soccer/tests/test_corner_signal.py -q
"""
import pytest

from prediction_market_soccer.model.inplay_corners import live_corners_fair
from prediction_market_soccer.strategy.inplay_tactics import corner_total_signal


# ── model ────────────────────────────────────────────────────────────────────

from prediction_market_soccer.model.inplay_constants import (
    MIN_CORNER_EDGE, CORNER_EDGE_ALARM)


def test_model_monotone_in_line():
    r = live_corners_fair(6, 60, 1, 0)
    ps = [r.p_over[L] for L in sorted(r.p_over)]
    assert all(ps[i] >= ps[i + 1] for i in range(len(ps) - 1))   # higher line → lower P(over)
    assert all(0.0 <= p <= 1.0 for p in ps)


def test_model_line_already_broken_is_certain():
    r = live_corners_fair(11, 80, 0, 0)
    assert r.p_over[10.5] == 1.0            # 11 corners already > 10.5


def test_model_more_corners_now_raises_pover():
    lo = live_corners_fair(4, 55, 0, 0).p_over[9.5]
    hi = live_corners_fair(9, 55, 0, 0).p_over[9.5]
    assert hi > lo


def test_model_time_decay_lowers_remaining():
    early = live_corners_fair(5, 30, 0, 0).nu_rem
    late = live_corners_fair(5, 75, 0, 0).nu_rem
    assert early > late >= 0.0


@pytest.mark.parametrize("c,m", [(None, 50), (-1, 50), (99, 50), (6, -5), (6, 200), (6, None)])
def test_model_rejects_bad_inputs(c, m):
    assert live_corners_fair(c, m, 0, 0).valid is False


def test_model_overdispersion_fatter_in_deep_tail():
    # a quiet late game (2 corners at 80'): remaining mean is small, so a high line is
    # deep in the upper tail — exactly where overdispersion (NegBin) puts more mass than
    # Poisson. (Below the remaining mean the relationship reverses, by construction.)
    from prediction_market_soccer.model.inplay_corners import live_corners_fair as f
    nb = f(2, 80, 0, 0, lines=(8.5,), dispersion_k=8).p_over[8.5]
    po = f(2, 80, 0, 0, lines=(8.5,), dispersion_k=1e9).p_over[8.5]
    assert nb > po


# ── signal: NO FALSE TRIGGERS ────────────────────────────────────────────────

GOOD_Q = {8.5: {"ask": 0.45, "bid": 0.43}}   # market cheap on the OVER vs a live-hot game


def test_signal_holds_without_quotes():
    assert corner_total_signal(9, 60, 1, 0, quotes=None).act == "HOLD"
    assert corner_total_signal(9, 60, 1, 0, quotes={}).act == "HOLD"


def test_signal_holds_when_corner_count_missing():
    assert corner_total_signal(None, 60, 1, 0, quotes=GOOD_Q).act == "HOLD"


@pytest.mark.parametrize("minute", [0, 5, 11, 90, 95, 120])
def test_signal_holds_outside_window(minute):
    assert corner_total_signal(9, minute, 1, 0, quotes=GOOD_Q).act == "HOLD"


@pytest.mark.parametrize("c", [-1, 41, 100])
def test_signal_holds_on_insane_corner_count(c):
    assert corner_total_signal(c, 60, 1, 0, quotes=GOOD_Q).act == "HOLD"


def test_signal_holds_on_subthreshold_edge():
    # a fair value that sits within the edge threshold of the ask → HOLD
    r = live_corners_fair(6, 60, 1, 0)
    p = r.p_over[8.5]
    q = {8.5: {"ask": round(p - 0.02, 3), "bid": round(p - 0.04, 3)}}   # 2pp < 7pp threshold
    assert corner_total_signal(6, 60, 1, 0, quotes=q).act == "HOLD"


def test_signal_fires_over_on_real_edge():
    """A hot game against a cheap OVER quote.

    The quote is chosen so the edge lands INSIDE the sanity band. The original test
    used ask=0.40, which produces an edge of 0.60 — six times the largest gap a real
    corner book has shown. That is the model disagreeing with the market structurally,
    and the tactic now refuses it (with an alarm) rather than trading one wrong view
    repeatedly; see test_absurd_edge_is_refused."""
    a = corner_total_signal(10, 55, 1, 1, quotes={8.5: {"ask": 0.82, "bid": 0.80}})
    assert a.act == "BUY" and a.side == "over"
    assert a.reason_key == "corner_value"
    assert MIN_CORNER_EDGE <= a.reason_args["edge"] <= CORNER_EDGE_ALARM


def test_absurd_edge_is_refused():
    """A 60-point model/book gap is a fault report, not an opportunity."""
    a = corner_total_signal(10, 55, 1, 1, quotes={8.5: {"ask": 0.40, "bid": 0.38}})
    assert a.act == "HOLD"


def test_signal_fires_under_on_real_edge():
    """Quiet game, OVER priced expensive → UNDER value.

    The ask is set so the UNDER edge lands inside the sanity band; the original 0.60
    produces a 0.68 edge, which the tactic now refuses as a prior fault."""
    a = corner_total_signal(2, 60, 0, 0, quotes={8.5: {"ask": 0.18, "bid": 0.16}})
    assert a.act == "BUY" and a.side == "under"
    assert MIN_CORNER_EDGE <= a.reason_args["edge"] <= CORNER_EDGE_ALARM


def test_signal_never_throws_on_fuzz():
    import random
    rng = random.Random(0)
    for _ in range(2000):
        c = rng.choice([None, -5, 0, 3, 9, 15, 41, 999])
        m = rng.choice([None, -1, 0, 12, 45, 60, 89, 90, 130])
        gh, ga = rng.randint(0, 5), rng.randint(0, 5)
        q = rng.choice([None, {}, {8.5: {"ask": rng.random(), "bid": rng.random()}},
                        {7.5: {"ask": 1.2}}, {9.5: {"ask": 0.0, "bid": -0.1}}])
        act = corner_total_signal(c, m, gh, ga, quotes=q)
        assert act.act in ("BUY", "HOLD")     # never SELL, never crash


def test_signal_picks_largest_edge_line():
    # two lines, both an over-edge; the bigger edge wins
    # Both edges must sit inside the sanity band, otherwise the wider one is refused
    # as a fault and "the bigger edge wins" is not what is being tested.
    q = {7.5: {"ask": 0.93, "bid": 0.91}, 8.5: {"ask": 0.80, "bid": 0.78}}
    a = corner_total_signal(9, 55, 1, 1, quotes=q)
    assert a.act == "BUY" and a.reason_args["line"] == 9   # over 8.5 = the 9+ line
