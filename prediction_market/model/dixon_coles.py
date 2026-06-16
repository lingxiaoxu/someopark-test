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
