"""Tests for inplay / calibrate / oos_eval / ensemble (plan 03 §4b/§7/§9, 10 §5.3)."""
from __future__ import annotations

import sqlite3

import numpy as np
import pytest

from prediction_market.ingest import store
from prediction_market.ingest.prior_ingest import load_prior
from prediction_market.model.calibrate import (
    brier_score,
    bootstrap_ci,
    closing_line_value,
    log_loss,
    reliability_curve,
)
from prediction_market.model.ensemble import generate_variants, run_ensemble
from prediction_market.model.inplay import live_from_strength, live_match_prob
from prediction_market.model.strength import build_strength


# ── calibrate ────────────────────────────────────────────────────────────────
def test_brier_and_logloss_perfect_and_worst():
    perfect = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
    assert brier_score(perfect, [0, 1]) == 0.0
    assert log_loss(perfect, [0, 1]) < 1e-6
    # Uniform 3-way Brier = 3 * (1/3 - {1,0,0})^2 pattern = 2/3.
    assert abs(brier_score([[1 / 3, 1 / 3, 1 / 3]], [0]) - (2 / 3)) < 1e-9


def test_reliability_curve_monotone_ish():
    rng = np.random.default_rng(0)
    p = rng.random(2000)
    occ = (rng.random(2000) < p).astype(int)  # perfectly calibrated by construction
    bins = reliability_curve(p, occ, n_bins=5)
    for b in bins:
        assert abs(b.mean_predicted - b.observed_freq) < 0.08


def test_closing_line_value_sign():
    assert closing_line_value(0.40, 0.50, side="yes") == pytest.approx(0.10)
    assert closing_line_value(0.40, 0.50, side="no") == pytest.approx(-0.10)


def test_bootstrap_ci_brackets_mean():
    lo, hi = bootstrap_ci([0.2, 0.3, 0.25, 0.28, 0.22], seed=1)
    assert lo <= 0.25 <= hi


# ── inplay ───────────────────────────────────────────────────────────────────
def test_inplay_draw_time_value_monotone():
    sm = build_strength(load_prior())
    # At 0:0, fair draw probability must rise monotonically toward full time.
    draws = [live_from_strength(sm, "brazil", "morocco", m, 0, 0).fair_draw
             for m in (1, 30, 60, 80, 89)]
    assert all(b >= a - 1e-9 for a, b in zip(draws, draws[1:]))
    assert draws[-1] > 0.8  # near full time, still 0:0 → draw very likely


def test_inplay_probs_sum_and_post_goal():
    lp = live_match_prob(1.6, 1.0, 55, 1, 0)
    assert abs(lp.p_home + lp.p_draw + lp.p_away - 1.0) < 1e-9
    # Leading at 55' → home win prob well above the draw prob.
    assert lp.p_home > lp.p_draw


def test_inplay_fulltime_locks_score():
    lp = live_match_prob(1.5, 1.2, 90, 2, 1)
    assert lp.tau == 0.0
    assert lp.p_home == pytest.approx(1.0)  # 2:1 at 90' → home already won


def test_red_card_reduces_scoring():
    base = live_match_prob(1.5, 1.5, 30, 0, 0)
    reds = live_match_prob(1.5, 1.5, 30, 0, 0, red_away=1)
    # Away red card → home relatively favoured vs symmetric baseline.
    assert reds.p_home > base.p_home


# ── ensemble ─────────────────────────────────────────────────────────────────
def test_generate_variants_count_and_base():
    from prediction_market.config import CONFIG
    variants = generate_variants(CONFIG.model, n_variants=6, seed=1)
    assert len(variants) == 6
    assert variants[0] == CONFIG.model  # base included first


def test_run_ensemble_small():
    prior = load_prior()
    ens = run_ensemble(prior, n_variants=3, n_sims=2000, seed=5)
    assert ens.n_variants == 3
    assert abs(sum(ens.p_champion_mean.values()) - 1.0) < 1e-6
    # Dispersion is non-negative and present for the favourite.
    assert all(s >= 0 for s in ens.p_champion_sigma.values())
    assert ens.p_champion_sigma["brazil"] >= 0


# ── oos_eval (uses an in-memory store with a couple of synthetic results) ─────
def test_oos_eval_on_synthetic_store():
    from prediction_market.model.oos_eval import evaluate
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    store.init_db(conn)
    # Map two API ids to canonical teams, add one finished fixture.
    for api_id, cid in ((900, "spain"), (901, "saudi_arabia")):
        store.upsert(conn, "team_meta",
                     {"api_id": api_id, "group_code": "G", "fifa_rank": None,
                      "canonical_team_id": cid, "updated_at": store.utcnow()}, pk=["api_id"])
    store.upsert(conn, "fixture", {
        "api_id": 5000, "league_id": 1, "season": 2026, "round": "Group Stage - 1",
        "status_short": "FT", "home_api_id": 900, "away_api_id": 901,
        "home_goals": 3, "away_goals": 0, "updated_at": store.utcnow(),
    }, pk=["api_id"])
    rep = evaluate(conn=conn, sm=build_strength(load_prior()))
    assert rep.n_matches == 1
    assert 0.0 <= rep.brier <= 2.0
    assert rep.favourite_hit_rate == 1.0  # Spain heavily favoured and won
