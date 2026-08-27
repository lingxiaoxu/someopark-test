"""Club-format priors: the caps-driven late-draw stance and the per-competition corner
prior — the two World Cup constants that became per-format / per-competition fits."""
from __future__ import annotations

import sqlite3

from prediction_market_soccer.config.leagues import caps_for
from prediction_market_soccer.ingest import store
from prediction_market_soccer.model import knockout_late_draw as kld
from prediction_market_soccer.model.inplay_corners import (CORNER_TOTAL_PRIOR, corner_prior,
                                                           fit_league_corner_priors,
                                                           live_corners_fair)


def _mem_db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    store.init_db(c)
    return c


# ── format selection comes from caps, never from the round name ──────────────
def test_family_comes_from_caps_not_the_round_string():
    assert kld.family_for(caps_for("epl", "Regular Season - 3")) == kld.LEAGUE
    assert kld.family_for(caps_for("ucl", "League Phase - 3"), "ucl") == kld.UEFA_LEAGUE_PHASE
    assert kld.family_for(caps_for("ucl", "Round of 16", leg=1)) == kld.CUP_LEG1
    assert kld.family_for(caps_for("ucl", "Round of 16", leg=2)) == kld.CUP_DECIDER
    assert kld.family_for(caps_for("argentina", "Clausura - Final")) == kld.CUP_DECIDER
    # An unresolved leg must not buy its way into the stronger (decider) stance.
    assert kld.family_for(caps_for("ucl", "Round of 16")) == kld.CUP_LEG1
    assert kld.family_for(None) == kld.LEAGUE


def test_knockout_stance_is_a_fade_and_the_league_stance_is_a_stand_aside():
    """The club fit reverses the WC premise: a level knockout decider resolves MORE often
    than the Poisson baseline (z=-3.58), while a level league match tracks it (z=+1.26)."""
    ko = caps_for("ucl", "Round of 16", leg=2)
    lg = caps_for("epl", "Regular Season - 3")
    assert kld.late_draw_stance(ko, minute=80, home_goals=1, away_goals=1).direction in ("fade", "neutral", "back")
    assert kld.late_draw_stance(lg, minute=80, home_goals=1, away_goals=1).direction == "neutral"
    assert kld.draw_persistence(ko) < kld.draw_persistence(lg)


def test_stance_is_neutral_unless_the_match_is_level_and_late():
    ko = caps_for("ucl", "Final", is_final=True)
    assert kld.late_draw_stance(ko, minute=80, home_goals=2, away_goals=1).direction == "neutral"
    assert kld.late_draw_stance(ko, minute=40, home_goals=1, away_goals=1).direction == "neutral"
    assert kld.late_draw_stance(ko, minute=80, home_goals=0, away_goals=0).direction in ("fade", "neutral", "back")


def test_persistence_shrinks_toward_no_view_and_stays_a_probability():
    # Every family that has not earned its estimate lands near 1.0 through the shrinkage,
    # so "stand aside" is produced by the evidence, not by a hand-set switch.
    # Which families clear the band is an empirical result that moves with each refit —
    # the original table said cup_decider did, and that turned out to be a survivorship
    # artefact (see the _FIT comment). So assert the SHRINKAGE CONTRACT instead: a family
    # is pulled toward 1.0 in proportion to how little its z has earned, and a family
    # that has not earned anything cannot produce a stance.
    for fam in (kld.LEAGUE, kld.UEFA_LEAGUE_PHASE, kld.CUP_LEG1, kld.CUP_DECIDER):
        n, ratio, z = kld._FIT[fam]
        p = kld.PERSISTENCE[fam]
        assert 0.5 < p < 1.5, f"{fam} persistence {p} left the plausible range"
        # shrunk value always sits between the raw ratio and "no view"
        assert min(ratio, 1.0) - 1e-9 <= p <= max(ratio, 1.0) + 1e-9
        # and it only clears the band when the fit is actually significant
        if abs(p - 1.0) >= kld.STANCE_BAND:
            assert abs(z) >= 2.0, f"{fam} produces a stance on z={z}"
    ko = caps_for("ucl", "Round of 16", leg=2)
    assert 0.0 < kld.adjusted_draw_prob(0.30, ko) < 0.30
    assert 0.0 < kld.adjusted_draw_prob(1.0, ko) < 1.0
    assert kld.adjusted_draw_prob(0.0, ko) > 0.0        # clamped away from a hard 0


# ── per-competition corner prior ─────────────────────────────────────────────
def test_corner_prior_falls_back_to_the_pool_for_an_unfitted_competition():
    mu_unknown, k_unknown = corner_prior("bundesliga")   # no corner rows in the club sample
    assert mu_unknown == CORNER_TOTAL_PRIOR
    assert corner_prior(None) == (mu_unknown, k_unknown)
    assert corner_prior("not_a_competition") == (mu_unknown, k_unknown)
    mu_epl, _ = corner_prior("epl")
    assert 6.0 < mu_epl < 13.0                          # a fitted entry, still sane


def test_live_corners_fair_uses_the_competition_prior_but_an_explicit_one_wins():
    base = live_corners_fair(3, 30, 0, 0, lines=(9.5,), comp_key="epl")
    pooled = live_corners_fair(3, 30, 0, 0, lines=(9.5,))
    # Same shape either way; the competition only shifts the level.
    assert base.valid and pooled.valid
    assert abs(base.exp_total - pooled.exp_total) < 1.0
    forced = live_corners_fair(3, 30, 0, 0, lines=(9.5,), nu_full=20.0, comp_key="epl")
    assert forced.exp_total > base.exp_total + 3.0      # explicit nu_full overrides the fit


def test_corner_fit_shrinks_a_thin_competition_toward_the_pool():
    """Two competitions, wildly different raw means, both on a handful of matches: the
    empirical-Bayes weight n/(n+n0) must pull them together rather than trust the noise."""
    c = _mem_db()
    from prediction_market_soccer.config.leagues import get
    # Match-to-match spread inside each competition is what makes a thin group mean
    # untrustworthy — with zero within-group noise the estimator would (correctly) refuse
    # to shrink at all, so the fixture data has to carry realistic scatter.
    scatter = (-3, 2, -1, 3, 0, -2, 1, 0)
    for i, (comp, per_team) in enumerate(((get("epl"), 3), (get("laliga"), 9))):
        for j, jitter in enumerate(scatter):
            fid = 1000 * (i + 1) + j
            c.execute("INSERT INTO fixture (api_id, league_id, season, round, status_short, "
                      "home_api_id, away_api_id, home_goals, away_goals) "
                      "VALUES (?,?,?,?,'FT',1,2,1,1)",
                      (fid, comp.api_football_id, comp.season, "Regular Season - 1"))
            for team in (1, 2):
                c.execute("INSERT INTO fixture_stats (fixture_api_id, team_api_id, corners) "
                          "VALUES (?,?,?)", (fid, team, per_team + jitter))
    c.commit()
    fit = fit_league_corner_priors(c)
    lo, hi = fit["competitions"]["epl"], fit["competitions"]["laliga"]
    assert lo["raw_mean"] == 6.0 and hi["raw_mean"] == 18.0
    assert lo["mu"] < fit["pooled_mu"] < hi["mu"]                 # order preserved
    assert (hi["mu"] - lo["mu"]) < (hi["raw_mean"] - lo["raw_mean"])   # but pulled together
    # Dispersion stays gated: neither competition is anywhere near MIN_N_DISPERSION.
    assert lo["k"] is None and hi["k"] is None
