"""Unit tests for the 8 data-mined in-play tactics (26-match intra-game study).

Each test is anchored to a REAL match from the study so the thresholds stay tied
to evidence, and includes a negative case so the signal doesn't fire on noise.
"""
from prediction_market.model.inplay import LiveMatchProb
from prediction_market.strategy.inplay_tactics import (
    dormant_explosion,
    finishing_uplift_over,
    formation_fragility,
    late_goal_bias,
    live_odds_crossval,
    lone_threat_removed,
    possession_trap_fade,
    xg_dominance_chase,
)


def _lp(minute, gh, ga, *, exp_remaining=1.0, p_over=None, tau=None):
    """Build a LiveMatchProb fixture with only the fields the tactics read."""
    tau = (90 - minute) / 90 if tau is None else tau
    return LiveMatchProb(
        minute=minute, home_goals=gh, away_goals=ga, tau=tau,
        p_home=0.4, p_draw=0.3, p_away=0.3, fair_draw=0.3,
        p_over_total=p_over or {2.5: 0.45}, exp_remaining_goals=exp_remaining,
        lam_home_eff=0.5, lam_away_eff=0.5,
    )


# 1. dormant_explosion — Switzerland-Bosnia: 0:0 quiet at HT but chances building → OVER.
def test_dormant_explosion_fires():
    a = dormant_explosion(_lp(50, 0, 0, exp_remaining=1.2), combined_xg=1.4)
    assert a.act == "BUY" and a.side == "over"


def test_dormant_explosion_skips_sterile():
    # Ghana-Panama: goalless but NO chances (combined xG 0.2) → must not fire.
    a = dormant_explosion(_lp(50, 0, 0, exp_remaining=0.6), combined_xg=0.2)
    assert a.act == "HOLD"


def test_dormant_explosion_skips_too_late():
    a = dormant_explosion(_lp(80, 0, 0, exp_remaining=1.2), combined_xg=1.5)
    assert a.act == "HOLD"


# 2. xg_dominance_chase — Switzerland 3.2 xG vs Qatar 0.6, level 1:1 → back Switzerland.
def test_xg_chase_level():
    a = xg_dominance_chase(xg_for=3.2, xg_against=0.6, goals_for=1, goals_against=1,
                           minute=70, side="home")
    assert a.act == "BUY" and a.side == "home" and a.urgency == "high"


def test_xg_chase_skips_when_leading():
    a = xg_dominance_chase(xg_for=2.5, xg_against=0.5, goals_for=2, goals_against=0,
                           minute=60, side="home")
    assert a.act == "HOLD"


def test_xg_chase_skips_small_edge():
    a = xg_dominance_chase(xg_for=1.1, xg_against=0.9, goals_for=0, goals_against=0,
                           minute=60, side="home")
    assert a.act == "HOLD"


# 3. possession_trap_fade — Panama 64% possession, 0.13 xG, losing → fade Panama.
def test_possession_trap_fires():
    a = possession_trap_fade(possession=0.64, xg_for=0.13, goals_for=0, goals_against=1,
                             minute=60, side="away")
    assert a.act == "BUY" and a.side == "home"


def test_possession_trap_skips_when_creating():
    # Türkiye had 72% possession but 1.36 xG — they DID create; not a sterile trap.
    a = possession_trap_fade(possession=0.72, xg_for=1.36, goals_for=0, goals_against=2,
                             minute=60, side="away")
    assert a.act == "HOLD"


# 4. formation_fragility — one side in 5-3-2 (concedes 3.5/g) → lean OVER.
def test_formation_fragility_fires():
    a = formation_fragility(_lp(40, 0, 0, exp_remaining=1.5),
                            home_formation="4-2-3-1", away_formation="5-3-2")
    assert a.act == "BUY" and a.side == "over"


def test_formation_fragility_skips_both_solid():
    a = formation_fragility(_lp(40, 0, 0, exp_remaining=1.5),
                            home_formation="4-2-3-1", away_formation="4-3-3")
    assert a.act == "HOLD"


# 5. lone_threat_removed — Argentina's Messi (60% of shots) subbed off → fade Argentina.
def test_lone_threat_fires():
    a = lone_threat_removed(side="home", lone_player="Lionel Messi", shot_share=0.60,
                            removed=True, minute=75, exp_remaining_goals=0.9)
    assert a.act == "BUY" and a.side == "away"


def test_lone_threat_skips_when_on_pitch():
    a = lone_threat_removed(side="home", lone_player="Lionel Messi", shot_share=0.60,
                            removed=False, minute=75, exp_remaining_goals=0.9)
    assert a.act == "HOLD"


# 6. late_goal_bias — 80', goals still expected → late OVER value (34% of goals at 75'+).
def test_late_goal_bias_fires():
    a = late_goal_bias(_lp(80, 1, 1, exp_remaining=0.7))
    assert a.act == "BUY" and a.side == "over"


def test_late_goal_bias_holds_under_when_held():
    a = late_goal_bias(_lp(80, 1, 1, exp_remaining=0.7), entry_under=0.6)
    assert a.act == "HOLD" and a.side == "under"


def test_late_goal_bias_skips_early():
    a = late_goal_bias(_lp(55, 0, 0, exp_remaining=1.2))
    assert a.act == "HOLD"


# 7. finishing_uplift_over — WC goals beat xG (+0.87); OVER under-priced vs a low market.
def test_finishing_uplift_vs_market():
    # Open game (2.4 goals still expected): model already prices OVER above a cheap
    # market, and the finishing reinforcement clears take-profit → BUY.
    a = finishing_uplift_over(_lp(55, 0, 0, exp_remaining=2.4, p_over={2.5: 0.45}),
                              market_over_price=0.30)
    assert a.act == "BUY" and a.side == "over"


def test_finishing_uplift_no_market_holds():
    # No OVER/UNDER market to value against → never emits a circular self-comparison.
    a = finishing_uplift_over(_lp(55, 0, 0, exp_remaining=2.4, p_over={2.5: 0.45}))
    assert a.act == "HOLD"


def test_finishing_uplift_needs_model_edge_first():
    # Model's own P(OVER) NOT above market → uplift alone must not create a signal.
    a = finishing_uplift_over(_lp(55, 0, 0, exp_remaining=1.5, p_over={2.5: 0.30}),
                              market_over_price=0.45)
    assert a.act == "HOLD"


def test_finishing_uplift_skips_late():
    a = finishing_uplift_over(_lp(82, 0, 0, exp_remaining=2.0, p_over={2.5: 0.30}),
                              market_over_price=0.20)
    assert a.act == "HOLD"


# 8. live_odds_crossval — sharp book leads model, or venue lags both.
def test_odds_crossval_book_leads():
    a = live_odds_crossval(model_fair=0.45, book_prob=0.55, side="home")
    assert a.act == "BUY" and a.side == "home"


def test_odds_crossval_venue_lags():
    a = live_odds_crossval(model_fair=0.50, book_prob=0.52, side="home", our_venue_price=0.42)
    assert a.act == "BUY" and a.urgency == "high"


def test_odds_crossval_aligned_holds():
    a = live_odds_crossval(model_fair=0.50, book_prob=0.51, side="home")
    assert a.act == "HOLD"


def test_odds_crossval_no_book_holds():
    a = live_odds_crossval(model_fair=0.50, book_prob=None, side="home")
    assert a.act == "HOLD"


# ── model-aware overshoot take-profit (the validated smart-exit) ───────────────
def test_overshoot_take_profit_sells_overreaction():
    import dataclasses

    from prediction_market.strategy.inplay_tactics import model_overshoot_take_profit
    # market bid 0.80 but live model fair only 0.55 → +0.25 over fair → SELL the overshoot
    lp = dataclasses.replace(_lp(60, 1, 0, exp_remaining=1.0), p_home=0.55)
    a = model_overshoot_take_profit("home", 0.80, lp)
    assert a.act == "SELL" and a.side == "home"


def test_overshoot_take_profit_holds_when_fair():
    import dataclasses

    from prediction_market.strategy.inplay_tactics import model_overshoot_take_profit
    # market 0.75 ≈ fair 0.72 (+0.03 < margin 0.12) → HOLD (value not over-reacted)
    lp = dataclasses.replace(_lp(60, 1, 0, exp_remaining=1.0), p_home=0.72)
    a = model_overshoot_take_profit("home", 0.75, lp)
    assert a.act == "HOLD"


# ── totals-market wiring (Kalshi KXWCTOTAL / Poly tsc) into find_opportunities ──
def _live_db_with_totals(gh, ga, minute):
    import sqlite3

    from prediction_market.ingest import store
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row; store.init_db(c)
    for api, cid in ((10, "brazil"), (20, "morocco")):
        store.upsert(c, "team_meta", {"api_id": api, "canonical_team_id": cid,
                                      "updated_at": store.utcnow()}, pk=["api_id"])
    store.upsert(c, "fixture", {"api_id": 1, "league_id": 1, "season": 2026, "status_short": "2H",
        "home_api_id": 10, "away_api_id": 20, "home_goals": gh, "away_goals": ga, "elapsed": minute,
        "round": "Group A", "updated_at": store.utcnow()}, pk=["api_id"])
    for tid, xg in ((10, 1.3), (20, 0.9)):
        store.upsert(c, "fixture_stats", {"fixture_api_id": 1, "team_api_id": tid, "xg": xg,
            "possession": 0.5, "shots_total": 8, "fetched_at": store.utcnow()},
            pk=["fixture_api_id", "team_api_id"])
    return c


def test_totals_relative_value_surfaces():
    from prediction_market.ingest.prior_ingest import load_prior
    from prediction_market.model.strength import build_strength
    from prediction_market.strategy.inplay_arb import find_opportunities
    c = _live_db_with_totals(0, 0, 55)
    sm = build_strength(load_prior())
    # OVER cheap → model (which likes UNDER at 0-0/55') flags a totals relative_value.
    qs = {"kalshi_totals": lambda fid: {"over": {"ask": 0.30, "bid": 0.28},
                                        "under": {"ask": 0.72, "bid": 0.70}}}
    opps = find_opportunities(conn=c, sm=sm, quote_sources=qs)
    rv = [o for o in opps if o["kind"] == "relative_value" and o["side"] in ("over", "under")]
    assert rv, "totals relative_value should surface"
    assert all(o["market"] is not None and o["edge"] is not None for o in rv)


def test_finishing_uplift_activates_with_totals_market():
    from prediction_market.ingest.prior_ingest import load_prior
    from prediction_market.model.strength import build_strength
    from prediction_market.strategy.inplay_arb import find_opportunities
    c = _live_db_with_totals(1, 1, 62)   # 1-1: model P(over 2.5) is high
    sm = build_strength(load_prior())
    qs = {"kalshi_totals": lambda fid: {"over": {"ask": 0.45, "bid": 0.43},
                                        "under": {"ask": 0.57, "bid": 0.55}}}
    opps = find_opportunities(conn=c, sm=sm, quote_sources=qs)
    keys = {o["reason_key"] for o in opps}
    assert "finishing_uplift_mkt" in keys, "signal #9 should activate against a real totals market"
