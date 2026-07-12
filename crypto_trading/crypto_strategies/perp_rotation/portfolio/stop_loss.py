"""stop_loss.py — Extreme-event stop-loss for perp rotation (Plan 05 §7).

COPIED from qlib-main/sector_rotation/portfolio/stop_loss.py (read-only
template). Mechanics preserved verbatim: position tracker with cooling-off,
portfolio circuit breaker (benchmark 3-day crash → halve all), per-position
collapse stop (from entry) and trailing stop (from peak).

Adaptations (plan "Change" column only): SPY → KXBTCPERP benchmark naming;
thresholds widened for crypto daily vol (3d circuit −15%, collapse −20%,
trailing −25% — starting points, calibrate; the config surface is identical).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import pandas as pd

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
#  Data structures — template verbatim
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class SectorPositionState:
    """Per-perp position tracking for stop-loss evaluation."""
    ticker: str
    entry_date: pd.Timestamp
    entry_weight: float
    entry_price: float
    peak_price: float
    days_held: int = 0


@dataclass
class StopLossEvent:
    """Recorded when a stop-loss triggers."""
    date: pd.Timestamp
    ticker: str
    stop_type: str            # "portfolio_circuit" | "sector_collapse" | "trailing_stop"
    reason: str
    entry_price: float
    current_price: float
    threshold: float
    pnl_pct: float
    days_held: int
    benchmark_return_3d: float   # KXBTCPERP context (template: spy_return_3d)


# ═══════════════════════════════════════════════════════════════════════════
#  Position tracker — template verbatim
# ═══════════════════════════════════════════════════════════════════════════

class SectorPositionTracker:
    """Track per-perp entry price, peak price, and days held."""

    def __init__(self):
        self.positions: Dict[str, SectorPositionState] = {}
        self.cooling_off: Dict[str, pd.Timestamp] = {}

    def update(self, rebalance_date: pd.Timestamp, weights: pd.Series,
               prices: pd.DataFrame):
        for ticker in weights.index:
            w = float(weights.get(ticker, 0.0))
            price = float(prices[ticker].loc[:rebalance_date].dropna().iloc[-1]) \
                if ticker in prices.columns and not prices[ticker].loc[:rebalance_date].dropna().empty else 0.0

            if w > 1e-4 and price > 0:
                if ticker in self.positions:
                    pos = self.positions[ticker]
                    pos.peak_price = max(pos.peak_price, price)
                    pos.days_held = (rebalance_date - pos.entry_date).days
                else:
                    self.positions[ticker] = SectorPositionState(
                        ticker=ticker, entry_date=rebalance_date, entry_weight=w,
                        entry_price=price, peak_price=price, days_held=0)
            else:
                if ticker in self.positions:
                    del self.positions[ticker]

    def is_cooling_off(self, ticker: str, current_date: pd.Timestamp,
                       cooling_days: int = 10) -> bool:
        if ticker not in self.cooling_off:
            return False
        days_since = (current_date - self.cooling_off[ticker]).days
        return days_since < cooling_days

    def record_stop(self, ticker: str, stop_date: pd.Timestamp):
        self.cooling_off[ticker] = stop_date
        if ticker in self.positions:
            del self.positions[ticker]

    def get_all_states(self) -> Dict[str, dict]:
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
#  Stop-loss checks — template verbatim, crypto default thresholds
# ═══════════════════════════════════════════════════════════════════════════

def check_portfolio_circuit_breaker(
    benchmark_prices: pd.Series,
    rebalance_date: pd.Timestamp,
    benchmark_3d_limit: float = -0.15,
) -> Tuple[bool, float]:
    """Benchmark (KXBTCPERP) 3-day cumulative drop beyond the limit."""
    if benchmark_prices is None or benchmark_prices.empty:
        return False, 0.0

    up_to = benchmark_prices.loc[:rebalance_date].dropna()
    if len(up_to) < 4:
        return False, 0.0

    ret_3d = float(up_to.iloc[-1] / up_to.iloc[-4] - 1)
    return ret_3d < benchmark_3d_limit, ret_3d


def check_sector_collapse(
    pos: SectorPositionState,
    current_price: float,
    max_dd_from_entry: float = -0.20,
) -> Tuple[bool, float]:
    """Single perp dropped > threshold from entry."""
    if pos.entry_price <= 0 or current_price <= 0:
        return False, 0.0
    pnl_pct = current_price / pos.entry_price - 1
    return pnl_pct < max_dd_from_entry, pnl_pct


def check_trailing_stop(
    pos: SectorPositionState,
    current_price: float,
    max_dd_from_peak: float = -0.25,
) -> Tuple[bool, float]:
    """Single perp dropped > threshold from its peak since entry."""
    if pos.peak_price <= 0 or current_price <= 0:
        return False, 0.0
    dd_from_peak = current_price / pos.peak_price - 1
    return dd_from_peak < max_dd_from_peak, dd_from_peak


# ═══════════════════════════════════════════════════════════════════════════
#  Main entry point — template flow verbatim
# ═══════════════════════════════════════════════════════════════════════════

def apply_position_stops(
    current_weights: pd.Series,
    position_tracker: SectorPositionTracker,
    sector_prices: pd.DataFrame,
    benchmark_prices: Optional[pd.Series],
    rebalance_date: pd.Timestamp,
    config: dict,
) -> Tuple[List[str], List[StopLossEvent], bool]:
    """Evaluate all stops → (stopped_tickers, events, halve_all)."""
    stopped_sectors: List[str] = []
    events: List[StopLossEvent] = []
    halve_all = False

    if not config.get("enabled", False):
        return stopped_sectors, events, halve_all

    cooling_days = config.get("cooling_off_days", 10)

    bench_3d_ret = 0.0
    if benchmark_prices is not None and not benchmark_prices.empty:
        up_to = benchmark_prices.loc[:rebalance_date].dropna()
        if len(up_to) >= 4:
            bench_3d_ret = float(up_to.iloc[-1] / up_to.iloc[-4] - 1)

    # ── 1. Portfolio Circuit Breaker ──────────────────────────────
    cb_cfg = config.get("portfolio_circuit_breaker", {})
    if cb_cfg.get("enabled", True):
        triggered, b3d = check_portfolio_circuit_breaker(
            benchmark_prices, rebalance_date,
            benchmark_3d_limit=cb_cfg.get("benchmark_3d_limit", -0.15),
        )
        if triggered:
            halve_all = True
            events.append(StopLossEvent(
                date=rebalance_date, ticker="PORTFOLIO", stop_type="portfolio_circuit",
                reason=f"KXBTCPERP 3d return {b3d:.1%} < "
                       f"{cb_cfg.get('benchmark_3d_limit', -0.15):.0%} threshold",
                entry_price=0.0, current_price=0.0,
                threshold=cb_cfg.get("benchmark_3d_limit", -0.15),
                pnl_pct=b3d, days_held=0, benchmark_return_3d=b3d,
            ))
            log.warning(f"[STOP LOSS] Portfolio circuit breaker! BTC 3d={b3d:.1%}")

    # ── 2. Per-perp stops ─────────────────────────────────────────
    sc_cfg = config.get("sector_collapse", {})
    ts_cfg = config.get("trailing_stop", {})

    for ticker, pos in list(position_tracker.positions.items()):
        w = float(current_weights.get(ticker, 0.0))
        if w < 1e-4:
            continue
        if position_tracker.is_cooling_off(ticker, rebalance_date, cooling_days):
            continue
        if ticker not in sector_prices.columns:
            continue
        price_series = sector_prices[ticker].loc[:rebalance_date].dropna()
        if price_series.empty:
            continue
        current_price = float(price_series.iloc[-1])

        pos.peak_price = max(pos.peak_price, current_price)

        if sc_cfg.get("enabled", True):
            triggered, pnl_pct = check_sector_collapse(
                pos, current_price,
                max_dd_from_entry=sc_cfg.get("max_dd_from_entry", -0.20),
            )
            if triggered:
                stopped_sectors.append(ticker)
                position_tracker.record_stop(ticker, rebalance_date)
                events.append(StopLossEvent(
                    date=rebalance_date, ticker=ticker, stop_type="sector_collapse",
                    reason=f"{ticker} down {pnl_pct:.1%} from entry ${pos.entry_price:.4f}",
                    entry_price=pos.entry_price, current_price=current_price,
                    threshold=sc_cfg.get("max_dd_from_entry", -0.20),
                    pnl_pct=pnl_pct, days_held=pos.days_held,
                    benchmark_return_3d=bench_3d_ret,
                ))
                log.warning(f"[STOP LOSS] Perp collapse: {ticker} {pnl_pct:.1%} from entry")
                continue

        if ts_cfg.get("enabled", True):
            triggered, dd_pct = check_trailing_stop(
                pos, current_price,
                max_dd_from_peak=ts_cfg.get("max_dd_from_peak", -0.25),
            )
            if triggered:
                stopped_sectors.append(ticker)
                position_tracker.record_stop(ticker, rebalance_date)
                events.append(StopLossEvent(
                    date=rebalance_date, ticker=ticker, stop_type="trailing_stop",
                    reason=f"{ticker} down {dd_pct:.1%} from peak ${pos.peak_price:.4f}",
                    entry_price=pos.entry_price, current_price=current_price,
                    threshold=ts_cfg.get("max_dd_from_peak", -0.25),
                    pnl_pct=current_price / pos.entry_price - 1,
                    days_held=pos.days_held, benchmark_return_3d=bench_3d_ret,
                ))
                log.warning(f"[STOP LOSS] Trailing stop: {ticker} {dd_pct:.1%} from peak")

    return stopped_sectors, events, halve_all
