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

from prediction_market.config import CONFIG
from prediction_market.model.dixon_coles import score_matrix
from prediction_market.model.strength import StrengthModel

# Game-state defaults (plan 03 §4b: calibrated, deliberately modest).
RED_CARD_LAMBDA_MULT = 0.70     # penalised side scores ~30% less after a red
LEAD_LEADER_MULT = 0.92         # leader eases off (base, per 1-goal lead at full time)
LEAD_TRAILER_MULT = 1.10        # trailing side pushes (base, per 1-goal deficit at full time)
XG_WEIGHT = 0.35                # MAX live-xG shading weight (earned fully only late — see ①)
# Late-game tempo kill when the score is LEVEL: empirically both teams wind down
# and settle for the point in the closing minutes, so remaining scoring decays
# faster than linear time. A plain double-Poisson therefore UNDER-prices the late
# level draw (the documented Dixon-Coles late-draw deficit). We deflate both sides'
# remaining lambda once a level game passes ~70', ramping to the full factor at 90'.
LATE_LEVEL_DEFLATE = 0.30
LATE_LEVEL_FROM_MIN = 70.0

# ── model-refinement tunables (all reversible via the live_match_prob flags) ──────────────
# ① Sample-size-aware xG shading. 25' of xG is mostly noise, so the shading weight ramps
#    linearly from 0 at kickoff to the full XG_WEIGHT at XG_FULL_TRUST_MIN. Set <=0 to fall
#    back to the old always-full weight.
XG_FULL_TRUST_MIN = 60.0
# ② Non-linear scoring hazard. Goals are NOT uniform across the 90 — the rate rises through the
#    match (fatigue, chasing). Bucketed share of goals per 15' window (empirical football profile,
#    rising into the second half). Used to scale the REMAINING scoring intensity instead of the
#    flat remaining-time fraction. `tau` itself stays linear (downstream consumers depend on it).
GOAL_HAZARD_SHARES = (0.11, 0.13, 0.16, 0.17, 0.20, 0.23)   # 0-15,15-30,30-45,45-60,60-75,75-90
# ③ Dixon-Coles low-score correlation on the RESIDUAL (remaining-goal) joint — corrects the
#    0-0/1-0/0-1/1-1 residual that drives the draw contract. Reuses the pre-match dc_rho.
RESIDUAL_DC_RHO = CONFIG.model.dc_rho
# ④ State-scaling: lead effect grows with the goal margin and with how late it is; a red card to
#    the TRAILING side bites extra (a comeback needs more attack, now a man down).
LEAD_MARGIN_STEP = 0.5          # each extra goal of margin adds this fraction of the base ease/push
RED_TRAILER_EXTRA = 0.85        # extra remaining-λ multiplier for a red to the trailing side


def _remaining_scoring_fraction(minute: float, total_minutes: float) -> float:
    """Fraction of a match's scoring intensity still to come after ``minute``.

    A flat hazard reproduces the old linear ``(90-minute)/90``. GOAL_HAZARD_SHARES makes the
    hazard rise through the match, so at (say) 25' MORE than the linear share of goals remains.
    Injury time past 90' accrues at the final bucket's rate. Normalised to 1.0 at kickoff.
    """
    edges = (0.0, 15.0, 30.0, 45.0, 60.0, 75.0, 90.0)
    shares = GOAL_HAZARD_SHARES
    full = float(sum(shares))                       # total mass over 0..90
    if full <= 0:
        return max(0.0, (total_minutes - minute) / 90.0)
    rem = 0.0
    for k in range(6):
        lo, hi = edges[k], edges[k + 1]
        rate = shares[k] / (hi - lo)                # mass per minute in this bucket
        a, b = max(lo, minute), min(hi, total_minutes)
        if b > a:
            rem += rate * (b - a)
    if total_minutes > 90.0:                        # injury time at the closing bucket's rate
        rem += (shares[5] / 15.0) * (total_minutes - 90.0)
    return rem / full


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


def _game_state_mult(home_goals: int, away_goals: int, red_home: int, red_away: int,
                     minute: float = 90.0, *, state_scaling: bool = True) -> tuple[float, float]:
    g_home, g_away = 1.0, 1.0
    # Red cards (each red compounds).
    g_home *= RED_CARD_LAMBDA_MULT ** max(0, red_home)
    g_away *= RED_CARD_LAMBDA_MULT ** max(0, red_away)
    # ④ A red to the TRAILING side bites extra — the comeback needs more attack, now a man down.
    if state_scaling:
        if red_home and home_goals < away_goals:
            g_home *= RED_TRAILER_EXTRA
        if red_away and away_goals < home_goals:
            g_away *= RED_TRAILER_EXTRA
    # Lead effect. Base multipliers are calibrated at a 1-goal lead at full time; ④ scales them
    # up with the goal margin and with how late it is (an early/narrow lead kills the game less).
    margin = abs(home_goals - away_goals)
    if margin > 0:
        ease, push = 1.0 - LEAD_LEADER_MULT, LEAD_TRAILER_MULT - 1.0     # 0.08 / 0.10
        if state_scaling:
            amp = (1.0 + LEAD_MARGIN_STEP * (margin - 1)) * min(1.0, minute / 90.0)
        else:
            amp = 1.0
        leader_mult, trailer_mult = 1.0 - ease * amp, 1.0 + push * amp
        if home_goals > away_goals:
            g_home *= leader_mult
            g_away *= trailer_mult
        else:
            g_away *= leader_mult
            g_home *= trailer_mult
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
    # ── refinement flags (defaults = refined model; set to the baseline values to revert) ──
    use_hazard: bool = True,              # ② rising goal-hazard vs flat remaining-time (validated: helps)
    xg_full_trust_min: float = XG_FULL_TRUST_MIN,  # ① minute at which xG earns full weight (<=0 → old full weight)
    residual_rho: float = RESIDUAL_DC_RHO,         # ③ Dixon-Coles rho on the residual joint (0 → independent; validated: helps fav-hit)
    # ④ margin/time-scaled lead + trailing-red bite. DEFAULT OFF: on the 2026 milestone backtest the
    # lead-scaling regressed Brier vs the fixed 0.92/1.10, and reds never occurred to test the red term.
    # Kept behind the flag for recalibration once more red/large-margin states accrue.
    state_scaling: bool = False,
) -> LiveMatchProb:
    """Live W/D/L + fair draw + remaining-goals distribution.

    ``lam_home/lam_away`` are the PRE-MATCH full-90' scoring intensities (from
    ``StrengthModel.pair_lambdas``); this function scales them by remaining time
    and game state. If live ``xg_home/xg_away`` are supplied, a side that is
    OUT-creating its pre-match expectation has its remaining lambda shaded up
    (intra-game stats driving the fair price, plan 03 §4b). Past 90'+injury, tau
    clamps to 0 (final score locked).

    Refinements (each individually revertible via the flags above):
      ① xG shading weight ramps with minutes played (25' of xG is mostly noise);
      ② remaining scoring scales by a rising goal-hazard, not flat time;
      ③ a Dixon-Coles rho corrects the low-score residual that prices the draw;
      ④ the lead effect scales with goal margin + time, and a trailing-side red bites extra;
      ⑤ real stoppage minutes flow in via ``injury_time`` (extends the remaining window).
    """
    total_minutes = 90.0 + max(0.0, injury_time)
    tau = max(0.0, (total_minutes - minute) / 90.0)   # linear remaining-time (exposed; consumers rely on it)
    # ② remaining scoring intensity: rising hazard when enabled, else the flat linear tau.
    scoring_rem = _remaining_scoring_fraction(minute, total_minutes) if use_hazard else tau
    g_home, g_away = _game_state_mult(home_goals, away_goals, red_home, red_away,
                                      minute, state_scaling=state_scaling)
    # Late level-draw correction: tempo dies when the score is level late, so the
    # remaining lambda decays faster than linear → lifts the late draw probability.
    if home_goals == away_goals and minute > LATE_LEVEL_FROM_MIN:
        ramp = min(1.0, (minute - LATE_LEVEL_FROM_MIN) / (90.0 - LATE_LEVEL_FROM_MIN))
        deflate = 1.0 - LATE_LEVEL_DEFLATE * ramp
        g_home *= deflate
        g_away *= deflate
    # ① xG performance shading: ratio of actual xG to pre-match-expected xG-so-far. The weight
    #    ramps from 0 at kickoff to XG_WEIGHT at xg_full_trust_min, so a noisy early read barely
    #    moves the fair price (a 25' xG earns ~40% of the weight, not the full 100%).
    if minute > 10:
        xw = XG_WEIGHT if xg_full_trust_min <= 0 else XG_WEIGHT * min(1.0, minute / xg_full_trust_min)
        exp_h, exp_a = lam_home * minute / 90.0, lam_away * minute / 90.0
        if xg_home is not None and exp_h > 0.1:
            g_home *= (1 - xw) + xw * min(2.0, xg_home / exp_h)
        if xg_away is not None and exp_a > 0.1:
            g_away *= (1 - xw) + xw * min(2.0, xg_away / exp_a)
    lh = lam_home * scoring_rem * g_home
    la = lam_away * scoring_rem * g_away

    # ③ Remaining-goal joint with a Dixon-Coles low-score correction on the residual (0-0/1-0/
    #    0-1/1-1) — the cells that decide the draw. residual_rho=0 recovers the independent case.
    m = score_matrix(lh, la, residual_rho, kmax)   # P(remaining_home=i, remaining_away=j)

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
