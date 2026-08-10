"""research/dsr.py — deflated selection for parameter grids scored by any per-event loss.

The loss is whatever the caller hands over, as long as LOWER IS BETTER and every column is
aligned to the same events in the same order. It was Brier when this was written and #119
moved the production caller to negative realised dollars; nothing here depends on which,
which is why `reason` says "loss/event" rather than naming a unit it cannot know.

`param_space.max_sets` decides how WIDE a grid may be. This decides whether the winner of
that grid earned its win. The two are different jobs and both are needed: sizing keeps the
search from becoming table lookup, deflation keeps the argmin from being reported as a
result. Neither one alone is enough — a 9-set grid on 40 events still produces a "best" set
every single time it runs, and that best set is a maximum of 9 noisy means.

**The statistic is a PAIRED difference against the incumbent, not a raw score.**
For each scored event i and candidate c:

    d_i(c) = brier(default, i) - brier(c, i)          positive = the candidate is better

Paired, because events are wildly uneven in difficulty — a CPI print in a quiet month and
one in a tariff month are not comparable observations, and the difference cancels the part
of the score that belongs to the event rather than to the parameters. The null hypothesis
is then exactly "this candidate is no better than what we already ship", E[d] = 0, and the
incumbent is automatically the fallback because its own d is identically zero.

**Bailey & López de Prado (2014).** Two equations, and this repo already contains a correct
and an incorrect implementation of each, so they are worth stating precisely.

Eq. 7 — expected maximum SR over N trials under the null:

    E[max SR] ~= sqrt(V[SR]) * ( (1-gamma) * Phi^-1(1 - 1/N) + gamma * Phi^-1(1 - 1/(N*e)) )

`V[SR]` is the variance of an individual trial's SR **under the null**, and this module
uses the ANALYTIC value 1/(n-1) rather than the observed spread of SRs across trials that
BLdP prescribe. That is a deliberate departure and the reason is the paired design.

BLdP estimate V[SR] empirically because in their setting — a pile of independently
constructed equity backtests — there is no common baseline to difference against, so the
null variance is not available in closed form. Differencing against the incumbent hands it
over directly: under H0 the paired edge d has mean zero, so SR = mean(d)/sd(d) has
sampling variance ~ 1/n and nothing needs estimating.

Using the observed spread here is not merely unnecessary, it is backwards. The spread
across candidates measures how much the candidates genuinely DIFFER in quality, which under
H0 is supposed to be zero and in practice is not. Measured on KXJOBLESSCLAIMS 2026-08-04:
four of the seven sets are genuinely bad (SR -0.23 to -0.57), which inflated the empirical
V[SR] by 4.9x over the analytic value and pushed the hurdle from 0.20 to 0.45. So adding a
BAD candidate to the grid raised the bar for the good one — a grid could be made to fail by
padding it with junk, or to pass by pruning the junk out afterwards.

The calibration check that settles it: against Bonferroni on the same data (raw t = 2.29,
K = 7, so p = 0.011 * 7 = 0.078, fail at 0.05), the analytic form gives p = 0.80 — fail at
0.95, same verdict, comparable margin. The empirical form gave p = 0.21, far stricter than
Bonferroni and not calibrated against anything. Note that the change does NOT flip that
decision; it was a reject before and after, which is how it was checked that this is a
correction and not a search for a threshold that passes.

Correlation between candidates is then handled in the conservative direction only: K
correlated trials have fewer than K effective degrees of freedom, so charging the full K
overstates the search and keeps the test strict. `expected_max_sr` still accepts an
explicit `var_sr` for callers whose design is not paired, but it is never below 1/(n-1).

    Trap: the second term is Phi^-1(1 - 1/(N*e)). `crypto_common/walk_forward.py:331`
    records finding `np.exp(-1)` there instead — NaN at small N, too lenient at large N.

Eq. 14 — the deflated p-value:

    DSR = Phi[ (SR - SR_0) * sqrt(T-1) / sqrt(1 - g3*SR + (g4-1)/4 * SR^2) ]

`g4` is the FULL kurtosis, 3 under normality, so the denominator is sqrt(1 + SR^2/2) — the
Lo (2002) estimator variance. `crypto_common/walk_forward.py:356` uses (g4-3)/4, which
drops that term and makes the test too lenient; `MTFSWalkForward.py:286` uses (g4-1)/4 and
is correct. This follows MTFS.

The result is a p-value, not a decision: `select()` applies the threshold, and its default
is one-sided 0.95. Below it the incumbent is kept and the reason is reported — a grid that
finds nothing is a legitimate and common outcome, and it must not read as a failure to run.
"""
from __future__ import annotations

import math

from scipy.stats import norm

GAMMA = 0.5772156649015329          # Euler-Mascheroni
ADOPT_P = 0.95                      # one-sided; below this the incumbent is kept
MIN_OBS = 12                        # matches param_space.MIN_EVENTS


def expected_max_sr(n_trials: int, n_obs: int, var_sr: float | None = None) -> float:
    """BLdP Eq. 7 with the ANALYTIC null variance 1/(n-1).

    `var_sr` overrides it for callers whose design is not paired and who therefore have to
    estimate the null spread; it is still floored at 1/(n-1), because a grid of near-clones
    would otherwise report ~zero dispersion and buy a 32-column search for the price of
    one. See the module docstring for why the paired path does not estimate at all.
    """
    if n_trials <= 1 or n_obs <= 1:
        return 0.0
    v = 1.0 / (n_obs - 1) if var_sr is None else max(float(var_sr), 1.0 / (n_obs - 1))
    n = max(n_trials, 2)
    z1 = norm.ppf(1.0 - 1.0 / n)
    z2 = norm.ppf(1.0 - 1.0 / (n * math.e))
    return math.sqrt(v) * ((1.0 - GAMMA) * z1 + GAMMA * z2)


def deflated_p(sr: float, sr_0: float, n_obs: int,
               skew: float = 0.0, kurt: float = 3.0) -> float:
    """BLdP Eq. 14. `kurt` is the FULL kurtosis (3 = normal), not the excess."""
    if n_obs <= 1:
        return 0.0
    denom_sq = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if denom_sq <= 0:
        denom_sq = 1e-9
    stat = (sr - sr_0) * math.sqrt(n_obs - 1) / math.sqrt(denom_sq)
    return float(norm.cdf(stat))


def _moments(xs: list[float]) -> tuple[float, float, float, float]:
    """(mean, sd, skew, full kurtosis) — sample sd, population skew/kurtosis.

    Written out rather than pulled from pandas because the caller may hand over a dozen
    values and scipy's small-sample bias corrections would then dominate the moment.
    """
    n = len(xs)
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1) if n > 1 else 0.0
    sd = math.sqrt(var)
    if sd <= 0:
        return m, 0.0, 0.0, 3.0
    m3 = sum((x - m) ** 3 for x in xs) / n
    m4 = sum((x - m) ** 4 for x in xs) / n
    pvar = sum((x - m) ** 2 for x in xs) / n
    return m, sd, m3 / pvar ** 1.5, m4 / pvar ** 2


def select(scores: dict, default_key: str, adopt_p: float = ADOPT_P,
           min_obs: int = MIN_OBS) -> dict:
    """Pick a parameter set from per-event Brier scores, or keep the incumbent.

    scores:      {candidate_key: [brier per scored event]} — every list must be aligned to
                 the SAME events in the SAME order, because the test is paired.
    default_key: the incumbent. Must be present; it is what everything is differenced
                 against and what is returned when nothing clears the bar.

    Returns a report. `chosen` is always a usable key. `adopted` says whether the grid
    actually moved anything, so a caller can log the difference between "the search ran and
    kept the default" and "the search did not run".
    """
    if default_key not in scores:
        raise KeyError(f"default_key {default_key!r} is not among the scored candidates")
    n_obs = len(scores[default_key])
    rep = {"chosen": default_key, "adopted": False, "n_obs": n_obs,
           "n_trials": len(scores), "dsr_p": None, "sr": None, "sr_0": None,
           "mean_edge": None, "reason": ""}
    for k, v in scores.items():
        if len(v) != n_obs:
            raise ValueError(f"candidate {k!r} has {len(v)} scores, default has {n_obs}"
                             " — the paired test needs one row per event for every column")
    if n_obs < min_obs:
        rep["reason"] = f"only {n_obs} scored events (< {min_obs}) — incumbent kept"
        return rep

    base = scores[default_key]
    # SR per candidate on the paired edge. A candidate that never differs from the default
    # has sd == 0 and no evidence either way, so it scores 0 rather than being dropped:
    # dropping it would shrink n_trials and quietly weaken the deflation.
    srs, edges = {}, {}
    for k, v in scores.items():
        d = [b - c for b, c in zip(base, v)]
        edges[k] = d
        m, sd, _, _ = _moments(d)
        srs[k] = (m / sd) if sd > 0 else 0.0

    best = max(srs, key=lambda k: (srs[k], k == default_key))
    sr = srs[best]
    if best == default_key or sr <= 0:
        rep["reason"] = "no candidate beat the incumbent on the paired mean"
        return rep

    sr_0 = expected_max_sr(len(srs), n_obs)
    m, _sd, skew, kurt = _moments(edges[best])
    p = deflated_p(sr, sr_0, n_obs, skew, kurt)

    rep.update({"dsr_p": round(p, 6), "sr": round(sr, 6), "sr_0": round(sr_0, 6),
                "mean_edge": round(m, 8), "best_key": best})
    if p >= adopt_p:
        rep["chosen"] = best
        rep["adopted"] = True
        rep["reason"] = (f"{best} beats the incumbent by {m:+.5f} loss/event "
                         f"(SR {sr:.3f} vs deflation hurdle {sr_0:.3f}, p={p:.3f})")
    else:
        rep["reason"] = (f"best candidate {best} ({m:+.5f} loss/event, SR {sr:.3f}) "
                         f"does not clear the {len(srs)}-trial hurdle {sr_0:.3f} "
                         f"(p={p:.3f} < {adopt_p}) — incumbent kept")
    return rep
