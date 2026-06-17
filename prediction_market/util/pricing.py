"""util/pricing.py — per-contract cents (¢) view (plan 18 §1).

Kalshi / Polymarket binary contracts settle at $1 (=100¢): YES wins → 100¢, loses → 0¢.
So the executable per-contract price in cents = quote(0–1) × 100. This is a PURE
display/conversion layer — nothing here changes any model number; it only translates
the 0–1 probabilities/quotes we already store into the ¢ unit the desk trades in.

  * a model/devig probability (0–1)  → its IMPLIED fair ¢
  * a venue ask/bid (0–1)            → the ¢ you actually pay / receive
  * a settled outcome                → 100¢ (won) / 0¢ (lost)

These three are distinct: the venue ¢ carries the vig/spread, the devig ¢ is the
market's fair view, the model ¢ is ours. We surface all of them side-by-side.
"""
from __future__ import annotations


def to_cents(q, ndigits: int = 1):
    """A 0–1 quote/probability → per-contract cents (¢). None-safe."""
    if q is None:
        return None
    return round(float(q) * 100.0, ndigits)


def mid(ask, bid):
    """Mid of an {ask, bid} pair (0–1). Falls back to whichever side is present."""
    if ask is None and bid is None:
        return None
    if ask is None:
        return float(bid)
    if bid is None:
        return float(ask)
    return (float(ask) + float(bid)) / 2.0


def mid_cents(ask, bid, ndigits: int = 1):
    """Per-contract mid price in ¢ from an {ask, bid} pair."""
    return to_cents(mid(ask, bid), ndigits)


def quote_to_cents(q3way: dict | None) -> dict | None:
    """{home:{ask,bid},draw:{...},away:{...}} → adds ask_c/bid_c/mid_c per side.

    Returns a NEW dict; the original 0–1 ask/bid are preserved untouched (ADD ONLY).
    """
    if not q3way:
        return None
    out: dict = {}
    for side in ("home", "draw", "away"):
        s = q3way.get(side)
        if not s:
            out[side] = s
            continue
        ask, bid = s.get("ask"), s.get("bid")
        out[side] = {**s, "ask_c": to_cents(ask), "bid_c": to_cents(bid), "mid_c": mid_cents(ask, bid)}
    # carry through any extra keys (e.g. 'devig') unchanged
    for k, v in q3way.items():
        if k not in out:
            out[k] = v
    return out


def model_cents(model: dict | None) -> dict | None:
    """{home,draw,away,...} probabilities → {home,draw,away} implied fair ¢."""
    if not model:
        return None
    return {s: to_cents(model.get(s)) for s in ("home", "draw", "away") if model.get(s) is not None}


def settle_cents(side: str, result: str | None):
    """FT settlement ¢ for a side: 100 if it won, 0 if it lost, None if undetermined."""
    if result is None:
        return None
    return 100.0 if side == result else 0.0


def pnl_cents(entry_cents, won: bool):
    """Per-contract realised P&L in ¢: won → 100−entry, lost → −entry."""
    if entry_cents is None:
        return None
    return round((100.0 - float(entry_cents)) if won else (-float(entry_cents)), 1)
