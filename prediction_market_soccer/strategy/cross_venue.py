"""Cross-venue relative value & locked arbitrage math (plan 08).

Two venues (Kalshi ↔ Polymarket US) price the same real outcome differently. Two
trade types:
  (A) **Locked arbitrage** when both settle identically — buy YES on the cheap
      venue + buy NO (= sell YES) on the expensive venue; exactly one leg pays $1.
  (B) **Statistical relative value** when settlement differs / single-sided —
      each leg stands on its own model edge.

This module is PURE MATH (no execution; execution + leg-risk are 🔒 until venue
credentials exist). It also scans single-venue mutually-exclusive baskets for the
"<$1 YES basket" free money (plan 09 §4).

CRITICAL SAFETY (plan 08 §2): locked arbitrage is only valid when settlement
equivalence is verified (`equiv_verified`). The caller must pass that flag; we
refuse to flag a lock as tradable otherwise.
"""
from __future__ import annotations

from dataclasses import dataclass

from prediction_market_soccer.config import CONFIG


@dataclass(frozen=True)
class LockArb:
    p_cheap_yes: float          # YES ask on the cheap venue
    p_expensive_yes: float      # YES ask on the expensive venue (we buy its NO)
    gross_lock: float           # probability gap = profit before costs
    net_lock: float             # after fees + slippage + tie risk + capital carry
    equiv_verified: bool
    tradable: bool

    @property
    def two_leg_cost(self) -> float:
        return self.p_cheap_yes + (1.0 - self.p_expensive_yes)


def evaluate_lock(
    p_cheap_yes: float,
    p_expensive_yes: float,
    *,
    equiv_verified: bool,
    fee_cheap: float = 0.0,
    fee_expensive: float = 0.0,
    slippage: float = 0.0,
    tie_risk_adj: float = 0.0,
    capital_carry: float = 0.0,
    theta_arb: float | None = None,
) -> LockArb:
    """Net locked-arb edge for a candidate pair (plan 08 §3).

        gross_lock = p_expensive_yes - p_cheap_yes
        net_lock   = gross_lock - fees - slippage - tie_risk - capital_carry

    Tradable only if net_lock >= theta_arb AND settlement equivalence verified.
    """
    theta_arb = CONFIG.risk.min_net_lock if theta_arb is None else theta_arb
    gross = p_expensive_yes - p_cheap_yes
    net = gross - fee_cheap - fee_expensive - slippage - tie_risk_adj - capital_carry
    tradable = bool(equiv_verified and net >= theta_arb and gross > 0)
    return LockArb(
        p_cheap_yes=p_cheap_yes, p_expensive_yes=p_expensive_yes,
        gross_lock=gross, net_lock=net, equiv_verified=equiv_verified, tradable=tradable,
    )


@dataclass(frozen=True)
class BasketArb:
    yes_ask_sum: float          # sum of YES asks + fees across the exclusive set
    profit: float               # 1 - cost; positive => guaranteed money
    tradable: bool


def scan_yes_basket(yes_asks: list[float], fees: list[float] | None = None,
                    *, theta: float | None = None) -> BasketArb:
    """Mutually-exclusive YES basket < $1 → buy all, exactly one pays $1 (plan 09 §4).

    ``yes_asks`` are the YES ask prices of every outcome in one exclusive event.
    """
    theta = CONFIG.risk.min_net_lock if theta is None else theta
    fees = fees or [0.0] * len(yes_asks)
    cost = sum(a + f for a, f in zip(yes_asks, fees))
    profit = 1.0 - cost
    return BasketArb(yes_ask_sum=cost, profit=profit, tradable=profit >= theta)
