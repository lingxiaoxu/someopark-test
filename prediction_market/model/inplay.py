"""In-play (live) match model (plan 03 §4b) — drives in-play trading (04 §4c).

The pre-match model gives the 0:0 kickoff probabilities. During a match the
price is driven by **current score + time remaining**. Given minute ``t``,
current score ``(h, a)`` and remaining-time fraction ``tau = (90-t)/90``:

    remaining_home ~ Poisson(lambda_home * tau * g_home)
    remaining_away ~ Poisson(lambda_away * tau * g_away)
    final score    = current score + remaining
    → live P(home/draw/away), fair draw price, remaining-goals distribution

``g_*`` is the game-state adjustment (plan 03 §4b):
  * red card → penalised side's lambda cut sharply;
  * lead effect → leader eases off slightly, trailer pushes (calibrated, small).

This module powers the two directly tradable mechanics (plan 03 §4b / 04 §4c):
the **time-value of the draw** and **post-goal draw repricing** — exposed via
``fair_draw`` so the strategy can compare to the live market price.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.stats import poisson

from prediction_market.config import CONFIG
from prediction_market.model.strength import StrengthModel

# Game-state defaults (plan 03 §4b: calibrated, deliberately modest).
RED_CARD_LAMBDA_MULT = 0.70     # penalised side scores ~30% less after a red
LEAD_LEADER_MULT = 0.92         # leader eases off
LEAD_TRAILER_MULT = 1.10        # trailing side pushes
XG_WEIGHT = 0.35                # how much live xG over/under-performance shades remaining lambda
# Late-game tempo kill when the score is LEVEL: empirically both teams wind down
# and settle for the point in the closing minutes, so remaining scoring decays
# faster than linear time. A plain double-Poisson therefore UNDER-prices the late
# level draw (the documented Dixon-Coles late-draw deficit). We deflate both sides'
# remaining lambda once a level game passes ~70', ramping to the full factor at 90'.
LATE_LEVEL_DEFLATE = 0.30
LATE_LEVEL_FROM_MIN = 70.0


@dataclass(frozen=True)
class LiveMatchProb:
    minute: int
    home_goals: int
    away_goals: int
    tau: float
    p_home: float
    p_draw: float
    p_away: float
    fair_draw: float            # == p_draw, surfaced for the draw-value trade
    p_over_total: dict          # {line: P(final total > line)}
    exp_remaining_goals: float
    lam_home_eff: float
    lam_away_eff: float


def _game_state_mult(home_goals: int, away_goals: int, red_home: int, red_away: int) -> tuple[float, float]:
    g_home, g_away = 1.0, 1.0
    # Red cards (each red compounds).
    g_home *= RED_CARD_LAMBDA_MULT ** max(0, red_home)
    g_away *= RED_CARD_LAMBDA_MULT ** max(0, red_away)
    # Lead effect.
    if home_goals > away_goals:
        g_home *= LEAD_LEADER_MULT
        g_away *= LEAD_TRAILER_MULT
    elif away_goals > home_goals:
        g_away *= LEAD_LEADER_MULT
        g_home *= LEAD_TRAILER_MULT
    return g_home, g_away


def live_match_prob(
    lam_home: float,
    lam_away: float,
    minute: int,
    home_goals: int,
    away_goals: int,
    *,
    red_home: int = 0,
    red_away: int = 0,
    injury_time: float = 0.0,
    xg_home: float | None = None,
    xg_away: float | None = None,
    total_lines: tuple[float, ...] = (1.5, 2.5, 3.5),
    kmax: int = 8,
) -> LiveMatchProb:
    """Live W/D/L + fair draw + remaining-goals distribution.

    ``lam_home/lam_away`` are the PRE-MATCH full-90' scoring intensities (from
    ``StrengthModel.pair_lambdas``); this function scales them by remaining time
    and game state. If live ``xg_home/xg_away`` are supplied, a side that is
    OUT-creating its pre-match expectation has its remaining lambda shaded up
    (intra-game stats driving the fair price, plan 03 §4b). Past 90'+injury, tau
    clamps to 0 (final score locked).
    """
    total_minutes = 90.0 + max(0.0, injury_time)
    tau = max(0.0, (total_minutes - minute) / 90.0)
    g_home, g_away = _game_state_mult(home_goals, away_goals, red_home, red_away)
    # Late level-draw correction: tempo dies when the score is level late, so the
    # remaining lambda decays faster than linear → lifts the late draw probability.
    if home_goals == away_goals and minute > LATE_LEVEL_FROM_MIN:
        ramp = min(1.0, (minute - LATE_LEVEL_FROM_MIN) / (90.0 - LATE_LEVEL_FROM_MIN))
        deflate = 1.0 - LATE_LEVEL_DEFLATE * ramp
        g_home *= deflate
        g_away *= deflate
    # xG performance shading: ratio of actual xG to pre-match-expected xG-so-far.
    if minute > 10:
        exp_h, exp_a = lam_home * minute / 90.0, lam_away * minute / 90.0
        if xg_home is not None and exp_h > 0.1:
            g_home *= (1 - XG_WEIGHT) + XG_WEIGHT * min(2.0, xg_home / exp_h)
        if xg_away is not None and exp_a > 0.1:
            g_away *= (1 - XG_WEIGHT) + XG_WEIGHT * min(2.0, xg_away / exp_a)
    lh = lam_home * tau * g_home
    la = lam_away * tau * g_away

    # Remaining-goal distributions (independent Poisson; rho correction is a
    # full-match low-score effect, not meaningful on the residual tail).
    rh = poisson.pmf(np.arange(kmax + 1), lh)
    ra = poisson.pmf(np.arange(kmax + 1), la)
    rh /= rh.sum(); ra /= ra.sum()
    m = np.outer(rh, ra)  # P(remaining_home=i, remaining_away=j)

    i = np.arange(kmax + 1)[:, None]
    j = np.arange(kmax + 1)[None, :]
    final_diff = (home_goals + i) - (away_goals + j)   # final home - away
    p_home = float(m[final_diff > 0].sum())
    p_draw = float(m[final_diff == 0].sum())
    p_away = float(m[final_diff < 0].sum())

    final_total = (home_goals + i) + (away_goals + j)
    p_over = {ln: float(m[final_total > ln].sum()) for ln in total_lines}
    exp_remaining = float(lh + la)

    return LiveMatchProb(
        minute=minute, home_goals=home_goals, away_goals=away_goals, tau=tau,
        p_home=p_home, p_draw=p_draw, p_away=p_away, fair_draw=p_draw,
        p_over_total=p_over, exp_remaining_goals=exp_remaining,
        lam_home_eff=lh, lam_away_eff=la,
    )


def live_from_strength(
    sm: StrengthModel, home_id: str, away_id: str, minute: int, home_goals: int, away_goals: int, **kw
) -> LiveMatchProb:
    """Convenience: derive pre-match lambdas from the strength model, then go live."""
    lam_h, lam_a = sm.pair_lambdas(home_id, away_id, knockout=False)
    return live_match_prob(lam_h, lam_a, minute, home_goals, away_goals, **kw)


if __name__ == "__main__":
    from prediction_market.ingest.prior_ingest import load_prior
    from prediction_market.model.strength import build_strength

    sm = build_strength(load_prior())
    print("Brazil vs Morocco, evolving 0:0 — draw time-value (plan 03 §4b):")
    for minute in (1, 30, 60, 80, 89):
        lp = live_from_strength(sm, "brazil", "morocco", minute, 0, 0)
        print(f"  {minute:>2}'  P(home)={lp.p_home:.3f} P(draw)={lp.p_draw:.3f} "
              f"P(away)={lp.p_away:.3f}  (fair draw rises as time runs out)")
    print("\nPost-goal repricing — Brazil 1:0 at 55':")
    lp = live_from_strength(sm, "brazil", "morocco", 55, 1, 0)
    print(f"  P(home)={lp.p_home:.3f} P(draw)={lp.p_draw:.3f} P(away)={lp.p_away:.3f}")
