"""Tests for inplay / calibrate / oos_eval / the season Monte-Carlo (plan 03 §4b/§7/§9).

The WC ensemble (perturbed variants through the 48-team tournament sim) is gone: a
club season carries its uncertainty INSIDE one sim via ``season_rating_sigma``
(TRANSFORM_PLAN C4), so the dispersion tests live on ``league_season`` instead.
"""
from __future__ import annotations

import numpy as np
import pytest

from prediction_market_soccer.model.calibrate import (
    brier_score,
    bootstrap_ci,
    closing_line_value,
    log_loss,
    reliability_curve,
)
from prediction_market_soccer.model.inplay import live_from_strength, live_match_prob
from prediction_market_soccer.tests import clubctx


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
    sm = clubctx.epl_strength()
    # At 0:0, fair draw probability must rise monotonically toward full time.
    draws = [live_from_strength(sm, clubctx.BRIGHTON[1], clubctx.BRENTFORD[1], m, 0, 0).fair_draw
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


# ── season Monte-Carlo (the club replacement for the WC ensemble) ─────────────
def test_season_sim_is_a_distribution_over_the_league():
    """One full remaining EPL calendar → champion/relegation masses that conserve the
    league's cardinality, and a favourite that leads without being a certainty."""
    from prediction_market_soccer.model.league_season import simulate_season
    c = clubctx.epl_season_db()
    sim = simulate_season(c, "epl", clubctx.epl_strength(), n_sims=3000, seed=5)
    # (masses are published rounded to 5dp, hence the 1e-4 slack)
    assert abs(sum(sim.p_champion.values()) - 1.0) < 1e-4
    assert abs(sum(sim.p_top_n.values()) - 4.0) < 1e-4            # EPL trades a top-4 cut
    assert abs(sum(sim.p_relegation.values()) - 3.0) < 1e-4       # 3 direct drops, no playoff
    assert sim.n_remaining == 380 and len(sim.club_ids) == 20
    assert max(sim.p_champion, key=sim.p_champion.get) == "arsenal"   # the anchor favourite
    assert all(0.0 <= v <= 1.0 for v in sim.p_champion.values())


def test_season_rating_sigma_widens_the_title_race():
    """The per-path season rating offset is what stops a per-match edge compounding
    over 38 rounds into false certainty (Bayern 99.99% against an 87c market): with
    sigma off the favourite runs away, with sigma on the race stays credible."""
    from dataclasses import replace

    from prediction_market_soccer.config import CONFIG
    from prediction_market_soccer.model.league_season import simulate_season
    c = clubctx.epl_season_db()
    sm = clubctx.epl_strength()
    sharp = simulate_season(c, "epl", sm, n_sims=3000, seed=5,
                            cfg=replace(CONFIG.model, season_rating_sigma=0.0))
    wide = simulate_season(c, "epl", sm, n_sims=3000, seed=5,
                           cfg=replace(CONFIG.model, season_rating_sigma=0.9))
    assert max(wide.p_champion.values()) < max(sharp.p_champion.values())
    assert abs(sum(wide.p_champion.values()) - 1.0) < 1e-4


# ── oos_eval (uses an in-memory store with a couple of synthetic results) ─────
def test_oos_eval_on_synthetic_store():
    from prediction_market_soccer.model.oos_eval import evaluate
    conn = clubctx.mem_db()
    clubctx.seed_teams(conn, clubctx.ARSENAL, clubctx.IPSWICH)
    clubctx.seed_fixture(conn, 5000, clubctx.ARSENAL, clubctx.IPSWICH, hg=3, ag=0, days_ago=4)
    # An explicit sm keeps the eval off the on-disk ratings cache (which a test must
    # neither read nor write).
    rep = evaluate(conn=conn, sm=clubctx.epl_strength())
    assert rep.n_matches == 1
    assert 0.0 <= rep.brier <= 2.0
    assert rep.favourite_hit_rate == 1.0  # Arsenal heavily favoured and won
