"""util/price_history.py — sample a historical price series at a target time (plan 18).

A milestone (kickoff + N minutes) maps to a wall-clock unix timestamp; we then take
the price from the nearest bar in a Kalshi candlestick / Polymarket prices-history
series. This makes a milestone's ¢ minute-accurate regardless of how jittery the
live polling was — the recorded value and the candlestick value should agree, and a
large gap is a data-quality flag.
"""
from __future__ import annotations


def price_at(series: list[dict], when_ts: int, *, key: str = "price",
             max_gap_s: int | None = 900):
    """Value of `series[*][key]` at the bar nearest `when_ts`.

    series: list of {"ts": <unix>, <key>: <float>} (Kalshi candlestick uses
    key='mid'/'ask'/'bid'/'last'; Poly prices_history uses key='price').
    Returns (value, bar_ts) for the closest bar, or (None, None) if the series is
    empty or the nearest bar is farther than max_gap_s (None disables the gap check).
    """
    if not series:
        return (None, None)
    best = min(series, key=lambda p: abs(p["ts"] - when_ts))
    if max_gap_s is not None and abs(best["ts"] - when_ts) > max_gap_s:
        return (None, None)
    return (best.get(key), best["ts"])


def kalshi_mid_series(candles: list[dict]) -> list[dict]:
    """Add a 'mid' = (ask+bid)/2 (or whichever side present) to candlestick bars."""
    from prediction_market.util.pricing import mid
    out = []
    for c in candles:
        out.append({**c, "mid": mid(c.get("ask"), c.get("bid"))})
    return out
