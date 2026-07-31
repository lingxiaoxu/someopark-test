"""strategy/devig.py — two devig paths (PLAN §11, §18.2-2).

* categorical: multiplicative normalisation of the leg asks (sum to 1)
* cumulative threshold ladder: per-leg two-sided devig (mid of bid/ask when both exist,
  else single side), then ISOTONIC regression to enforce a monotone survival curve in
  strike; differencing yields the market-implied pmf. Monotonicity violations BEFORE
  correction are returned — they are riskless arbitrage signals (§11 consistency).
"""
from __future__ import annotations

import numpy as np


def devig_categorical(asks: dict[str, float]) -> dict[str, float]:
    tot = sum(asks.values())
    assert tot > 0, "empty book"
    return {k: v / tot for k, v in asks.items()}


def _leg_prob(yes_bid: float | None, yes_ask: float | None) -> float | None:
    """Two-sided de-vigged probability of one binary leg (mid; one-sided books tolerated)."""
    if yes_bid is not None and yes_ask is not None:
        return (yes_bid + yes_ask) / 2.0
    if yes_ask is not None:
        return max(yes_ask - 0.01, 0.0)          # ask-only: assume ~1c half-spread
    if yes_bid is not None:
        return min(yes_bid + 0.01, 1.0)
    return None


def isotonic_decreasing(xs: list[float], ys: list[float]) -> list[float]:
    """PAV: best monotone non-increasing fit of ys ordered by ascending xs."""
    y = [-v for v in ys]                          # fit non-decreasing on negated values
    w = [1.0] * len(y)
    blocks: list[list[float]] = []                # [sum_wy, sum_w]
    for yi, wi in zip(y, w):
        blocks.append([yi * wi, wi])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            b = blocks.pop()
            blocks[-1][0] += b[0]
            blocks[-1][1] += b[1]
    out = []
    for swy, sw in blocks:
        out += [swy / sw] * int(round(sw))
    return [-v for v in out]


def ladder_implied(legs: list[dict]) -> dict:
    """legs: [{strike, yes_bid, yes_ask}] (any order). Returns
    {strikes, survival (isotonic), pmf (differenced incl. tails), violations}."""
    rows = sorted((l for l in legs if l.get("strike") is not None
                   and _leg_prob(l.get("yes_bid"), l.get("yes_ask")) is not None),
                  key=lambda l: l["strike"])
    if len(rows) < 2:
        return {"strikes": [], "survival": [], "pmf": {}, "violations": []}
    xs = [float(l["strike"]) for l in rows]
    raw = [float(_leg_prob(l.get("yes_bid"), l.get("yes_ask"))) for l in rows]
    # arbitrage: survival must be non-increasing — a strictly tradable violation is
    # ask(low strike) < bid(high strike)
    violations = []
    for i in range(len(rows) - 1):
        lo, hi = rows[i], rows[i + 1]
        if lo.get("yes_ask") is not None and hi.get("yes_bid") is not None \
                and lo["yes_ask"] < hi["yes_bid"] - 1e-9:
            violations.append({"buy": lo, "sell": hi,
                               "gross": round(hi["yes_bid"] - lo["yes_ask"], 4)})
    surv = isotonic_decreasing(xs, raw)
    surv = [min(max(v, 0.0), 1.0) for v in surv]
    pmf: dict[float, float] = {}
    prev = 1.0
    for x, sv in zip(xs, surv):
        pmf[x] = max(prev - sv, 0.0)              # mass at/below this strike since last
        prev = sv
    pmf[float("inf")] = max(prev, 0.0)            # above the top strike
    return {"strikes": xs, "survival": surv, "pmf": pmf, "violations": violations}


def bucket_implied(legs: list[dict]) -> dict:
    """Between-bucket markets (KXWTIW: mutually exclusive '$X.00 to $X.99' legs + open
    tails). Devig = multiplicative normalisation of the per-leg two-sided mids (the
    categorical rule — buckets partition the outcome space). pmf is keyed by the bucket's
    lower edge (tail 'less' legs by -inf). Violations here are the PARTITION arbs:
    sum(asks) < 1 (buy every YES) / sum(bids) > 1 (buy every NO)."""
    rows = [l for l in legs
            if _leg_prob(l.get("yes_bid"), l.get("yes_ask")) is not None]
    if len(rows) < 2:
        return {"strikes": [], "survival": [], "pmf": {}, "violations": []}
    raw = {l["ticker"]: float(_leg_prob(l.get("yes_bid"), l.get("yes_ask"))) for l in rows}
    tot = sum(raw.values())
    violations = []
    asks = [l["yes_ask"] for l in rows if l.get("yes_ask") is not None]
    bids = [l["yes_bid"] for l in rows if l.get("yes_bid") is not None]
    if len(asks) == len(rows) and sum(asks) < 1.0 - 1e-9:
        violations.append({"kind": "buy_all_yes", "gross": round(1.0 - sum(asks), 4)})
    if len(bids) == len(rows) and sum(bids) > 1.0 + 1e-9:
        violations.append({"kind": "buy_all_no", "gross": round(sum(bids) - 1.0, 4)})
    def _edge(l):
        if l.get("strike") is not None:
            return float(l["strike"])
        return float("-inf")                       # 'less' tail
    pmf = {_edge(l): raw[l["ticker"]] / tot for l in rows} if tot > 0 else {}
    return {"strikes": sorted(pmf), "survival": [], "pmf": pmf, "violations": violations}
