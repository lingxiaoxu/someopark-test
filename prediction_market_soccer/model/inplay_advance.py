"""In-play (live) 2-way ADVANCE model (plan 24 §2) — the knockout "who advances" twin of
model/inplay.py's regulation 3-way live model.

model/inplay.py::live_match_prob gives the live 90-MINUTE 3-way (home/draw/away). The per-match
KXWCGAME/fwc contract settles on that. But the SEPARATE 2-way advance market (Kalshi
KXWCADVANCE / Poly aadc-…-to-advance) settles on WHO ADVANCES — regulation, then extra time,
then penalties. This module computes that live, period-aware, recomputed every poll cycle (same
cadence as live_match_prob):

    P(home advances) =
        period "reg":  P(home win 90') + P(draw 90') · P(home wins ET+pens | level at 90')
        period "et" :  P(home ahead after ET) + P(level after ET) · P(home win shootout)
        period "pens": P(home win shootout)   (live-conditioned on the running tally if known)

It REUSES live_match_prob for the regulation 3-way (so the two models share identical
state-adjusted λ — red cards, lead effect, late-level deflation, xG shading) and layers ET +
penalties on top. ET is modelled with FATIGUE (λ decays through ET) and an optional GOLDEN-GOAL
mode (OFF by default — modern WC plays the full 30'). Penalties use the sequential shootout DP
in model/penalties.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import exp

import numpy as np

from prediction_market_soccer.model.dixon_coles import score_matrix
from prediction_market_soccer.model.inplay import RED_CARD_LAMBDA_MULT, live_match_prob

# ET / fatigue defaults (plan 24 §2.3).
ET_FRACTION = 30.0 / 90.0      # extra time is 30' of a 90' match's scoring window
FATIGUE_K = 0.20              # λ reduction by end of ET (tired legs); avg over the window applied
ET_GOLDEN_GOAL = False        # modern WC ET is full 30'; toggle on only for golden-goal rules


@dataclass(frozen=True)
class LiveAdvanceProb:
    minute: int
    period: str                 # "reg" | "et" | "pens"
    p_home_advance: float
    p_away_advance: float
    p_reg_decides: float        # P(tie decided in regulation) — transparency
    p_et_decides: float         # P(decided in extra time | reached ET)
    p_pens_decides: float       # P(goes to penalties)
    lam_home_et: float
    lam_away_et: float
    # Regulation context carried through so the 2-way tactics can read totals/state from the
    # SAME object (totals settle at 90' regardless of the advance market). Empty/zero in
    # et/pens periods (the totals market has already settled).
    home_goals: int = 0
    away_goals: int = 0
    tau: float = 0.0
    exp_remaining_goals: float = 0.0
    p_over_total: dict = field(default_factory=dict)


def _et_lambdas(lam_home: float, lam_away: float, red_home: int, red_away: int,
                remaining_frac: float, fatigue_k: float) -> tuple[float, float]:
    """ET scoring intensities: base 90' λ scaled to the (remaining) ET window, carrying the
    persistent red-card penalty, reduced by an average fatigue factor over the ET window."""
    fatigue = 1.0 - fatigue_k * 0.5     # average of a 0→fatigue_k ramp across the window
    gh = RED_CARD_LAMBDA_MULT ** max(0, red_home)
    ga = RED_CARD_LAMBDA_MULT ** max(0, red_away)
    lh = max(1e-9, lam_home * ET_FRACTION * remaining_frac * fatigue * gh)
    la = max(1e-9, lam_away * ET_FRACTION * remaining_frac * fatigue * ga)
    return lh, la


def _advance_after_et(lam_home: float, lam_away: float, shootout_home: float,
                      et_home: int, et_away: int, remaining_frac: float, *,
                      red_home: int, red_away: int, fatigue_k: float, golden_goal: bool,
                      rho: float, kmax: int) -> tuple[float, float]:
    """P(home advances) entering/continuing ET with current ET aggregate (et_home, et_away)
    and `remaining_frac` of ET left. Returns (p_home_advance, p_goes_to_pens)."""
    lh, la = _et_lambdas(lam_home, lam_away, red_home, red_away, remaining_frac, fatigue_k)
    if golden_goal and et_home == et_away:
        # First ET goal wins; no goal → penalties. (Only well-defined from level.)
        p_any = 1.0 - exp(-(lh + la))
        p_home_first = lh / (lh + la) if (lh + la) > 0 else 0.5
        p_pens = 1.0 - p_any
        p_home_adv = p_any * p_home_first + p_pens * shootout_home
        return p_home_adv, p_pens
    m = score_matrix(lh, la, rho, kmax)
    i = np.arange(kmax + 1)[:, None]
    j = np.arange(kmax + 1)[None, :]
    diff = (et_home + i) - (et_away + j)
    p_home = float(m[diff > 0].sum())
    p_level = float(m[diff == 0].sum())     # → penalties
    p_home_adv = p_home + p_level * shootout_home
    return p_home_adv, p_level


def live_advance_prob(
    lam_home: float,
    lam_away: float,
    minute: int,
    home_goals: int,
    away_goals: int,
    *,
    period: str = "reg",
    red_home: int = 0,
    red_away: int = 0,
    injury_time: float = 0.0,
    xg_home: float | None = None,
    xg_away: float | None = None,
    et_home_goals: int = 0,
    et_away_goals: int = 0,
    shootout_home: float = 0.5,
    et_then_pens: bool = True,
    fatigue_k: float = FATIGUE_K,
    golden_goal: bool = ET_GOLDEN_GOAL,
    rho: float = -0.05,
    kmax: int = 8,
) -> LiveAdvanceProb:
    """Live P(home advances) / P(away advances) for a knockout tie.

    ``lam_home/lam_away`` are the PRE-MATCH full-90' intensities (StrengthModel.pair_lambdas,
    knockout=True). ``shootout_home`` is P(home wins the shootout) — pass the detailed value
    from model.penalties.shootout_win_prob_detailed(...). ``period`` follows the API-Football
    status: regulation ("reg"), extra time ("et"), penalties ("pens"). For "et",
    ``et_home_goals/et_away_goals`` is the ET-only aggregate since 90'. For "pens", pass
    ``shootout_home`` already conditioned on the running tally (the caller has the conversions).
    """
    if period == "pens":
        p_home_adv = shootout_home
        return LiveAdvanceProb(minute=minute, period="pens",
                               p_home_advance=p_home_adv, p_away_advance=1.0 - p_home_adv,
                               p_reg_decides=0.0, p_et_decides=0.0, p_pens_decides=1.0,
                               lam_home_et=0.0, lam_away_et=0.0,
                               home_goals=home_goals, away_goals=away_goals,
                               tau=0.0, exp_remaining_goals=0.0, p_over_total={})

    if period == "et":
        remaining = max(0.0, (120.0 - max(90.0, minute)) / 30.0)
        p_home_adv, p_pens = _advance_after_et(
            lam_home, lam_away, shootout_home, et_home_goals, et_away_goals, remaining,
            red_home=red_home, red_away=red_away, fatigue_k=fatigue_k, golden_goal=golden_goal,
            rho=rho, kmax=kmax)
        lh, la = _et_lambdas(lam_home, lam_away, red_home, red_away, remaining, fatigue_k)
        return LiveAdvanceProb(minute=minute, period="et",
                               p_home_advance=p_home_adv, p_away_advance=1.0 - p_home_adv,
                               p_reg_decides=0.0, p_et_decides=1.0 - p_pens, p_pens_decides=p_pens,
                               lam_home_et=lh, lam_away_et=la,
                               home_goals=home_goals, away_goals=away_goals,
                               tau=remaining, exp_remaining_goals=float(lh + la), p_over_total={})

    # period == "reg": regulation 3-way (reuse live_match_prob), then layer the
    # competition's OWN tie-break on the draw. UEFA plays extra time first;
    # CONMEBOL ties go straight to penalties (Competition.et_in_ties in the
    # registry). Assuming extra time everywhere gave a Libertadores or Sudamericana
    # decider 30 minutes of scoring that the rules do not grant, which moves the
    # advance probability toward the stronger side — exactly the wrong direction,
    # since a shootout is the great equaliser.
    lp = live_match_prob(lam_home, lam_away, minute, home_goals, away_goals,
                         red_home=red_home, red_away=red_away, injury_time=injury_time,
                         xg_home=xg_home, xg_away=xg_away, kmax=kmax)
    # Conditional on a level result at 90' (fresh ET from 0-0), full ET remaining.
    if et_then_pens:
        p_adv_if_level, p_pens_if_level = _advance_after_et(
            lam_home, lam_away, shootout_home, 0, 0, 1.0,
            red_home=red_home, red_away=red_away, fatigue_k=fatigue_k, golden_goal=golden_goal,
            rho=rho, kmax=kmax)
    else:
        p_adv_if_level, p_pens_if_level = shootout_home, 1.0
    p_home_adv = lp.p_home + lp.p_draw * p_adv_if_level
    lh, la = _et_lambdas(lam_home, lam_away, red_home, red_away, 1.0, fatigue_k)
    return LiveAdvanceProb(
        minute=minute, period="reg",
        p_home_advance=p_home_adv, p_away_advance=1.0 - p_home_adv,
        p_reg_decides=lp.p_home + lp.p_away, p_et_decides=lp.p_draw * (1.0 - p_pens_if_level),
        p_pens_decides=lp.p_draw * p_pens_if_level,
        lam_home_et=lh, lam_away_et=la,
        home_goals=home_goals, away_goals=away_goals, tau=lp.tau,
        exp_remaining_goals=lp.exp_remaining_goals, p_over_total=lp.p_over_total)


def live_advance_from_strength(sm, home_id: str, away_id: str, minute: int,
                               home_goals: int, away_goals: int, **kw) -> LiveAdvanceProb:
    """Convenience: pre-match knockout lambdas + detailed shootout prob from the strength
    model, then go live. Extra kwargs forward to live_advance_prob (period, reds, et goals…)."""
    from prediction_market_soccer.model.penalties import shootout_win_prob_detailed
    lam_h, lam_a = sm.pair_lambdas(home_id, away_id, knockout=True, host_neutral=True)
    shootout_home = kw.pop("shootout_home", None)
    if shootout_home is None:
        shootout_home = shootout_win_prob_detailed(sm, home_id, away_id)
    return live_advance_prob(lam_h, lam_a, minute, home_goals, away_goals,
                             shootout_home=shootout_home, **kw)


if __name__ == "__main__":
    from prediction_market_soccer.ingest.club_prior import load_prior
    from prediction_market_soccer.model.strength import build_strength

    sm = build_strength(load_prior())
    print("Brazil vs Japan — live P(Brazil advances), evolving from 0-0:")
    for minute in (1, 45, 75, 89):
        la = live_advance_from_strength(sm, "brazil", "japan", minute, 0, 0)
        print(f"  {minute:>2}'  adv={la.p_home_advance:.3f}  "
              f"(reg {la.p_reg_decides:.2f} / et {la.p_et_decides:.2f} / pens {la.p_pens_decides:.2f})")
    print("\nLevel late → into ET → penalties:")
    et = live_advance_from_strength(sm, "brazil", "japan", 105, 0, 0, period="et",
                                    et_home_goals=0, et_away_goals=0)
    print(f"  105' ET 0-0: adv={et.p_home_advance:.3f} (pens {et.p_pens_decides:.2f})")
    pen = live_advance_from_strength(sm, "brazil", "japan", 120, 0, 0, period="pens")
    print(f"  penalties:   adv={pen.p_home_advance:.3f}")
