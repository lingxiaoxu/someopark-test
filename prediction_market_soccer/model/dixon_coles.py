"""Dixon-Coles double-Poisson single-match kernel (plan 03 §2/§4).

Given two scoring intensities (lambda_home, lambda_away), build the full score
probability matrix with the Dixon-Coles low-score correlation correction, then
derive every single-match market: W/D/L, total goals, over/under, both-teams-
to-score, and the knockout "advance" probability (regulation + extra time +
penalties, plan 03 §4b).

All probabilities use float64; price->probability conversion downstream uses
Decimal (plan 01 §4.3), but the model's own math is float.
"""
from __future__ import annotations

import numpy as np
from scipy.stats import poisson


def score_matrix(
    lam_h: float,
    lam_a: float,
    rho: float = -0.05,
    kmax: int = 10,
) -> np.ndarray:
    """P(home=x, away=y) matrix, shape (kmax+1, kmax+1), normalized.

    Dixon-Coles tau correction applies only to the four low-score cells
    (0-0, 0-1, 1-0, 1-1), capturing their empirical dependence (plan 03 §2).
    """
    h = poisson.pmf(np.arange(kmax + 1), lam_h)
    a = poisson.pmf(np.arange(kmax + 1), lam_a)
    m = np.outer(h, a)
    # Low-score correlation correction.
    m[0, 0] *= 1.0 - lam_h * lam_a * rho
    m[0, 1] *= 1.0 + lam_h * rho
    m[1, 0] *= 1.0 + lam_a * rho
    m[1, 1] *= 1.0 - rho
    total = m.sum()
    return m / total if total > 0 else m


def wdl(m: np.ndarray) -> tuple[float, float, float]:
    """(P home win, P draw, P away win) from a score matrix."""
    p_home = float(np.tril(m, -1).sum())  # home goals > away goals
    p_draw = float(np.trace(m))
    p_away = float(np.triu(m, 1).sum())
    return p_home, p_draw, p_away


def total_goals_pmf(m: np.ndarray) -> np.ndarray:
    """PMF over total goals (home+away), index = total goal count."""
    kmax = m.shape[0] - 1
    pmf = np.zeros(2 * kmax + 1)
    for x in range(kmax + 1):
        for y in range(kmax + 1):
            pmf[x + y] += m[x, y]
    return pmf


def over_under(m: np.ndarray, line: float) -> tuple[float, float, float]:
    """(P over, P under, P push) for a totals line (e.g. 2.5, 3.0).

    Half-lines have zero push; integer lines can push on an exact total.
    """
    pmf = total_goals_pmf(m)
    totals = np.arange(len(pmf))
    p_over = float(pmf[totals > line].sum())
    p_under = float(pmf[totals < line].sum())
    p_push = float(pmf[totals == line].sum()) if float(line).is_integer() else 0.0
    return p_over, p_under, p_push


def both_teams_score(m: np.ndarray) -> float:
    """P(both teams score >= 1)."""
    return float(m[1:, 1:].sum())


def knockout_advance_prob(
    lam_h: float,
    lam_a: float,
    *,
    rho: float = -0.05,
    kmax: int = 10,
    et_fraction: float = 30.0 / 90.0,
    penalty_home_edge: float = 0.50,
) -> float:
    """P(home team advances) in a knockout tie (plan 03 §4b).

        P(adv) = P(home win 90') + P(draw 90') * [P(home win ET) +
                 P(draw ET) * P(home win shootout)]

    ``penalty_home_edge`` is the home team's shootout win probability
    (0.50 neutral; favorite ~0.52-0.55 per plan).
    """
    reg = score_matrix(lam_h, lam_a, rho, kmax)
    pw_h, pd, _pw_a = wdl(reg)

    et = score_matrix(lam_h * et_fraction, lam_a * et_fraction, rho, kmax)
    et_h, et_d, _et_a = wdl(et)

    return float(pw_h + pd * (et_h + et_d * penalty_home_edge))


def two_leg_advance_prob(
    lam_h: float,
    lam_a: float,
    agg_h: int,
    agg_a: int,
    *,
    rho: float = -0.05,
    kmax: int = 10,
    et_fraction: float = 30.0 / 90.0,
    penalty_home_edge: float = 0.50,
    et_then_pens: bool = True,
) -> float:
    """P(the SECOND-LEG HOME team advances) given the first-leg aggregate
    (TRANSFORM_PLAN C5). No away-goals rule (abolished 2021): a level aggregate
    after leg 2 goes to ET then pens (UEFA, ``et_then_pens=True``) or straight
    to pens (CONMEBOL ties, ``False``). ``agg_h``/``agg_a`` = aggregate goals of
    the leg-2 home/away side carried IN from leg 1 (0-0 for a fresh deciding leg).
    """
    m = score_matrix(lam_h, lam_a, rho, kmax)
    k = m.shape[0]
    # vectorized: the aggregate verdict depends only on (gh−ga) vs (agg_a−agg_h)
    gh_g, ga_g = np.indices((k, k))
    diff = gh_g - ga_g
    need = agg_a - agg_h              # home advances iff diff > need; level iff ==
    p_adv = float(m[diff > need].sum())
    p_level = float(m[diff == need].sum())
    if p_level > 0:
        if et_then_pens:
            et = score_matrix(lam_h * et_fraction, lam_a * et_fraction, rho, kmax)
            et_h, et_d, _ = wdl(et)   # ET decides on total (aggregate level → ET goals decide)
            p_adv += p_level * (et_h + et_d * penalty_home_edge)
        else:
            p_adv += p_level * penalty_home_edge
    return float(p_adv)


def tie_advance_prob(
    lam1_h: float,
    lam1_a: float,
    lam2_h: float,
    lam2_a: float,
    *,
    rho: float = -0.05,
    kmax: int = 10,
    et_fraction: float = 30.0 / 90.0,
    penalty_leg2_home_edge: float = 0.50,
    et_then_pens: bool = True,
) -> float:
    """P(the LEG-1 HOME team advances) over a full two-legged tie, BEFORE leg 1.

    Leg-1 home team = A (hosts leg 1, visits leg 2). Analytic expectation of
    ``two_leg_advance_prob`` over the leg-1 score distribution — no Monte Carlo.
    ``penalty_leg2_home_edge`` is the shootout edge of the LEG-2 HOME side (= B).
    """
    m1 = score_matrix(lam1_h, lam1_a, rho, kmax)
    k = m1.shape[0]
    # leg-2 verdict depends only on d = g1b − g1a (B's aggregate head-start), so
    # evaluate two_leg ONCE per distinct d ∈ [−kmax, kmax] instead of per cell
    # (121 calls → ≤21; each itself vectorized above). Exact, no approximation.
    p2_cache: dict[int, float] = {}

    def p_b_adv_for(d: int) -> float:
        if d not in p2_cache:
            p2_cache[d] = two_leg_advance_prob(
                lam2_h, lam2_a, max(d, 0), max(-d, 0), rho=rho, kmax=kmax,
                et_fraction=et_fraction, penalty_home_edge=penalty_leg2_home_edge,
                et_then_pens=et_then_pens)
        return p2_cache[d]

    p_a = 0.0
    for g1a in range(k):          # A's leg-1 (home) goals
        for g1b in range(k):      # B's leg-1 (away) goals
            p = float(m1[g1a, g1b])
            if p < 1e-12:
                continue
            p_a += p * (1.0 - p_b_adv_for(g1b - g1a))
    return float(p_a)
