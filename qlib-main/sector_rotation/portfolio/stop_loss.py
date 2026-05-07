"""
stop_loss.py — Extreme-event stop-loss for sector rotation (Phase 1)
====================================================================

Three stop-loss types designed for long-only monthly rebalancing:

  1. Portfolio Circuit Breaker: SPY 3-day cumulative drop > 7%
     → halve ALL positions (don't fully exit — avoid selling at the bottom)

  2. Sector Collapse: single ETF drops > 12% from entry price
     → zero that sector

  3. Trailing Stop: single ETF drops > 15% from its peak since entry
     → zero that sector

Design principles:
  - Extreme events ONLY — normal -5% to -8% drops do NOT trigger
  - In 7 years (2018-2026), triggers ~3-5 times total (COVID, 2025 tariff shock)
  - Monthly rebalance already handles normal momentum decay
  - Circuit breaker halves, does NOT fully exit (COVID day 1 = -7.8%, day 3 = +9.4%)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
#  Data structures
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class SectorPositionState:
    """Per-sector position tracking for stop-loss evaluation."""
    ticker: str
    entry_date: pd.Timestamp
    entry_weight: float
    entry_price: float        # ETF price at entry (= cost_basis)
    peak_price: float         # trailing high since entry
    days_held: int = 0


@dataclass
class StopLossEvent:
    """Recorded when a stop-loss triggers."""
    date: pd.Timestamp
    ticker: str
    stop_type: str            # "portfolio_circuit" | "sector_collapse" | "trailing_stop"
    reason: str               # human-readable
    entry_price: float
    current_price: float
    threshold: float
    pnl_pct: float            # position PnL at trigger
    days_held: int
    spy_return_3d: float      # SPY context


# ═══════════════════════════════════════════════════════════════════════════
#  Position tracker (persists across rebalance dates)
# ═══════════════════════════════════════════════════════════════════════════

class SectorPositionTracker:
    """
    Track per-sector entry price, peak price, and days held.
    Updated at each rebalance date.
    """

    def __init__(self):
        self.positions: Dict[str, SectorPositionState] = {}
        self.cooling_off: Dict[str, pd.Timestamp] = {}  # ticker → stop date

    def update(
        self,
        rebalance_date: pd.Timestamp,
        weights: pd.Series,
        prices: pd.DataFrame,
    ):
        """Update position states based on current weights and prices."""
        for ticker in weights.index:
            w = float(weights.get(ticker, 0.0))
            price = float(prices[ticker].loc[:rebalance_date].dropna().iloc[-1]) \
                if ticker in prices.columns else 0.0

            if w > 1e-4 and price > 0:
                if ticker in self.positions:
                    # Existing position — update peak and days
                    pos = self.positions[ticker]
                    pos.peak_price = max(pos.peak_price, price)
                    pos.days_held = (rebalance_date - pos.entry_date).days
                else:
                    # New position — record entry
                    self.positions[ticker] = SectorPositionState(
                        ticker=ticker,
                        entry_date=rebalance_date,
                        entry_weight=w,
                        entry_price=price,
                        peak_price=price,
                        days_held=0,
                    )
            else:
                # Position exited (weight → 0)
                if ticker in self.positions:
                    del self.positions[ticker]

    def is_cooling_off(self, ticker: str, current_date: pd.Timestamp,
                       cooling_days: int = 10) -> bool:
        """Check if a sector is in cooling-off period after stop-loss."""
        if ticker not in self.cooling_off:
            return False
        days_since = (current_date - self.cooling_off[ticker]).days
        return days_since < cooling_days

    def record_stop(self, ticker: str, stop_date: pd.Timestamp):
        """Record stop-loss event for cooling-off tracking."""
        self.cooling_off[ticker] = stop_date
        if ticker in self.positions:
            del self.positions[ticker]

    def get_all_states(self) -> Dict[str, dict]:
        """Serialize all position states for recording."""
        return {
            ticker: {
                "entry_date": str(pos.entry_date.date()),
                "entry_price": round(pos.entry_price, 4),
                "peak_price": round(pos.peak_price, 4),
                "days_held": pos.days_held,
                "entry_weight": round(pos.entry_weight, 4),
            }
            for ticker, pos in self.positions.items()
        }


# ═══════════════════════════════════════════════════════════════════════════
#  Stop-loss checks
# ═══════════════════════════════════════════════════════════════════════════

def check_portfolio_circuit_breaker(
    spy_prices: pd.Series,
    rebalance_date: pd.Timestamp,
    spy_3d_limit: float = -0.07,
) -> Tuple[bool, float]:
    """
    Portfolio circuit breaker: SPY 3-day cumulative drop exceeds threshold.

    Returns (triggered, spy_3d_return).

    Historical triggers (2018-2026):
      2020-03-12: SPY 3d = -12.6% (COVID)
      2025-04-04: SPY 3d = -10.7% (tariff shock)
      2022-06-16: SPY 3d = -8.8% (bear market, borderline)
    """
    if spy_prices is None or spy_prices.empty:
        return False, 0.0

    spy_up_to = spy_prices.loc[:rebalance_date].dropna()
    if len(spy_up_to) < 4:
        return False, 0.0

    spy_3d_ret = float(spy_up_to.iloc[-1] / spy_up_to.iloc[-4] - 1)
    return spy_3d_ret < spy_3d_limit, spy_3d_ret


def check_sector_collapse(
    pos: SectorPositionState,
    current_price: float,
    max_dd_from_entry: float = -0.12,
) -> Tuple[bool, float]:
    """
    Single sector collapse: ETF dropped > threshold from entry price.

    Returns (triggered, pnl_pct).
    """
    if pos.entry_price <= 0 or current_price <= 0:
        return False, 0.0
    pnl_pct = current_price / pos.entry_price - 1
    return pnl_pct < max_dd_from_entry, pnl_pct


def check_trailing_stop(
    pos: SectorPositionState,
    current_price: float,
    max_dd_from_peak: float = -0.15,
) -> Tuple[bool, float]:
    """
    Trailing stop: ETF dropped > threshold from its peak since entry.

    Returns (triggered, dd_from_peak_pct).
    """
    if pos.peak_price <= 0 or current_price <= 0:
        return False, 0.0
    dd_from_peak = current_price / pos.peak_price - 1
    return dd_from_peak < max_dd_from_peak, dd_from_peak


# ═══════════════════════════════════════════════════════════════════════════
#  Main entry point
# ═══════════════════════════════════════════════════════════════════════════

def apply_position_stops(
    current_weights: pd.Series,
    position_tracker: SectorPositionTracker,
    sector_prices: pd.DataFrame,
    spy_prices: Optional[pd.Series],
    rebalance_date: pd.Timestamp,
    config: dict,
) -> Tuple[List[str], List[StopLossEvent], bool]:
    """
    Evaluate all stop-loss conditions.

    Returns:
      stopped_sectors: list of tickers to zero out
      events: list of StopLossEvent records
      halve_all: True if circuit breaker triggered (halve entire portfolio)
    """
    stopped_sectors: List[str] = []
    events: List[StopLossEvent] = []
    halve_all = False

    if not config.get("enabled", False):
        return stopped_sectors, events, halve_all

    cooling_days = config.get("cooling_off_days", 10)

    # Get SPY 3d return for context (used in all events)
    spy_3d_ret = 0.0
    if spy_prices is not None and not spy_prices.empty:
        spy_up_to = spy_prices.loc[:rebalance_date].dropna()
        if len(spy_up_to) >= 4:
            spy_3d_ret = float(spy_up_to.iloc[-1] / spy_up_to.iloc[-4] - 1)

    # ── 1. Portfolio Circuit Breaker ──────────────────────────────
    cb_cfg = config.get("portfolio_circuit_breaker", {})
    if cb_cfg.get("enabled", True):
        triggered, spy_3d = check_portfolio_circuit_breaker(
            spy_prices, rebalance_date,
            spy_3d_limit=cb_cfg.get("spy_3d_limit", -0.07),
        )
        if triggered:
            halve_all = True
            events.append(StopLossEvent(
                date=rebalance_date,
                ticker="PORTFOLIO",
                stop_type="portfolio_circuit",
                reason=f"SPY 3d return {spy_3d:.1%} < {cb_cfg.get('spy_3d_limit', -0.07):.0%} threshold",
                entry_price=0.0,
                current_price=0.0,
                threshold=cb_cfg.get("spy_3d_limit", -0.07),
                pnl_pct=spy_3d,
                days_held=0,
                spy_return_3d=spy_3d,
            ))
            log.warning(f"[STOP LOSS] Portfolio circuit breaker! SPY 3d={spy_3d:.1%}")

    # ── 2. Per-sector stops ───────────────────────────────────────
    sc_cfg = config.get("sector_collapse", {})
    ts_cfg = config.get("trailing_stop", {})

    for ticker, pos in list(position_tracker.positions.items()):
        w = float(current_weights.get(ticker, 0.0))
        if w < 1e-4:
            continue

        # Skip if in cooling-off
        if position_tracker.is_cooling_off(ticker, rebalance_date, cooling_days):
            continue

        # Get current price
        if ticker not in sector_prices.columns:
            continue
        price_series = sector_prices[ticker].loc[:rebalance_date].dropna()
        if price_series.empty:
            continue
        current_price = float(price_series.iloc[-1])

        # Update peak
        pos.peak_price = max(pos.peak_price, current_price)

        # 2a. Sector Collapse
        if sc_cfg.get("enabled", True):
            triggered, pnl_pct = check_sector_collapse(
                pos, current_price,
                max_dd_from_entry=sc_cfg.get("max_dd_from_entry", -0.12),
            )
            if triggered:
                stopped_sectors.append(ticker)
                position_tracker.record_stop(ticker, rebalance_date)
                events.append(StopLossEvent(
                    date=rebalance_date,
                    ticker=ticker,
                    stop_type="sector_collapse",
                    reason=f"{ticker} down {pnl_pct:.1%} from entry ${pos.entry_price:.2f}",
                    entry_price=pos.entry_price,
                    current_price=current_price,
                    threshold=sc_cfg.get("max_dd_from_entry", -0.12),
                    pnl_pct=pnl_pct,
                    days_held=pos.days_held,
                    spy_return_3d=spy_3d_ret,
                ))
                log.warning(f"[STOP LOSS] Sector collapse: {ticker} {pnl_pct:.1%} from entry")
                continue  # Don't check trailing if already stopped

        # 2b. Trailing Stop
        if ts_cfg.get("enabled", True):
            triggered, dd_pct = check_trailing_stop(
                pos, current_price,
                max_dd_from_peak=ts_cfg.get("max_dd_from_peak", -0.15),
            )
            if triggered:
                stopped_sectors.append(ticker)
                position_tracker.record_stop(ticker, rebalance_date)
                events.append(StopLossEvent(
                    date=rebalance_date,
                    ticker=ticker,
                    stop_type="trailing_stop",
                    reason=f"{ticker} down {dd_pct:.1%} from peak ${pos.peak_price:.2f}",
                    entry_price=pos.entry_price,
                    current_price=current_price,
                    threshold=ts_cfg.get("max_dd_from_peak", -0.15),
                    pnl_pct=current_price / pos.entry_price - 1,
                    days_held=pos.days_held,
                    spy_return_3d=spy_3d_ret,
                ))
                log.warning(f"[STOP LOSS] Trailing stop: {ticker} {dd_pct:.1%} from peak")

    return stopped_sectors, events, halve_all
