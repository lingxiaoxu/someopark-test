"""Penalty-shootout model (plan 03 §4b, 15) — team-specific, NOT a flat average.

Research (1067 European cup shootouts + international studies) shows shootouts are
far from coin tosses:
  * **team quality** dominates — top-market-value quartile teams win ~60%;
  * **kick order** matters — the team kicking first wins ~59% (ABAB), but the
    order is a coin toss, so it widens the distribution without shifting the
    expectation;
  * **reputation / history** — some nations are persistently strong (Germany,
    Argentina, Croatia) or weak (historically England, Netherlands) at shootouts,
    beyond their general quality.

So instead of a flat ``penalty_favorite_edge`` we compute a per-pair shootout
win probability from the rating gap + a small reputation overlay. The kick-order
coin-flip is modelled as added variance (it averages out in the binary "advance"
expectation but matters for path/scenario risk).
"""
from __future__ import annotations

import math

from prediction_market.model.strength import StrengthModel

# Reputation overlay (additive to the favourite's shootout edge), from historical
# shootout records. Small, documented priors — refine with a shootout dataset.
SHOOTOUT_REPUTATION: dict[str, float] = {
    "germany": +0.05, "argentina": +0.05, "croatia": +0.04, "brazil": +0.02,
    "uruguay": +0.02, "france": +0.01,
    "england": -0.02, "netherlands": -0.03, "spain": -0.01, "italy": +0.01,
}

# How strongly the rating gap maps to a shootout edge (gentler than open play —
# shootouts compress quality, but quality still tilts it, research-backed).
_SO_BETA = 0.45
_SO_MIN, _SO_MAX = 0.35, 0.65          # clamp: even a mismatch is ~65/35, not 90/10
KICK_ORDER_FIRST_EDGE = 0.095          # ~59.5% first-kicker advantage (variance)


def shootout_win_prob(sm: StrengthModel, team_a: str, team_b: str) -> float:
    """P(team_a wins a shootout vs team_b): quality gap + reputation, clamped."""
    ra, rb = sm.ratings.get(team_a, 0.0), sm.ratings.get(team_b, 0.0)
    quality = 0.5 + 0.5 * math.tanh(_SO_BETA * (ra - rb))
    rep = SHOOTOUT_REPUTATION.get(team_a, 0.0) - SHOOTOUT_REPUTATION.get(team_b, 0.0)
    return max(_SO_MIN, min(_SO_MAX, quality + rep))


def kick_order_band(p_shootout: float) -> tuple[float, float]:
    """The (first-kicker, second-kicker) shootout probabilities for ``p_shootout``.

    The coin toss decides which applies. Used for scenario/variance analysis
    (e.g. a favourite who loses the toss is meaningfully more vulnerable).
    """
    first = min(0.95, p_shootout + KICK_ORDER_FIRST_EDGE / 2)
    second = max(0.05, p_shootout - KICK_ORDER_FIRST_EDGE / 2)
    return first, second


if __name__ == "__main__":
    from prediction_market.ingest.prior_ingest import load_prior
    from prediction_market.model.strength import build_strength

    sm = build_strength(load_prior())
    pairs = [("germany", "england"), ("brazil", "croatia"), ("france", "argentina"),
             ("spain", "netherlands"), ("argentina", "brazil")]
    print("shootout win prob (team A vs B): quality + reputation, NOT flat 53%")
    for a, b in pairs:
        p = shootout_win_prob(sm, a, b)
        first, second = kick_order_band(p)
        print(f"  {a:<10} vs {b:<12} P(A)={p:.3f}  (kick-order: {first:.2f} if A kicks first / {second:.2f} if second)")
