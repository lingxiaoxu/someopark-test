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


def test_fc_goal_rate_orders_by_role():
    from prediction_market.ingest.fc_ingest import fc_goal_rate
    elite = fc_goal_rate(92, 91, 91, 91, "Attack")        # Mbappé-class striker
    squad = fc_goal_rate(79, 78, 79, 77, "Attack")        # Balogun-class squad striker
    mid = fc_goal_rate(88, 89, 80, 90, "Midfielder")      # ball-playing mid
    defender = fc_goal_rate(50, 60, 70, 80, "Defense")    # high-rated CB
    assert elite > squad > defender
    assert elite > mid > defender
    assert 0.45 < elite < 0.95 and 0.2 < squad < 0.5      # rates land in a sane band


def test_teammate_competition_discounts_shared_attack():
    """A lone spearhead keeps his rate; co-stars who split a team's goals are
    discounted (France's Mbappé/Dembélé/Olise; 2002-Brazil effect)."""
    from prediction_market.model.golden_boot import Player, apply_teammate_competition
    lone = Player("a", "Lone", "norway", mu_goals_per_match=0.70, start_prob=0.9, pen_taker=False)
    # three equal co-stars on one team
    co = [Player(f"f{i}", f"Co{i}", "france", mu_goals_per_match=0.60, start_prob=0.9,
                 pen_taker=False) for i in range(3)]
    out = {p.player_id: p for p in apply_teammate_competition([lone] + co, kappa=0.35)}
    # lone star: share≈1 → essentially no discount
    assert out["a"].mu_goals_per_match > 0.69
    # each co-star: share≈1/3 → discounted below original 0.60
    assert all(out[f"f{i}"].mu_goals_per_match < 0.60 for i in range(3))
    # but bounded — never cut by more than kappa
    assert all(out[f"f{i}"].mu_goals_per_match > 0.60 * (1 - 0.35) for i in range(3))
    # kappa=0 disables it
    same = apply_teammate_competition([lone] + co, kappa=0.0)
    assert same[1].mu_goals_per_match == 0.60


def test_golden_boot_rate_regresses_burst_to_talent():
    """A 1-game 2-goal burst from a squad striker must regress toward his FC talent
    rate (Balogun bug), not inflate near 2.0 — the boot is talent x knockout depth."""
    import sqlite3
    from prediction_market.ingest import store
    from prediction_market.model.golden_boot import build_golden_boot_players, games_played_by_team
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row; store.init_db(c)
    store.upsert(c, "team_meta", {"api_id": 30, "group_code": "A", "fifa_rank": None,
                 "canonical_team_id": "united_states", "updated_at": store.utcnow()}, pk=["api_id"])
    # FC talent prior: a 0.33-rate squad striker.
    c.execute("INSERT INTO fc_player (fc_id, name, canonical_team_id, position_type, overall, "
              "finishing, positioning, shot_power, penalties, sho, pen_taker, goal_rate, "
              "team_attack_rank, source, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (1, "F. Balogun", "united_states", "Attack", 77, 79, 78, 79, 71, 77, 0, 0.33, 1,
               "ea_fc26", store.utcnow()))
    # WC-to-date: 2 goals in 1 appearance.
    store.upsert(c, "player", {"api_id": 1, "name": "F. Balogun", "position": "Attacker"}, pk=["api_id"])
    store.upsert(c, "player_stat", {"player_api_id": 1, "league_id": 1, "season": CONFIG.soccer.season,
                 "team_api_id": 30, "appearances": 1, "goals": 2}, pk=["player_api_id", "league_id", "season"])
    players = build_golden_boot_players(c)
    bal = next(p for p in players if "Balogun" in p.name)
    assert bal.goals_so_far == 2                       # real goals carried as head start
    assert 0.33 < bal.mu_goals_per_match < 0.65        # posterior regressed toward talent, not ~2.0
    # games-played correction: USA has played 1 settled match (vs a distinct opponent).
    store.upsert(c, "team_meta", {"api_id": 31, "group_code": "A", "fifa_rank": None,
                 "canonical_team_id": "england", "updated_at": store.utcnow()}, pk=["api_id"])
    store.upsert(c, "fixture", {"api_id": 100, "home_api_id": 30, "away_api_id": 31,
                 "status_short": "FT", "home_goals": 2, "away_goals": 1}, pk=["api_id"])
    assert games_played_by_team(c).get("united_states") == 1


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
