"""Deflation has exactly one job: stop the argmin from being reported as a discovery.

So the load-bearing test here is a Monte Carlo of the null — many grids of candidates that
are all secretly equal to the incumbent, checking how often `select` adopts one anyway. A
naive argmin adopts on essentially every one of those draws, because a maximum of K noisy
means is positive with probability approaching 1 as K grows. That is the failure mode this
module exists for, and asserting the arithmetic of Eq. 7 and Eq. 14 without asserting the
false-adoption rate would leave it untested.

The rest pin the two sign errors this repo has already made once each (see dsr.py's module
docstring): the N*e term in Eq. 7, and the (kurt-1)/4 term in Eq. 14.
"""
from __future__ import annotations

import math
import random

import pytest

from prediction_market_macro.research.dsr import (MIN_OBS, deflated_p, expected_max_sr,
                                                  select)


def _grid(rng, n_obs, n_trials, edge=0.0, noise=0.02, default="p0"):
    """A default column plus n_trials-1 candidates whose paired edge has mean `edge`."""
    base = [rng.uniform(0.05, 0.45) for _ in range(n_obs)]
    scores = {default: base}
    for i in range(1, n_trials):
        scores[f"p{i}"] = [b - (rng.gauss(edge, noise)) for b in base]
    return scores


# ── the null ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("n_trials", [9, 27, 72])
def test_pure_noise_is_almost_never_adopted(n_trials):
    """Every candidate is the incumbent plus zero-mean noise, so every adoption is a false
    positive. The 0.95 threshold buys a one-sided 5% nominal rate; the floor on V[SR]
    makes the realised rate lower still."""
    rng = random.Random(20260804 + n_trials)
    adopted = sum(select(_grid(rng, 40, n_trials, edge=0.0), "p0")["adopted"]
                  for _ in range(300))
    assert adopted / 300 <= 0.05, f"{adopted}/300 false adoptions at K={n_trials}"


@pytest.mark.parametrize("n_trials", [9, 27, 72])
def test_the_naive_argmin_this_replaces_would_have_adopted_nearly_every_time(n_trials):
    """Pins the size of the problem rather than assuming it. If this ever stops holding,
    the Monte Carlo above has gone slack and is no longer testing anything."""
    rng = random.Random(11 + n_trials)
    wins = 0
    for _ in range(300):
        s = _grid(rng, 40, n_trials, edge=0.0)
        base = s["p0"]
        best = max(s, key=lambda k: sum(b - c for b, c in zip(base, s[k])))
        wins += best != "p0"
    assert wins / 300 > 0.95, "the undeflated argmin should pick a 'winner' almost always"


# ── power ───────────────────────────────────────────────────────────────────────

def test_a_real_edge_is_adopted():
    """Deflation that never adopts anything is just as useless as one that always does."""
    rng = random.Random(7)
    rep = select(_grid(rng, 60, 27, edge=0.03, noise=0.01), "p0")
    assert rep["adopted"] and rep["dsr_p"] >= 0.95
    assert rep["mean_edge"] > 0


def test_the_hurdle_rises_with_the_number_of_trials():
    """Same edge, same history, more columns searched — must get harder, not easier."""
    ps = []
    for k in (5, 40, 300):
        rng = random.Random(3)
        ps.append(select(_grid(rng, 40, k, edge=0.004, noise=0.01), "p0")["sr_0"])
    assert ps[0] < ps[1] < ps[2]


# ── the incumbent's standing ────────────────────────────────────────────────────

def test_the_incumbent_is_returned_whenever_nothing_clears_the_bar():
    rng = random.Random(5)
    rep = select(_grid(rng, 40, 27, edge=0.0), "p0", adopt_p=0.999999)
    assert rep["chosen"] == "p0" and rep["adopted"] is False and rep["reason"]


def test_thin_history_skips_the_test_entirely():
    rng = random.Random(5)
    rep = select(_grid(rng, MIN_OBS - 1, 27, edge=0.5, noise=0.001), "p0")
    assert rep["chosen"] == "p0" and not rep["adopted"]
    assert "scored events" in rep["reason"]


def test_identical_candidates_cannot_be_adopted():
    """The dead-parameter case: a grid of exact clones. There is no edge to find and
    nothing may be adopted, and it must not divide by a zero sd on the way."""
    base = [0.1, 0.2, 0.3] * 10
    rep = select({f"p{i}": list(base) for i in range(20)}, "p0")
    assert rep["chosen"] == "p0" and not rep["adopted"]


def test_clones_do_not_buy_a_free_pass_through_the_variance_floor():
    """19 clones of the incumbent plus one lucky column. If a caller passes an empirical
    V[SR], it is ~zero here because the clones are identical, so an unfloored Eq. 7 would
    deflate by almost nothing and wave the lucky column through on a 20-column search."""
    rng = random.Random(99)
    n = 40
    base = [rng.uniform(0.05, 0.45) for _ in range(n)]
    scores = {f"p{i}": list(base) for i in range(20)}
    scores["p19"] = [b - rng.gauss(0.0, 0.02) for b in base]
    rep = select(scores, "p0")
    assert not rep["adopted"]
    assert expected_max_sr(20, n, var_sr=0.0) == expected_max_sr(20, n) > 0


def test_a_bad_candidate_cannot_raise_the_bar_for_a_good_one():
    """The reason Eq. 7 uses the analytic null variance and not the observed spread of
    SRs. Padding a grid with junk widens that spread, and under the BLdP estimator that
    would RAISE the hurdle for the genuine winner — so a grid could be failed by adding
    bad candidates, or passed by pruning them out afterwards. Measured on real data
    (KXJOBLESSCLAIMS, 2026-08-04) the inflation was 4.9x. Only the trial COUNT may matter.
    """
    rng = random.Random(4242)
    n = 48
    base = [rng.uniform(0.05, 0.45) for _ in range(n)]
    good = {"p0": base, "p1": [b - rng.gauss(0.02, 0.05) for b in base]}
    padded = dict(good)
    for i in range(2, 7):                       # five genuinely terrible candidates
        padded[f"p{i}"] = [b + rng.gauss(0.05, 0.05) for b in base]
    # same number of trials, but the extra columns are junk rather than clones
    clones = dict(good)
    for i in range(2, 7):
        clones[f"p{i}"] = list(base)
    assert select(padded, "p0")["sr_0"] == pytest.approx(select(clones, "p0")["sr_0"])


def test_the_hurdle_agrees_with_bonferroni():
    """An independent calibration. A one-sided Bonferroni correction over K trials and the
    deflated test should reach the same verdict at matched levels; large disagreement means
    one of them is not measuring multiplicity. This is the check that caught the empirical
    V[SR] being ~4x too strict."""
    from scipy.stats import norm
    rng = random.Random(2026)
    n, k = 48, 7
    for edge, noise in ((0.0, 0.02), (0.006, 0.02), (0.02, 0.02)):
        s = _grid(rng, n, k, edge=edge, noise=noise)
        rep = select(s, "p0")
        sr = rep["sr"]
        if sr is None:
            continue
        bonf = min(1.0, (1.0 - norm.cdf(sr * math.sqrt(n))) * k)
        assert (rep["dsr_p"] >= 0.95) == (bonf <= 0.05), \
            f"DSR p={rep['dsr_p']:.3f} disagrees with Bonferroni p={bonf:.3f}"


# ── the two arithmetic traps ────────────────────────────────────────────────────

def test_eq7_second_term_uses_N_times_e_not_N_over_e():
    """`np.exp(-1)` in place of the reciprocal was a real bug here once
    (crypto_common/walk_forward.py:331): it goes NaN at small N. Pin finiteness at N=2 and
    monotonicity, both of which the buggy form fails."""
    vals = [expected_max_sr(k, 1000) for k in (2, 3, 10, 100, 1000)]
    assert all(math.isfinite(v) for v in vals)
    assert vals == sorted(vals)
    assert expected_max_sr(1, 1000) == 0.0


def test_eq14_denominator_is_the_lo_variance_not_the_excess_kurtosis_form():
    """Under normality the denominator must be sqrt(1 + SR^2/2). The (kurt-3)/4 spelling
    gives sqrt(1), which is smaller, which makes every p-value too generous."""
    sr, n = 0.8, 50
    got = deflated_p(sr, 0.0, n, skew=0.0, kurt=3.0)
    from scipy.stats import norm
    want = float(norm.cdf(sr * math.sqrt(n - 1) / math.sqrt(1 + sr * sr / 2)))
    assert got == pytest.approx(want, abs=1e-12)
    assert got < float(norm.cdf(sr * math.sqrt(n - 1)))     # strictly stricter


def test_negative_skew_is_penalised():
    """1 - g3*SR grows as skew goes negative, so a left-tailed edge must score lower."""
    assert deflated_p(0.5, 0.0, 50, skew=-1.5) < deflated_p(0.5, 0.0, 50, skew=1.5)


# ── misuse ──────────────────────────────────────────────────────────────────────

def test_a_missing_incumbent_is_an_error_not_a_default():
    with pytest.raises(KeyError):
        select({"a": [0.1] * 20}, "p0")


def test_misaligned_columns_are_an_error_because_the_test_is_paired():
    """Silently zipping to the shorter column would difference two different events."""
    with pytest.raises(ValueError):
        select({"p0": [0.1] * 20, "p1": [0.1] * 19}, "p0")
