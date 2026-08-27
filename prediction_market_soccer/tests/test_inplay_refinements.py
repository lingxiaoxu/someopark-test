"""Tests for the live in-play model refinements (①②③⑤) and their revert flags.

Baseline behaviour must be exactly recoverable via the flags, and each refinement must move
the fair price in the intended direction. See research/inplay_calibration.py for the backtest.
"""
import pytest

from prediction_market_soccer.model.inplay import live_match_prob, _remaining_scoring_fraction


# ② rising goal-hazard: more of the scoring is still to come than a flat clock implies.
def test_goal_hazard_endpoints_and_late_skew():
    assert _remaining_scoring_fraction(0.0, 90.0) == pytest.approx(1.0)
    assert _remaining_scoring_fraction(90.0, 90.0) == pytest.approx(0.0)
    # at 25' the rising hazard leaves MORE than the linear (90-25)/90 share still to play.
    assert _remaining_scoring_fraction(25.0, 90.0) > (90.0 - 25.0) / 90.0
    # monotonically decreasing through the match.
    fr = [_remaining_scoring_fraction(m, 90.0) for m in range(0, 91, 10)]
    assert all(b <= a + 1e-12 for a, b in zip(fr, fr[1:]))


def test_hazard_flag_reverts_to_linear():
    # use_hazard=False must reproduce the flat-tau remaining goals exactly.
    lp = live_match_prob(1.6, 1.0, 30, 0, 0, use_hazard=False, residual_rho=0.0)
    tau = (90 - 30) / 90.0
    assert lp.exp_remaining_goals == pytest.approx((1.6 + 1.0) * tau, rel=1e-6)


# ① xG shading weight ramps with minutes — an early over-performance moves the price LESS
#    than the same signal read at full-trust minute.
def test_xg_shading_is_gentler_early():
    kw = dict(home_goals=0, away_goals=0, xg_home=1.2, xg_away=0.0)  # home wildly out-creating
    no_xg = live_match_prob(1.3, 1.3, 25, 0, 0).p_home
    ramped = live_match_prob(1.3, 1.3, 25, **kw, xg_full_trust_min=60.0).p_home
    full = live_match_prob(1.3, 1.3, 25, **kw, xg_full_trust_min=0.0).p_home   # old always-full weight
    # over-performance lifts home either way, but the ramped (sample-size-aware) lift is smaller.
    assert no_xg < ramped < full


# ③ Dixon-Coles residual rho lifts the low-score draw mass.
def test_residual_rho_lifts_draw():
    level = dict(minute=80, home_goals=1, away_goals=1)
    indep = live_match_prob(1.4, 1.2, **level, residual_rho=0.0).p_draw
    dc = live_match_prob(1.4, 1.2, **level, residual_rho=-0.05).p_draw
    assert dc > indep


# ⑤ real stoppage time extends the scoring window (score not locked during 90+N).
def test_stoppage_extends_window():
    locked = live_match_prob(1.5, 1.2, 90, 1, 1, injury_time=0.0)
    stoppage = live_match_prob(1.5, 1.2, 90, 1, 1, injury_time=5.0)
    assert locked.tau == 0.0 and locked.exp_remaining_goals == pytest.approx(0.0)
    assert stoppage.tau > 0.0 and stoppage.exp_remaining_goals > 0.0


# ④ state-scaling is OFF by default (backtest regressed) — default must equal the fixed-mult path.
def test_state_scaling_off_by_default():
    lead = dict(minute=70, home_goals=1, away_goals=0)
    default = live_match_prob(1.6, 1.0, **lead)
    explicit_off = live_match_prob(1.6, 1.0, **lead, state_scaling=False)
    assert default.p_home == pytest.approx(explicit_off.p_home)
    assert default.p_draw == pytest.approx(explicit_off.p_draw)
