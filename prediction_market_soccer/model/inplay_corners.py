"""inplay_corners.py — live fair value for the total-corners market.

Mirrors model/inplay.py (the 3-way Skellam model): takes the live match state and a
pre-match corner-intensity prior, returns the fair P(final total corners > line) for
each quoted line. The corner market (Kalshi per-competition `*CORNERS` "X+", i.e. over
(X-1).5) settles on FULL-TIME total corners, so live signals are far more robust than
pre-match: the realized corner count `corners_now` is already banked, and only the
remaining corners are modelled.

Two lessons from the corner backtest are baked in:
  * OVERDISPERSION. Match-total corners are overdispersed against Poisson (club fit:
    mean 9.32, var 12.92 across 203 matches) — a Poisson tail understates high totals
    and overstates the middle lines. We model the REMAINING corners as Negative-Binomial
    (Poisson–Gamma) with a mild dispersion, so the fair probabilities carry the real fat
    right tail.
  * PACE SHRINKAGE. The pre-match prior is unreliable for a specific team pair, but
    the realized in-match pace is noisy early. We blend them: the prior is worth
    ~`PRIOR_MINUTES` of observation, so realized pace takes over as the match runs.

PER-COMPETITION PRIOR (club edition). The corner rate is a competition property, not a
World Cup constant, so the full-match expectation is looked up per competition from
`data/priors/league_corners.json` (fitted by `fit_league_corner_priors` below) with the
global mean as the fallback for any competition that has no fitted entry yet.

Pure functions apart from the one lazily-cached JSON read; the fit itself only runs from
`__main__`. The signal layer (strategy/inplay_tactics.py) reads live corners from
fixture_stats and calls `live_corners_fair`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from scipy.stats import nbinom, poisson

# ── tunables (conservative; a live signal must not over-fire) ─────────────────
# Fitted on the club data actually in soccer.db (203 finished fixtures with both teams'
# corner counts, 11 competitions): mean total 9.32, var 12.92. This replaces the WC-2026
# 9.5; it is the FALLBACK only — a competition with a fitted entry uses its own value.
CORNER_TOTAL_PRIOR = 9.32     # pre-match full-match total-corners expectation (pooled club fit)
PRIOR_MINUTES = 30.0          # the prior is worth this many minutes of observed pace
DISPERSION_K = 10.0          # NegBin size for REMAINING corners; var = m(1+m/k). ↑k → ~Poisson
REG_MINUTES = 90.0
# A competition needs this many fitted matches before its OWN dispersion is trusted. The
# across-match variance we can measure mixes fixture-level heterogeneity (which team pair
# it is) into the dispersion, so it is a floor on the true within-fixture k rather than an
# estimate of it; below this n that floor is pure noise and the global DISPERSION_K —
# validated in the corner backtest — is the safer tail. Mirrors the §3.5 per-league
# calibration gate: build the machinery now, open it when the sample earns it.
MIN_N_DISPERSION = 100
PRIORS_FILENAME = "league_corners.json"
# score-state coupling of the REMAINING corner rate (mild — see corner event study):
COUPLE_ONE_GOAL = 1.10       # a one-goal game stays contested → slightly more corners
COUPLE_BLOWOUT = 0.92        # 2+ goal gap → game opens/eases → slightly fewer contested corners


@dataclass(frozen=True)
class LiveCornersFair:
    minute: int
    corners_now: int
    tau: float                 # fraction of regulation remaining
    nu_rem: float              # expected remaining corners (state-adjusted)
    exp_total: float           # corners_now + nu_rem
    p_over: dict = field(default_factory=dict)   # {line(float): P(final total > line)}
    valid: bool = True         # False → inputs unusable, do NOT trade


_PRIORS_CACHE: dict | None = None


def _load_priors() -> dict:
    """Lazily read data/priors/league_corners.json. A missing/broken file is not fatal —
    every competition then falls back to the pooled global, exactly as before the fit."""
    global _PRIORS_CACHE
    if _PRIORS_CACHE is None:
        from prediction_market_soccer.config import CONFIG
        p = CONFIG.paths.priors / PRIORS_FILENAME
        try:
            _PRIORS_CACHE = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except Exception as e:
            print(f"[inplay_corners] league_corners.json unreadable ({type(e).__name__}: {e}) "
                  f"— falling back to the pooled prior")
            _PRIORS_CACHE = {}
    return _PRIORS_CACHE


def corner_prior(comp_key: str | None) -> tuple[float, float]:
    """(nu_full, dispersion_k) for a competition. Unknown/unfitted → the pooled globals."""
    entry = (_load_priors().get("competitions") or {}).get(comp_key or "") or {}
    mu = entry.get("mu")
    k = entry.get("k")            # null until the competition clears MIN_N_DISPERSION
    return (float(mu) if mu else CORNER_TOTAL_PRIOR,
            float(k) if k else DISPERSION_K)


def _remaining_rate(nu_full: float, corners_now: int, minute: float) -> float:
    """Blend the pre-match full-match rate with the realized in-match pace.
    Returns an estimate of the FULL-match total; the caller scales by tau."""
    minute = max(minute, 0.0)
    realized_full = (corners_now / minute * REG_MINUTES) if minute > 0 else nu_full
    w_prior = PRIOR_MINUTES / (PRIOR_MINUTES + minute)
    return w_prior * nu_full + (1.0 - w_prior) * realized_full


def _coupling(home_goals: int, away_goals: int) -> float:
    gap = abs(home_goals - away_goals)
    if gap == 0:
        return 1.0
    if gap == 1:
        return COUPLE_ONE_GOAL
    return COUPLE_BLOWOUT


def _p_over_remaining(need: float, nu_rem: float, dispersion_k: float) -> float:
    """P(remaining corners make the final total exceed the line).
    `need` = line - corners_now (how many MORE corners are required to go Over).
    NegBin(size=k, mu=nu_rem) on the remaining count; Poisson as k→∞ fallback."""
    if need < 0:
        return 1.0                       # line already broken → Over certain
    # remaining must be at least floor(need)+1 to exceed a half-integer line
    k_min = int(need) + 1
    if nu_rem <= 1e-9:
        return 0.0 if k_min >= 1 else 1.0
    if dispersion_k >= 1e6:
        return float(poisson.sf(k_min - 1, nu_rem))
    # NegBin parameterised by mean nu_rem and size k: p = k/(k+mu)
    p = dispersion_k / (dispersion_k + nu_rem)
    return float(nbinom.sf(k_min - 1, dispersion_k, p))


def live_corners_fair(
    corners_now: int,
    minute: int,
    home_goals: int,
    away_goals: int,
    *,
    lines: tuple[float, ...] = (6.5, 7.5, 8.5, 9.5, 10.5),
    nu_full: float | None = None,
    injury_time: float = 0.0,
    dispersion_k: float | None = None,
    comp_key: str | None = None,
) -> LiveCornersFair:
    """Fair P(final total corners > line) for each line, from the live state.

    corners_now : live total corners (both teams). MUST be a finite int ≥ 0.
    minute      : elapsed regulation minute.
    comp_key    : competition key — supplies the per-competition prior when `nu_full` /
                  `dispersion_k` are not passed explicitly (explicit args always win, so a
                  caller with a fixture-specific prior is unaffected).
    Returns valid=False if inputs are unusable (caller must then NOT trade)."""
    if nu_full is None or dispersion_k is None:
        mu_c, k_c = corner_prior(comp_key)
        nu_full = mu_c if nu_full is None else nu_full
        dispersion_k = k_c if dispersion_k is None else dispersion_k
    # input sanity — the model refuses rather than guesses
    if corners_now is None or minute is None:
        return LiveCornersFair(0, 0, 0.0, 0.0, 0.0, {}, valid=False)
    if corners_now < 0 or corners_now > 40 or minute < 0 or minute > 130:
        return LiveCornersFair(minute, corners_now, 0.0, 0.0, float(corners_now), {}, valid=False)

    played = min(minute, REG_MINUTES)
    tau = max(0.0, (REG_MINUTES + max(injury_time, 0.0) - minute) / REG_MINUTES)
    base_full = _remaining_rate(nu_full, corners_now, played)
    nu_rem = max(0.0, base_full * tau * _coupling(home_goals, away_goals))
    p_over = {float(L): _p_over_remaining(L - corners_now, nu_rem, dispersion_k) for L in lines}
    return LiveCornersFair(minute, corners_now, round(tau, 4), round(nu_rem, 3),
                           round(corners_now + nu_rem, 3), p_over, valid=True)


# ── per-competition fit (offline; writes data/priors/league_corners.json) ─────
def fit_league_corner_priors(conn) -> dict:
    """Empirical-Bayes fit of the full-match total-corner prior per competition.

    Why EB and not the raw per-competition mean: the whole club sample is ~200 matches
    across 11 competitions, so a competition's raw mean (EPL 8.0 on n=10, Ligue 1 10.2 on
    n=9) is dominated by sampling noise. The James-Stein shrinkage weight n/(n+n0) is
    itself estimated from the data — n0 = within-competition variance / between-competition
    variance — so it tightens automatically as the season fills in. On today's sample the
    between-competition component is ~0.06 against a within of ~12.9, i.e. the spread of
    the observed means is almost entirely noise, and every competition lands within ±0.15
    of the pool. That is the honest answer, not a defect: hand-entering "EPL ≈ 10.5" from
    outside knowledge would be exactly the guess this fit exists to avoid.
    """
    rows = conn.execute(
        "SELECT f.league_id lid, SUM(fs.corners) tot, COUNT(*) nt "
        "FROM fixture_stats fs JOIN fixture f ON f.api_id = fs.fixture_api_id "
        "WHERE fs.corners IS NOT NULL AND f.status_short = 'FT' "
        "GROUP BY fs.fixture_api_id HAVING nt = 2").fetchall()
    from prediction_market_soccer.config import leagues as _lg
    by: dict[str, list[int]] = {}
    for r in rows:
        comp = _lg.by_api_id(r["lid"])
        if comp:
            by.setdefault(comp.key, []).append(int(r["tot"]))
    if not by:
        return {"competitions": {}, "n_total": 0}

    allv = [x for v in by.values() for x in v]
    n_all = len(allv)
    mu0 = sum(allv) / n_all
    var0 = sum((x - mu0) ** 2 for x in allv) / max(n_all - 1, 1)
    means = {k: sum(v) / len(v) for k, v in by.items()}
    n_groups = len(by)
    within = (sum(sum((x - means[k]) ** 2 for x in v) for k, v in by.items())
              / max(n_all - n_groups, 1))
    gm = sum(means[k] * len(by[k]) for k in by) / n_all
    btw_raw = (sum(len(by[k]) * (means[k] - gm) ** 2 for k in by) / max(n_groups - 1, 1)
               if n_groups > 1 else 0.0)
    # positive-part between-group variance: the naive estimator goes negative when the
    # observed spread is smaller than sampling noise alone would produce.
    between = max((btw_raw - within) / (n_all / n_groups), 1e-6)
    n0 = within / between

    comps: dict[str, dict] = {}
    for k, v in by.items():
        n = len(v)
        m = sum(v) / n
        var = sum((x - m) ** 2 for x in v) / (n - 1) if n > 1 else None
        mu = (n * m + n0 * mu0) / (n + n0)
        # NegBin size from var = m(1 + m/k); note this is the ACROSS-match variance, so it
        # is a floor on the within-fixture k (see MIN_N_DISPERSION).
        k_fit = (m * m / (var - m)) if (var is not None and var > m) else None
        comps[k] = {
            "mu": round(mu, 3),
            "k": (round(k_fit, 2) if (k_fit and n >= MIN_N_DISPERSION) else None),
            "n": n,
            "raw_mean": round(m, 3),
            "raw_var": (round(var, 3) if var is not None else None),
            "k_floor": (round(k_fit, 2) if k_fit else None),
        }
    from datetime import datetime, timezone
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "method": ("empirical-Bayes shrinkage of each competition's mean full-match total "
                   "corners toward the pooled mean, weight n/(n+n0) with n0 = within-comp "
                   "variance / positive-part between-comp variance; dispersion emitted only "
                   f"at n >= {MIN_N_DISPERSION}"),
        "pooled_mu": round(mu0, 3),
        "pooled_var": round(var0, 3),
        "n_total": n_all,
        "n0": round(n0, 1),
        "within_var": round(within, 3),
        "between_var": round(between, 4),
        "min_n_dispersion": MIN_N_DISPERSION,
        "competitions": dict(sorted(comps.items(), key=lambda kv: -kv[1]["n"])),
    }


if __name__ == "__main__":
    import sys

    from prediction_market_soccer.config import CONFIG
    from prediction_market_soccer.ingest import store

    payload = fit_league_corner_priors(store.init_db())
    print(f"corner prior fit: n={payload['n_total']} matches, pooled mu={payload['pooled_mu']}, "
          f"shrinkage n0={payload['n0']}")
    for k, e in payload["competitions"].items():
        print(f"  {k:14s} n={e['n']:3d} raw={e['raw_mean']:6.3f} -> mu={e['mu']:6.3f} "
              f"k={e['k'] if e['k'] else '(pooled)'}")
    if "--write" in sys.argv:
        out = CONFIG.paths.priors / PRIORS_FILENAME
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"wrote {out}")
