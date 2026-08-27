"""test_inplay_advance_model.py — unit + in-memory virtual-match tests for the live 2-way
ADVANCE model (model/inplay_advance.py) and the finer sequential shootout (model/penalties.py).
Plan 24 §2. No network, no DB — pure model math on a strength prior.

Run:  conda run -n someopark_run python -m pytest prediction_market_soccer/tests/test_inplay_advance_model.py -q
  or: conda run -n someopark_run python prediction_market_soccer/tests/test_inplay_advance_model.py
"""
from __future__ import annotations

from prediction_market_soccer.model.penalties import (
    shootout_win_prob_dp,
    shootout_conversions,
    shootout_win_prob_detailed,
)


# ── Sequential shootout DP ───────────────────────────────────────────────────
def test_shootout_symmetric_is_half():
    assert abs(shootout_win_prob_dp(0.75, 0.75) - 0.5) < 1e-9


def test_shootout_favourite_above_half():
    assert shootout_win_prob_dp(0.85, 0.65) > 0.5
    # complementary: A vs B and B vs A sum to 1
    p = shootout_win_prob_dp(0.85, 0.65)
    q = shootout_win_prob_dp(0.65, 0.85)
    assert abs((p + q) - 1.0) < 1e-9


def test_shootout_live_tally_conditioning():
    # A leads 3-1 after 3 kicks each → A should be a strong favourite vs the pre-shootout prior.
    base = shootout_win_prob_dp(0.75, 0.75)
    ahead = shootout_win_prob_dp(0.75, 0.75, taken_a=3, scored_a=3, taken_b=3, scored_b=1)
    assert ahead > base > 0.49
    assert ahead > 0.85
    # Insurmountable: A 3-0 after 3 each, only 2 kicks left for B → B can at most reach 2 < 3.
    clinched = shootout_win_prob_dp(0.75, 0.75, taken_a=3, scored_a=3, taken_b=3, scored_b=0)
    assert clinched > 0.99


# ── Live advance model invariants + virtual match ────────────────────────────
# The advance product only exists where a tie does (caps.advance), so the pair is a
# UEFA knockout one: Lyon (the qualifying phase's rating anchor) against Tre Fiori
# (its weakest club) gives an unambiguous favourite for the directional assertions.
_H, _A = "lyon", "tre_fiori"


def _sm():
    from prediction_market_soccer.tests.clubctx import ucl_strength
    return ucl_strength()


def test_advance_probs_sum_to_one_every_state():
    from prediction_market_soccer.model.inplay_advance import live_advance_from_strength
    sm = _sm()
    states = [
        dict(minute=1, home_goals=0, away_goals=0, period="reg"),
        dict(minute=60, home_goals=1, away_goals=0, period="reg"),
        dict(minute=88, home_goals=1, away_goals=1, period="reg"),
        dict(minute=105, home_goals=0, away_goals=0, period="et", et_home_goals=0, et_away_goals=0),
        dict(minute=118, home_goals=0, away_goals=0, period="et", et_home_goals=1, et_away_goals=0),
        dict(minute=120, home_goals=0, away_goals=0, period="pens"),
    ]
    for st in states:
        la = live_advance_from_strength(sm, _H, _A, **st)
        assert abs((la.p_home_advance + la.p_away_advance) - 1.0) < 1e-9, st
        assert 0.0 <= la.p_home_advance <= 1.0


def test_leading_increases_advance_prob():
    from prediction_market_soccer.model.inplay_advance import live_advance_from_strength
    sm = _sm()
    base = live_advance_from_strength(sm, _H, _A, 60, 0, 0).p_home_advance
    lead = live_advance_from_strength(sm, _H, _A, 60, 1, 0).p_home_advance
    trail = live_advance_from_strength(sm, _H, _A, 60, 0, 1).p_home_advance
    assert lead > base > trail


def test_fatigue_pushes_more_to_penalties():
    """Higher ET fatigue → fewer ET goals → more level-after-ET → more penalty deciders."""
    from prediction_market_soccer.model.inplay_advance import live_advance_prob
    lam_h = lam_a = 1.4
    lo = live_advance_prob(lam_h, lam_a, 91, 0, 0, period="et", shootout_home=0.5, fatigue_k=0.0)
    hi = live_advance_prob(lam_h, lam_a, 91, 0, 0, period="et", shootout_home=0.5, fatigue_k=0.5)
    assert hi.p_pens_decides > lo.p_pens_decides


def test_golden_goal_toggle_changes_et():
    from prediction_market_soccer.model.inplay_advance import live_advance_prob
    lam_h, lam_a = 1.6, 1.0   # home stronger
    full = live_advance_prob(lam_h, lam_a, 91, 0, 0, period="et", shootout_home=0.5, golden_goal=False)
    gg = live_advance_prob(lam_h, lam_a, 91, 0, 0, period="et", shootout_home=0.5, golden_goal=True)
    # Both valid distributions; golden goal changes the ET resolution path.
    assert abs((gg.p_home_advance + gg.p_away_advance) - 1.0) < 1e-9
    assert gg.p_home_advance != full.p_home_advance


def test_penalties_period_uses_shootout_prob():
    from prediction_market_soccer.model.inplay_advance import live_advance_prob
    la = live_advance_prob(1.4, 1.4, 120, 0, 0, period="pens", shootout_home=0.58)
    assert abs(la.p_home_advance - 0.58) < 1e-9
    assert la.p_pens_decides == 1.0


# ── 2-way hedge (plan 24 §4) ─────────────────────────────────────────────────
def test_hedge_full_locks_equal_payoff():
    from prediction_market_soccer.strategy.inplay_hedge_advance import (
        Position, Quotes, hedge_advance_protection)
    pos = Position(shares=10, entry_c=60.0, side="home")
    q = Quotes.from_probs(0.75, 0.27)            # home_adv 75¢ / away_adv 27¢
    sol = hedge_advance_protection(pos, q, target="full")
    assert abs(sol.b - 10.0) < 1e-9             # one-for-one in 2-way
    r = sol.payoff
    assert abs(r.pnl_home_adv - r.pnl_away_adv) < 1e-9   # equal both outcomes
    assert abs(r.pnl_home_adv - 130.0) < 1e-6           # 1000 − 600 − 270 = 130¢ locked


def test_hedge_break_even_zeroes_downside():
    from prediction_market_soccer.strategy.inplay_hedge_advance import (
        Position, Quotes, hedge_advance_protection)
    pos = Position(shares=10, entry_c=60.0, side="home")
    q = Quotes.from_probs(0.75, 0.27)
    sol = hedge_advance_protection(pos, q, target="break_even")
    assert sol.payoff.pnl_away_adv >= -1e-6     # the hedged (opponent-advances) state ≥ 0
    assert sol.payoff.pnl_home_adv > 0          # still profit if our side advances


def test_hedge_dutch_lock_detects_underround():
    from prediction_market_soccer.strategy.inplay_hedge_advance import Quotes, dutch_lock
    assert dutch_lock(Quotes.from_probs(0.55, 0.40)).tradable      # Σ=95 < 100 → arb
    assert not dutch_lock(Quotes.from_probs(0.75, 0.27)).tradable  # Σ=102 ≥ 100


if __name__ == "__main__":
    # In-memory virtual match: strong vs weak, evolving scoreline → live advance trace.
    from prediction_market_soccer.model.inplay_advance import live_advance_from_strength
    sm = _sm()
    print(f"shootout conversions {_H}/{_A}:", shootout_conversions(sm, _H, _A))
    print(f"detailed shootout P({_H}):", round(shootout_win_prob_detailed(sm, _H, _A), 3))
    print(f"\n=== Virtual tie: {_H} vs {_A} — live P({_H} advances) ===")
    timeline = [
        (10, 0, 0, "reg"), (30, 0, 0, "reg"), (55, 0, 1, "reg"),   # underdog shock lead
        (70, 1, 1, "reg"), (89, 1, 1, "reg"),                      # favourite equalises, level late
        (100, 1, 1, "et"), (118, 1, 1, "et"),                       # into ET, still level
        (120, 1, 1, "pens"),                                        # penalties
    ]
    for mn, gh, ga, period in timeline:
        kw = dict(period=period)
        if period == "et":
            kw.update(et_home_goals=0, et_away_goals=0)
        la = live_advance_from_strength(sm, _H, _A, mn, gh, ga, **kw)
        print(f"  {mn:>3}' {gh}-{ga} [{period:>4}]  P(home adv)={la.p_home_advance:.3f}  "
              f"reg={la.p_reg_decides:.2f} et={la.p_et_decides:.2f} pens={la.p_pens_decides:.2f}")
    print("\nrun pytest for assertions.")
