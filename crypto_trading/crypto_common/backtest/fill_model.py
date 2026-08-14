"""Realistic fill model on RECORDED tape (Plan 01 §8: "walk historical depth").

The candle-tape backtest assumes every passive order fills — optimistic. This
models maker fills against the DENSE recorded trade tape (~100k trades/day) so we
capture the two effects that actually decide whether the maker edge is real:

  1. FILL PROBABILITY — a post_only order fills only if the market later trades
     THROUGH our price; if price runs away we miss the trade entirely.
  2. ADVERSE SELECTION — we fill preferentially when the market comes to us (i.e.
     right as it's about to continue against us), which is the true cost of
     providing liquidity.

Queue model (conservative): when we post at price P we join the BACK of the queue,
so we fill only after cumulative same-or-better opposite-taker volume since we
posted exceeds ``queue_ahead`` (default = the resting size at the touch × a
fraction). ``queue_ahead=0`` recovers the optimistic "fills on first touch".

This is a model, not ground truth — 10s book snapshots + trade tape can't see
sub-10s queue dynamics. It is far closer to reality than synthetic-depth candle
fills, and its assumptions are explicit + swept.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class FillResult:
    filled: bool
    fill_ts: pd.Timestamp | None
    fill_price: float | None
    reason: str            # "filled" | "timeout_unfilled" | "crossed" | "no_trades"


def simulate_maker_fill(limit_price: float, side: str, post_ts: pd.Timestamp,
                        trades: pd.DataFrame, *, timeout: pd.Timedelta,
                        queue_ahead: float = 0.0) -> FillResult:
    """Will a post_only order at ``limit_price`` fill via the trade tape?

    ``side`` = "bid" (buy limit) fills when SELL-initiated trades (taker_side
    'ask') print at price ≤ limit; "ask" (sell limit) fills when BUY-initiated
    trades print at ≥ limit. Cumulative such volume must exceed ``queue_ahead``.
    ``trades`` is the tape (index=dt, cols price/count/taker_side).
    """
    # PIT: strictly AFTER post_ts — a print at the exact decision instant was part
    # of the information that triggered the order and cannot also fill it.
    window = trades.loc[post_ts:post_ts + timeout]
    window = window[window.index > post_ts]
    if window.empty:
        return FillResult(False, None, None, "no_trades")
    if side == "bid":
        crossing = window[(window.taker_side == "ask") & (window.price <= limit_price)]
    else:
        crossing = window[(window.taker_side == "bid") & (window.price >= limit_price)]
    if crossing.empty:
        return FillResult(False, None, None, "timeout_unfilled")
    cum = crossing["count"].cumsum()
    hit = cum[cum > queue_ahead]
    if hit.empty:
        return FillResult(False, None, None, "timeout_unfilled")   # queue never cleared
    fill_ts = hit.index[0]
    return FillResult(True, fill_ts, limit_price, "filled")        # maker fills AT our limit


def simulate_taker_fill(touch_price: float, side: str, ts: pd.Timestamp) -> FillResult:
    """Aggressive exit — cross the spread, fill immediately at the touch. Used to
    GUARANTEE an exit when a passive exit didn't fill in time (pay taker)."""
    return FillResult(True, ts, touch_price, "crossed")


def realized_vol_annual_from_mark(mark_underlying: pd.Series) -> float:
    """365-annualized realized vol of 1-min underlying returns (for sizing)."""
    r = mark_underlying.pct_change().dropna()
    if len(r) < 2:
        return 0.0
    return float(r.std() * np.sqrt(365 * 24 * 60))
