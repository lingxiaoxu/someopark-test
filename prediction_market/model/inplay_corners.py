"""inplay_corners.py — live fair value for the total-corners market.

Mirrors model/inplay.py (the 3-way Skellam model): takes the live match state and a
pre-match corner-intensity prior, returns the fair P(final total corners > line) for
each quoted line. The corner market (Kalshi KXWCCORNERS "X+", i.e. over (X-1).5)
settles on FULL-TIME total corners, so live signals are far more robust than
pre-match: the realized corner count `corners_now` is already banked, and only the
remaining corners are modelled.

Two lessons from the France/Morocco corner backtest are baked in:
  * OVERDISPERSION. Match-total corners have var≈16 vs mean≈9.8 — a Poisson tail
    understates high totals and overstates the middle lines. We model the REMAINING
    corners as Negative-Binomial (Poisson–Gamma) with a mild dispersion, so the fair
    probabilities carry the real fat right tail.
  * PACE SHRINKAGE. The pre-match prior is unreliable for a specific team pair, but
    the realized in-match pace is noisy early. We blend them: the prior is worth
    ~`PRIOR_MINUTES` of observation, so realized pace takes over as the match runs.

Pure functions; no I/O. The signal layer (strategy/inplay_tactics.py) reads live
corners from fixture_stats and calls `live_corners_fair`.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from scipy.stats import nbinom, poisson

# ── tunables (conservative; a live signal must not over-fire) ─────────────────
CORNER_TOTAL_PRIOR = 9.5      # pre-match full-match total-corners expectation (WC 2026 ~9.8)
PRIOR_MINUTES = 30.0          # the prior is worth this many minutes of observed pace
DISPERSION_K = 10.0          # NegBin size for REMAINING corners; var = m(1+m/k). ↑k → ~Poisson
REG_MINUTES = 90.0
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
    nu_full: float = CORNER_TOTAL_PRIOR,
    injury_time: float = 0.0,
    dispersion_k: float = DISPERSION_K,
) -> LiveCornersFair:
    """Fair P(final total corners > line) for each line, from the live state.

    corners_now : live total corners (both teams). MUST be a finite int ≥ 0.
    minute      : elapsed regulation minute.
    Returns valid=False if inputs are unusable (caller must then NOT trade)."""
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
