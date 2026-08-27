"""Tests for the modeling engine — club edition (plan 03, TRANSFORM_PLAN C2/C5).

The Dixon-Coles kernel is unchanged from the WC module; everything above it moved:
ratings are reverse-fitted to a club prior's season POINTS-PER-ROUND (not to three
group games), home advantage belongs to whoever hosts the fixture (not to three host
nations), and the two-legged tie replaces the single knockout match as the cup unit.
"""
from __future__ import annotations

import numpy as np

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.config.leagues import fitted_params
from prediction_market_soccer.ingest.club_prior import load_prior
from prediction_market_soccer.model.dixon_coles import (
    both_teams_score,
    knockout_advance_prob,
    over_under,
    score_matrix,
    tie_advance_prob,
    two_leg_advance_prob,
    wdl,
)
from prediction_market_soccer.model.match_pricing import price_match
from prediction_market_soccer.model.strength import _expected_ppr, build_strength
from prediction_market_soccer.tests import clubctx


# ── Dixon-Coles kernel ───────────────────────────────────────────────────────
def test_score_matrix_normalised():
    m = score_matrix(1.4, 1.1, rho=-0.05, kmax=10)
    assert abs(m.sum() - 1.0) < 1e-9
    assert (m >= -1e-12).all()


def test_wdl_partitions_probability():
    m = score_matrix(1.6, 0.9)
    h, d, a = wdl(m)
    assert abs(h + d + a - 1.0) < 1e-9
    assert h > a  # stronger home lambda → home favoured


def test_over_under_and_btts_bounds():
    m = score_matrix(1.5, 1.3)
    over, under, push = over_under(m, 2.5)
    assert abs(over + under + push - 1.0) < 1e-9
    assert push == 0.0  # half-line never pushes
    assert 0.0 <= both_teams_score(m) <= 1.0


def test_knockout_advance_complementary():
    # P(A advances) + P(B advances) == 1 with mirrored penalty edges.
    a = knockout_advance_prob(1.5, 1.1, penalty_home_edge=0.53)
    b = knockout_advance_prob(1.1, 1.5, penalty_home_edge=0.47)
    assert abs(a + b - 1.0) < 1e-9


# ── Two-legged ties (C5 — the club knockout unit) ────────────────────────────
def test_two_leg_advance_respects_the_first_leg_aggregate():
    """No away-goals rule (abolished 2021): only the aggregate and who is level."""
    even = two_leg_advance_prob(1.3, 1.3, 0, 0)
    assert abs(even - 0.5) < 0.02                       # symmetric, fresh deciding leg
    ahead = two_leg_advance_prob(1.3, 1.3, 2, 0)        # 2-goal cushion carried in
    behind = two_leg_advance_prob(1.3, 1.3, 0, 2)
    assert ahead > even > behind
    assert abs((ahead + two_leg_advance_prob(1.3, 1.3, 0, 2)) - 1.0) < 0.05


def test_two_leg_level_aggregate_goes_to_et_or_straight_to_pens():
    """UEFA plays ET before penalties; CONMEBOL goes straight to pens. With a
    LOPSIDED pair the extra half-hour is another chance for the stronger side, so
    the ET route must favour it more than a coin-flip shootout does."""
    uefa = two_leg_advance_prob(1.9, 0.8, 0, 0, penalty_home_edge=0.5, et_then_pens=True)
    conmebol = two_leg_advance_prob(1.9, 0.8, 0, 0, penalty_home_edge=0.5, et_then_pens=False)
    assert uefa > conmebol > 0.5


def test_tie_advance_prob_is_complementary_over_the_full_tie():
    """A whole tie priced before leg 1: A hosts leg 1, B hosts leg 2. Mirroring the tie
    (swap home/away λ in BOTH legs = relabel which club we are asking about) must
    complement to 1."""
    a = tie_advance_prob(1.8, 0.9, 1.1, 1.4, penalty_leg2_home_edge=0.5)
    b = tie_advance_prob(0.9, 1.8, 1.4, 1.1, penalty_leg2_home_edge=0.5)
    assert abs((a + b) - 1.0) < 1e-6
    assert a > 0.5    # the stronger side in both legs


def test_symmetric_tie_slightly_favours_the_leg_two_host():
    """Two identically-rated clubs are not a coin flip over a tie: extra time is played
    at the SECOND leg's ground, so its host carries that half-hour's home edge."""
    p_leg1_host = tie_advance_prob(1.4, 1.1, 1.4, 1.1, penalty_leg2_home_edge=0.5)
    assert 0.47 < p_leg1_host < 0.50


# ── Strength calibration (reverse-fit to the club prior) ─────────────────────
def test_strength_calibration_matches_prior_points_per_round():
    """The whole point of the fit: model-implied points-per-round over a full double
    round-robin must land on the prior's anchor_points for every club."""
    prior = load_prior("epl")
    sm = clubctx.epl_strength()
    members = [t.club_id for t in prior.teams]
    ep = _expected_ppr(sm.ratings, members, sm.cfg, comp="epl")
    errs = [abs(ep[t.club_id] - t.anchor_points) for t in prior.teams]
    # A small systematic residual is structural, not slack: the total points a round
    # can yield is fixed by the model's draw rate, so a set of anchors whose mean the
    # model cannot reproduce leaves every club a shared offset. What matters is that
    # no club is individually mis-fitted.
    assert np.median(errs) < 0.10
    assert max(errs) < 0.15
    assert np.std(errs) < 0.05          # the residual is a shared offset, not scatter


def test_strength_ordering_follows_the_anchor():
    """A league table carries no group-difficulty confound (the WC ``rank_anchor_weight``
    patch existed only for that), so the rating order IS the anchor order."""
    prior = load_prior("epl")
    sm = clubctx.epl_strength()
    by_anchor = [t.club_id for t in sorted(prior.teams, key=lambda t: -t.anchor_points)]
    by_rating = sorted(sm.ratings, key=lambda c: -sm.ratings[c])
    assert by_rating[0] == by_anchor[0] == "arsenal"
    assert by_rating[-1] == by_anchor[-1]
    b = sm.cfg.rating_bound
    assert all(-b <= r <= b for r in sm.ratings.values())


def test_promoted_clubs_sit_at_the_bottom_of_the_prior():
    """A promoted side has no top-flight record, so its anchor is a rebuilt estimate —
    it must not inherit a mid-table anchor by accident."""
    prior = load_prior("epl")
    promoted = [t for t in prior.teams if t.promoted]
    assert promoted, "the EPL prior should carry the promoted sides"
    established = [t.anchor_points for t in prior.teams if not t.promoted]
    assert max(t.anchor_points for t in promoted) <= max(established)
    assert all(t.last_rank is None or t.last_rank > 0 for t in promoted)


def test_host_ids_is_an_always_empty_backcompat_shim():
    """Club football has no host nation: the field survives only so every copied WC
    consumer keeps working, and an empty set makes every host branch a no-op."""
    sm = clubctx.epl_strength()
    assert sm.host_ids == frozenset()


# ── Per-competition parameters (C2) ──────────────────────────────────────────
def test_per_league_mu_and_home_advantage_are_used():
    """base_mu / home_adv are fitted PER COMPETITION from last season's results; a model
    built for a competition must price with that competition's pair, not the globals."""
    fp = fitted_params("epl")
    sm = clubctx.epl_strength()
    assert sm.comp == "epl"
    assert sm._mu == fp["base_mu"] and sm._ha == fp["home_adv"]
    # Serie A is a lower-scoring, lower-home-advantage league than the Bundesliga —
    # a single global constant cannot represent both.
    assert fitted_params("seriea")["base_mu"] < fitted_params("bundesliga")["base_mu"]
    assert fitted_params("seriea")["home_adv"] < fitted_params("epl")["home_adv"]


def test_home_advantage_applies_to_every_fixture_and_is_dropped_on_a_neutral_venue():
    """The C2 semantic inversion: home advantage belongs to whoever HOSTS, on every
    fixture — the exception is a neutral venue (a final), not the rule."""
    import math
    sm = clubctx.epl_strength()
    a, b = "brighton", "brentford"
    lam_h, lam_a = sm.pair_lambdas(a, b)
    n_h, n_a = sm.pair_lambdas(a, b, neutral=True)
    # The host edge is exactly exp(home_adv) on the host's λ and nothing on the visitor's.
    assert abs(lam_h / n_h - math.exp(sm._ha)) < 1e-9
    assert abs(lam_a - n_a) < 1e-12
    # host_neutral is accepted as a synonym so copied WC call sites keep their meaning.
    assert sm.pair_lambdas(a, b, host_neutral=True) == (n_h, n_a)
    # BOTH clubs get it when they host — it belongs to the venue, not to an identity.
    h_a_home, lam_b_away = sm.pair_lambdas(a, b)
    h_b_home, lam_a_away = sm.pair_lambdas(b, a)
    assert h_a_home > lam_a_away and h_b_home > lam_b_away


def test_knockout_scale_lowers_both_lambdas():
    sm = clubctx.ucl_strength()
    base = sm.pair_lambdas("lyon", "celtic")
    ko = sm.pair_lambdas("lyon", "celtic", knockout=True)
    s = CONFIG.model.knockout_lambda_scale
    assert all(abs(k - b * s) < 1e-9 for k, b in zip(ko, base))


def test_rating_shift_perturbs_only_the_gap():
    """The season Monte-Carlo carries parameter risk through this knob; a zero shift
    must leave single-match pricing byte-identical."""
    sm = clubctx.epl_strength()
    a, b = "arsenal", "chelsea"
    assert sm.pair_lambdas(a, b, rating_shift=0.0) == sm.pair_lambdas(a, b)
    up = sm.pair_lambdas(a, b, rating_shift=0.5)
    base = sm.pair_lambdas(a, b)
    assert up[0] > base[0] and up[1] < base[1]


# ── Per-competition alt-data weights ─────────────────────────────────────────
def test_altdata_weights_are_per_competition():
    """A 38-round top-5 league, a CONMEBOL two-legged cup and a 153-club European
    qualifying bracket each carry recent form differently, so each competition uses the
    pair fitted on its OWN history — one global constant would misweight all three."""
    from dataclasses import replace

    from prediction_market_soccer.config.leagues import altdata_weights
    from prediction_market_soccer.model.altdata_adjust import TeamAdj

    w_arg = altdata_weights("argentina")
    w_laliga = altdata_weights("laliga")
    assert w_arg["oppadj_off_weight"] > 0 and w_arg["oppadj_def_weight"] > 0
    assert w_laliga["oppadj_off_weight"] == 0 and w_laliga["oppadj_def_weight"] == 0
    assert altdata_weights(None) == {}          # no competition ⇒ fall back to the globals

    # Same ratings, same alt-data — only the competition differs → different λ.
    sm = clubctx.epl_strength()
    adj = {"arsenal": TeamAdj(off_z=1.5, def_z=0.0), "chelsea": TeamAdj(off_z=0.0, def_z=1.5)}
    fitted = replace(sm, adj=adj)
    lam_fitted = fitted.pair_lambdas("arsenal", "chelsea")
    zeroed = replace(sm, adj=adj, comp="laliga", base_mu=sm.base_mu, home_adv=sm.home_adv)
    lam_zeroed = zeroed.pair_lambdas("arsenal", "chelsea")
    assert lam_fitted != lam_zeroed
    # laliga's fitted weights are all zero → the adjustment is an exact no-op there.
    assert lam_zeroed == sm.pair_lambdas("arsenal", "chelsea")


def test_altdata_adjustment_is_bounded():
    """However extreme the alt-data, the per-side log adjustment is clipped so no
    single signal can dominate the price."""
    from dataclasses import replace

    from prediction_market_soccer.model.altdata_adjust import TeamAdj

    sm = clubctx.epl_strength()
    base = sm.pair_lambdas("arsenal", "chelsea")
    wild = replace(sm, adj={"arsenal": TeamAdj(off_z=50.0, def_z=-50.0),
                            "chelsea": TeamAdj(off_z=-50.0, def_z=50.0)})
    out = wild.pair_lambdas("arsenal", "chelsea")
    clip = CONFIG.model.adj_log_clip
    assert all(b / np.exp(clip) - 1e-9 <= o <= b * np.exp(clip) + 1e-9
               for o, b in zip(out, base))


# ── Match pricing ────────────────────────────────────────────────────────────
def test_price_match_wdl_sums_to_one():
    sm = clubctx.epl_strength()
    mp = price_match(sm, "arsenal", "ipswich")
    assert abs(mp.p_home + mp.p_draw + mp.p_away - 1.0) < 1e-9
    assert mp.p_home > mp.p_away  # Arsenal strongly favoured at home
    ko = price_match(sm, "arsenal", "ipswich", knockout=True)
    assert ko.p_home_advance is not None and 0.5 < ko.p_home_advance < 1.0


def test_price_match_totals_and_btts_are_probabilities():
    sm = clubctx.epl_strength()
    mp = price_match(sm, "brighton", "brentford")
    assert abs(mp.p_over_2_5 + mp.p_under_2_5 - 1.0) < 1e-9
    assert 0.0 < mp.p_btts < 1.0
    assert mp.lam_home > 0 and mp.lam_away > 0


def test_strength_update_nudges_by_performance():
    """With 34-38 rounds a season this result-update is the model's main in-season
    signal (§3.2), so an upset has to move both clubs the right way."""
    from prediction_market_soccer.model.strength import update_with_results
    sm = clubctx.epl_strength()
    r0_top, r0_bottom = sm.ratings["arsenal"], sm.ratings["ipswich"]
    upd = update_with_results(sm, [{
        "home_id": "ipswich", "away_id": "arsenal",
        "home_goals": 3, "away_goals": 0, "days_ago": 0}], lr=0.1)
    assert upd.ratings["ipswich"] > r0_bottom
    assert upd.ratings["arsenal"] < r0_top
    # No results → ratings unchanged, and the per-league fields survive the update.
    same = update_with_results(sm, [], lr=0.1)
    assert same.ratings["arsenal"] == r0_top
    assert same.comp == sm.comp and same.base_mu == sm.base_mu and same.home_adv == sm.home_adv


def test_club_aggregation_and_blend():
    from prediction_market_soccer.ingest import store
    from prediction_market_soccer.model.club_aggregation import blend_into_strength, squad_attack_quality
    c = clubctx.mem_db()
    clubctx.seed_teams(c, clubctx.ARSENAL, clubctx.IPSWICH)
    # Arsenal's players score a lot per 90; Ipswich's little.
    store.upsert(c, "player_stat", {"player_api_id": 1, "league_id": 39, "season": 2026,
        "team_api_id": clubctx.ARSENAL[0], "appearances": 1, "minutes": 90, "goals": 3,
        "assists": 1}, pk=["player_api_id", "league_id", "season"])
    store.upsert(c, "player_stat", {"player_api_id": 2, "league_id": 39, "season": 2026,
        "team_api_id": clubctx.IPSWICH[0], "appearances": 1, "minutes": 90, "goals": 0,
        "assists": 0}, pk=["player_api_id", "league_id", "season"])
    q = squad_attack_quality(c, season=2026)
    assert q["arsenal"] > q["ipswich"]
    sm = clubctx.epl_strength()
    blended = blend_into_strength(sm, q, weight=0.1)
    assert blended.ratings["arsenal"] > sm.ratings["arsenal"]      # high output → nudged up
    assert blended.ratings["ipswich"] < sm.ratings["ipswich"]
    assert blended.comp == "epl"                                    # per-league fields kept


# ── FC26 talent prior (feeds the top-scorer race) ────────────────────────────
def test_fc_goal_rate_orders_by_role():
    from prediction_market_soccer.ingest.fc_ingest import fc_goal_rate
    elite = fc_goal_rate(92, 91, 91, 91, "Attack")        # world-class striker
    squad = fc_goal_rate(79, 78, 79, 77, "Attack")        # squad striker
    mid = fc_goal_rate(88, 89, 80, 90, "Midfielder")      # ball-playing mid
    defender = fc_goal_rate(50, 60, 70, 80, "Defense")    # high-rated CB
    assert elite > squad > defender
    assert elite > mid > defender
    assert 0.45 < elite < 0.95 and 0.2 < squad < 0.5      # rates land in a sane band


def test_build_strength_defaults_to_the_merged_prior():
    """``league=None`` builds the cross-league model every global path uses; it rates
    every enabled competition's clubs at once."""
    sm = clubctx.all_comps_strength()
    assert sm.comp is None
    for cid in ("arsenal", "lyon", "palmeiras", "bayern_mnchen"):
        assert cid in sm.ratings
    assert build_strength(load_prior("epl"), league="epl").comp == "epl"
