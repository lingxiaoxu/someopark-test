"""strategy/edge.py + fees + sizing (PLAN §11, §18.2-3).

Enumerates STRUCTURES, not just legs, and BOTH sides of every leg:
  * single YES  (buy yes at ask)
  * single NO   (buy no at 1 - yes_bid)
  * adjacent bucket spread: YES(>k_i) + NO(>k_j), j>i — payoff = 1 + 1{print in bucket};
    effective bucket price = combined cost - 1

Fees (measured Kalshi schedule): taker fee per EXECUTED trade = ceil_cents(0.07·C·P·(1-P));
settlement is free → hold-to-settle pays entry legs only.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


def taker_fee(price: float, count: int) -> float:
    if count <= 0 or not (0 < price < 1):
        return 0.0
    return math.ceil(round(0.07 * count * price * (1 - price), 6) * 100) / 100.0


@dataclass(frozen=True)
class Leg:
    ticker: str
    side: str          # "yes" | "no"
    price: float       # cost per contract in $
    depth: float       # $ available at that price


@dataclass(frozen=True)
class Struct:
    kind: str          # "single" | "bucket"
    legs: tuple[Leg, ...]
    fair: float        # probability of the paying event (bucket: P(bucket))
    cost: float        # effective binary price in $ (bucket: sum(cost)-1)
    max_loss: float    # per-contract worst case in $
    desc: str

    def net_edge(self, count: int = 1) -> float:
        fee = sum(taker_fee(l.price, count) for l in self.legs) / max(count, 1)
        return self.fair - self.cost - fee


def enumerate_structs(legs_meta: list[dict], pmf_fair, strict: bool) -> list[Struct]:
    """legs_meta: [{ticker, strike, cap_strike, strike_type, yes_bid, yes_ask, bid_depth,
    ask_depth}]. pmf_fair: settlement-grid pmf from OUR model. Cumulative 'greater' legs
    price off survival(strict = series rule); between/less legs (KXWTIW buckets + tails)
    price off leg_fair with their own strike metadata."""
    from prediction_market_macro.model.common import leg_fair, survival
    out: list[Struct] = []
    rows = sorted([l for l in legs_meta if l.get("strike") is not None
                   and l.get("strike_type") not in ("between", "less", "less_or_equal")],
                  key=lambda l: l["strike"])
    for l in legs_meta:
        if l.get("strike_type") not in ("between", "less", "less_or_equal"):
            continue
        fair = leg_fair(pmf_fair, l["strike_type"], l.get("strike"), l.get("cap_strike"))
        rng = (f"[{l['strike']:g},{l['cap_strike']:g}]" if l["strike_type"] == "between"
               else f"<{l['cap_strike']:g}")
        if l.get("yes_ask") is not None and 0 < l["yes_ask"] < 1:
            out.append(Struct("single", (Leg(l["ticker"], "yes", l["yes_ask"], l["ask_depth"]),),
                              fair=fair, cost=l["yes_ask"], max_loss=l["yes_ask"],
                              desc=f"YES {l['ticker']} {rng} @{l['yes_ask']:.2f}"))
        if l.get("yes_bid") is not None and 0 < l["yes_bid"] < 1:
            no_price = round(1 - l["yes_bid"], 4)
            out.append(Struct("single", (Leg(l["ticker"], "no", no_price, l["bid_depth"]),),
                              fair=1 - fair, cost=no_price, max_loss=no_price,
                              desc=f"NO {l['ticker']} {rng} @{no_price:.2f}"))
    for l in rows:
        sv = survival(pmf_fair, float(l["strike"]), strict=strict)
        if l.get("yes_ask") is not None and 0 < l["yes_ask"] < 1:
            out.append(Struct("single", (Leg(l["ticker"], "yes", l["yes_ask"], l["ask_depth"]),),
                              fair=sv, cost=l["yes_ask"], max_loss=l["yes_ask"],
                              desc=f"YES {l['ticker']} @{l['yes_ask']:.2f}"))
        if l.get("yes_bid") is not None and 0 < l["yes_bid"] < 1:
            no_price = round(1 - l["yes_bid"], 4)
            out.append(Struct("single", (Leg(l["ticker"], "no", no_price, l["bid_depth"]),),
                              fair=1 - sv, cost=no_price, max_loss=no_price,
                              desc=f"NO {l['ticker']} @{no_price:.2f}"))
    for width in (1, 2):                                # adjacent + 2-wide (§11)
        for i in range(len(rows) - width):
            lo, hi = rows[i], rows[i + width]
            if lo.get("yes_ask") is None or hi.get("yes_bid") is None:
                continue
            no_hi = round(1 - hi["yes_bid"], 4)
            cost = lo["yes_ask"] + no_hi
            eff = cost - 1.0                            # effective bucket price
            if not (0.005 < eff < 0.995):
                continue
            p_bucket = (survival(pmf_fair, float(lo["strike"]), strict=strict)
                        - survival(pmf_fair, float(hi["strike"]), strict=strict))
            out.append(Struct(
                "bucket",
                (Leg(lo["ticker"], "yes", lo["yes_ask"], lo["ask_depth"]),
                 Leg(hi["ticker"], "no", no_hi, hi["bid_depth"])),
                fair=p_bucket, cost=eff, max_loss=eff,
                desc=f"BUCKET ({lo['strike']:g},{hi['strike']:g}] @{eff:.2f}"))
    return out


def quarter_kelly_usd(fair: float, cost: float, bankroll: float, cap: float = 1.0) -> float:
    """Kelly fraction for a binary bought at `cost` paying $1: f* = (p - c)/(1 - c)."""
    if not (0 < cost < 1) or fair <= cost:
        return 0.0
    f = (fair - cost) / (1 - cost) / 4.0
    return round(min(cap, max(0.0, f * bankroll)), 2)
