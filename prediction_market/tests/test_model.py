"""Tests for the modeling engine (plan 03)."""
from __future__ import annotations

import numpy as np

from prediction_market.config import CONFIG
from prediction_market.ingest.prior_ingest import load_prior
from prediction_market.model.dixon_coles import (
    both_teams_score,
    knockout_advance_prob,
    over_under,
    score_matrix,
    wdl,
)
from prediction_market.model.golden_boot import simulate_golden_boot
from prediction_market.model.match_pricing import price_match
from prediction_market.model.strength import build_strength, _expected_points
from prediction_market.model.tournament import simulate


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


# ── Strength calibration ─────────────────────────────────────────────────────
def test_strength_calibration_matches_prior_points():
    # The exp-points reverse-fit mechanism (rank_anchor_weight=0) must hit targets.
    from dataclasses import replace
    prior = load_prior()
    sm = build_strength(prior, replace(CONFIG.model, rank_anchor_weight=0.0))
    ep = _expected_points(sm.ratings, prior.draw(), sm.host_ids, sm.cfg)
    errs = [abs(ep[t.team_id] - t.exp_points) for t in prior.teams]
    assert np.median(errs) < 0.2
    assert max(errs) < 0.7


def test_rank_anchor_fixes_strength_ordering():
    # exp_points alone over-rates Brazil (weak group) above France (#1, hard group);
    # the rank-anchor blend must restore France > Brazil (matches FIFA rank + market).
    from dataclasses import replace
    prior = load_prior()
    exp_only = build_strength(prior, replace(CONFIG.model, rank_anchor_weight=0.0))
    blended = build_strength(prior)  # default rank_anchor_weight
    assert exp_only.ratings["brazil"] > exp_only.ratings["france"]   # the bug
    assert blended.ratings["france"] > blended.ratings["brazil"]     # fixed


# ── Tournament sim ───────────────────────────────────────────────────────────
def test_tournament_probabilities_valid():
    prior = load_prior()
    sm = build_strength(prior)
    res = simulate(prior, sm, n_sims=3000, seed=7)
    # Champion probabilities form a distribution over 48 teams.
    assert abs(sum(res.p_champion.values()) - 1.0) < 1e-6
    # Exactly 32 teams advance each sim → mean total advance == 32/... per team sum.
    assert abs(sum(res.p_advance.values()) - 32.0) < 0.2
    # Every probability ordering is sane: champion <= final <= sf <= advance.
    for t in res.team_ids:
        assert res.p_champion[t] <= res.p_final[t] + 1e-9
        assert res.p_final[t] <= res.p_sf[t] + 1e-9
        assert res.p_sf[t] <= res.p_advance[t] + 1e-9
        assert 3.0 <= res.e_matches[t] <= 7.0


def test_tournament_tracks_prior_advance_direction():
    prior = load_prior()
    sm = build_strength(prior)
    res = simulate(prior, sm, n_sims=4000, seed=11)
    # Strong prior favourites should have high model advance probability.
    assert res.p_advance["brazil"] > 0.85
    assert res.p_advance["spain"] > 0.85
    # Weakest teams should rarely advance.
    assert res.p_advance["curacao"] < 0.25


# ── Golden boot ──────────────────────────────────────────────────────────────
def test_golden_boot_distribution():
    prior = load_prior()
    sm = build_strength(prior)
    res = simulate(prior, sm, n_sims=4000, seed=3)
    gb = simulate_golden_boot(res, seed=4)
    # Some boot probability is captured by long-tail "field"; modelled players
    # sum to <= 1 and each is a valid probability.
    total = sum(gb.p_golden_boot.values())
    assert 0.0 < total <= 1.0 + 1e-9
    assert all(0.0 <= v <= 1.0 for v in gb.p_golden_boot.values())


# ── Match pricing ────────────────────────────────────────────────────────────
def test_strength_update_nudges_by_performance():
    from prediction_market.model.strength import update_with_results
    sm = build_strength(load_prior())
    r0_spain, r0_saudi = sm.ratings["spain"], sm.ratings["saudi_arabia"]
    # Big upset: Saudi Arabia beats Spain 3-0 → Saudi up, Spain down.
    upd = update_with_results(sm, [{
        "home_id": "saudi_arabia", "away_id": "spain",
        "home_goals": 3, "away_goals": 0, "days_ago": 0}], lr=0.1)
    assert upd.ratings["saudi_arabia"] > r0_saudi
    assert upd.ratings["spain"] < r0_spain
    # No results → ratings unchanged.
    assert update_with_results(sm, [], lr=0.1).ratings["spain"] == r0_spain


def test_price_match_wdl_sums_to_one():
    sm = build_strength(load_prior())
    mp = price_match(sm, "spain", "saudi_arabia")
    assert abs(mp.p_home + mp.p_draw + mp.p_away - 1.0) < 1e-9
    assert mp.p_home > mp.p_away  # Spain strongly favoured
    ko = price_match(sm, "spain", "saudi_arabia", knockout=True)
    assert ko.p_home_advance is not None and 0.5 < ko.p_home_advance < 1.0


def test_club_aggregation_and_blend():
    import sqlite3
    from prediction_market.ingest import store
    from prediction_market.model.club_aggregation import squad_attack_quality, blend_into_strength
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row; store.init_db(c)
    for api_id, cid in ((10, "spain"), (20, "saudi_arabia")):
        store.upsert(c, "team_meta", {"api_id": api_id, "group_code": "G", "fifa_rank": None,
                     "canonical_team_id": cid, "updated_at": store.utcnow()}, pk=["api_id"])
    # Spain players score a lot per 90; Saudi players little.
    store.upsert(c, "player_stat", {"player_api_id": 1, "league_id": 1, "season": 2026,
        "team_api_id": 10, "appearances": 1, "minutes": 90, "goals": 3, "assists": 1}, pk=["player_api_id", "league_id", "season"])
    store.upsert(c, "player_stat", {"player_api_id": 2, "league_id": 1, "season": 2026,
        "team_api_id": 20, "appearances": 1, "minutes": 90, "goals": 0, "assists": 0}, pk=["player_api_id", "league_id", "season"])
    q = squad_attack_quality(c, season=2026)
    assert q["spain"] > q["saudi_arabia"]
    sm = build_strength(load_prior())
    blended = blend_into_strength(sm, q, weight=0.1)
    assert blended.ratings["spain"] > sm.ratings["spain"]          # high output → nudged up
    assert blended.ratings["saudi_arabia"] < sm.ratings["saudi_arabia"]


def test_r3_intensity_incentives():
    from prediction_market.model.tournament import r3_intensity
    cfg = CONFIG.model
    pts = np.array([6, 0, 3, 4])   # clinched, winless, mid, mid
    inten = r3_intensity(pts, cfg)
    assert inten[0] == cfg.r3_rotation_intensity      # clinched → rotate (<1)
    assert inten[1] == cfg.r3_desperation_intensity   # winless → push (>1)
    assert inten[2] == 1.0 and inten[3] == 1.0
    assert inten[0] < 1.0 < inten[1]


def test_r3_incentive_flag_runs_both_ways():
    from dataclasses import replace
    from prediction_market.model.tournament import simulate
    prior = load_prior()
    sm = build_strength(prior)
    sm_off = build_strength(prior, replace(CONFIG.model, r3_incentives=False))
    a = simulate(prior, sm, n_sims=2000, seed=1)
    b = simulate(prior, sm_off, n_sims=2000, seed=1)
    assert abs(sum(a.p_champion.values()) - 1.0) < 1e-6
    assert abs(sum(b.p_champion.values()) - 1.0) < 1e-6
