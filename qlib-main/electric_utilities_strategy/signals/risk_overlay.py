"""
risk_overlay.py — Independent Risk Control Layer
=================================================
Applied AFTER signal scoring to control position sizing.
NOT a scoring factor — a position multiplier.

Three independent multipliers:
  1. Entry/Exit Filter: per-sector trend gate (MA50/100 + relative strength)
  2. Market Risk Multiplier: overall market regime (SPY MA trend + breadth)
  3. Drawdown Multiplier: portfolio-level protection

Final position = score_weight × entry_gate × market_multiplier × dd_multiplier
Remainder → cash

References:
  - Antonacci (2014): Dual Momentum — absolute + relative momentum
  - Faber (2007): "A Quantitative Approach to Tactical Asset Allocation"
    — 10-month MA as allocation rule
  - Clare et al. (2016): trend-following overlay reduces drawdowns 30-50%
"""
from __future__ import annotations

import logging
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
#  1. Entry/Exit Filter (per-sector trend gate)
# ═══════════════════════════════════════════════════════════════════════════

def compute_sector_entry_gate(
    sector_prices: pd.DataFrame,
    benchmark_prices: pd.Series,
    ma_short: int = 50,
    ma_long: int = 100,
) -> pd.DataFrame:
    """
    Per-sector entry gate: a sector can receive allocation only if
    it passes at minimum one of these trend conditions:
      - Price > MA(short) AND relative strength trending up
      - OR Price > MA(long) (relaxed condition for established trends)

    Returns
    -------
    pd.DataFrame — boolean mask (True = entry allowed), same shape as sector_prices
    """
    # Moving averages
    ma_s = sector_prices.rolling(ma_short, min_periods=ma_short // 2).mean()
    ma_l = sector_prices.rolling(ma_long, min_periods=ma_long // 2).mean()

    # Relative strength: sector / benchmark
    rs = sector_prices.div(benchmark_prices, axis=0)
    rs_ma = rs.rolling(ma_short, min_periods=ma_short // 2).mean()

    # Entry conditions (relaxed: OR logic)
    above_short_ma = sector_prices > ma_s
    above_long_ma = sector_prices > ma_l
    rs_trending_up = rs > rs_ma

    # Allow entry if: (above short MA AND RS positive) OR (above long MA)
    gate = (above_short_ma & rs_trending_up) | above_long_ma

    return gate


def compute_sector_exit_signal(
    sector_prices: pd.DataFrame,
    benchmark_prices: pd.Series,
    ma_exit: int = 100,
    max_drawdown_pct: float = -0.10,
    lookback_high: int = 63,
) -> pd.DataFrame:
    """
    Per-sector exit signal: force exit if sector shows trend destruction.

    Exit if:
      (Price < MA(exit) AND relative strength below its MA50)
      OR sector drawdown from 63-day high > max_drawdown_pct

    Returns
    -------
    pd.DataFrame — boolean mask (True = EXIT signal), same shape as sector_prices
    """
    ma_l = sector_prices.rolling(ma_exit, min_periods=ma_exit // 2).mean()

    rs = sector_prices.div(benchmark_prices, axis=0)
    rs_ma = rs.rolling(50, min_periods=25).mean()

    # Drawdown from rolling high
    rolling_high = sector_prices.rolling(lookback_high, min_periods=lookback_high // 2).max()
    drawdown = sector_prices / rolling_high - 1

    # Exit conditions
    trend_broken = (sector_prices < ma_l) & (rs < rs_ma)
    severe_dd = drawdown < max_drawdown_pct

    return trend_broken | severe_dd


# ═══════════════════════════════════════════════════════════════════════════
#  2. Market Risk Multiplier (portfolio-level)
# ═══════════════════════════════════════════════════════════════════════════

def compute_market_risk_multiplier(
    benchmark_prices: pd.Series,
    vix: Optional[pd.Series] = None,
    ma_medium: int = 100,
    ma_long: int = 200,
) -> pd.Series:
    """
    Market-wide risk multiplier based on SPY trend.

    Rules:
      SPY > MA200 AND SPY > MA100 → 1.0 (full exposure)
      SPY > MA200 but < MA100     → 0.75 (cautious)
      SPY < MA200                 → 0.50 (defensive)
      SPY < MA200 AND VIX > 30    → 0.35 (crisis)

    Returns
    -------
    pd.Series — daily multiplier (0.35 to 1.0), same index as benchmark_prices
    """
    ma_med = benchmark_prices.rolling(ma_medium, min_periods=ma_medium // 2).mean()
    ma_lng = benchmark_prices.rolling(ma_long, min_periods=ma_long // 2).mean()

    multiplier = pd.Series(1.0, index=benchmark_prices.index)

    # Conditions (applied in order, later overrides earlier)
    below_ma200 = benchmark_prices < ma_lng
    below_ma100 = benchmark_prices < ma_med
    above_ma200 = ~below_ma200

    multiplier[above_ma200 & below_ma100] = 0.75
    multiplier[below_ma200] = 0.50

    if vix is not None:
        vix_aligned = vix.reindex(benchmark_prices.index, method="ffill")
        crisis = below_ma200 & (vix_aligned > 30)
        multiplier[crisis] = 0.35

    return multiplier


# ═══════════════════════════════════════════════════════════════════════════
#  3. Portfolio Drawdown Multiplier
# ═══════════════════════════════════════════════════════════════════════════

def compute_drawdown_multiplier(
    portfolio_equity: pd.Series,
    dd_cautious: float = -0.05,
    dd_defensive: float = -0.08,
    dd_crisis: float = -0.12,
) -> float:
    """
    Portfolio-level drawdown protection. Returns a single multiplier
    based on current drawdown from peak.

    Rules:
      DD > -5%:  1.0 (no reduction)
      DD -5% to -8%: 0.70
      DD -8% to -12%: 0.40
      DD < -12%: 0.20

    Parameters
    ----------
    portfolio_equity : pd.Series — daily equity curve
    dd_cautious/defensive/crisis : float — thresholds (negative)

    Returns
    -------
    float — multiplier (0.20 to 1.0)
    """
    if portfolio_equity.empty:
        return 1.0

    peak = portfolio_equity.expanding().max()
    current_dd = float(portfolio_equity.iloc[-1] / peak.iloc[-1] - 1)

    if current_dd > dd_cautious:
        return 1.0
    elif current_dd > dd_defensive:
        return 0.70
    elif current_dd > dd_crisis:
        return 0.40
    else:
        return 0.20


# ═══════════════════════════════════════════════════════════════════════════
#  Convenience: apply full risk overlay to weights
# ═══════════════════════════════════════════════════════════════════════════

def apply_risk_overlay(
    target_weights: pd.Series,
    sector_prices: pd.DataFrame,
    benchmark_prices: pd.Series,
    portfolio_equity: Optional[pd.Series] = None,
    vix: Optional[pd.Series] = None,
    rebalance_date: Optional[pd.Timestamp] = None,
    config: Optional[dict] = None,
) -> pd.Series:
    """
    Apply full 3-layer risk overlay to target portfolio weights.

    Parameters
    ----------
    target_weights : pd.Series — sector weights from scoring (tickers → float)
    sector_prices : pd.DataFrame — daily sector prices (for entry/exit gate)
    benchmark_prices : pd.Series — SPY daily prices
    portfolio_equity : pd.Series — portfolio equity curve (for DD multiplier)
    vix : pd.Series — VIX daily values (optional, for market multiplier)
    rebalance_date : pd.Timestamp — date to evaluate (default: last available)
    config : dict — risk overlay configuration (thresholds)

    Returns
    -------
    pd.Series — adjusted weights (sum may be < 1.0, remainder = cash)
    """
    cfg = config or {}
    dt = rebalance_date or sector_prices.index[-1]

    # ── Layer 1: Entry/Exit gate ──────────────────────────────────────────
    entry_gate = compute_sector_entry_gate(
        sector_prices, benchmark_prices,
        ma_short=cfg.get("entry_ma_short", 50),
        ma_long=cfg.get("entry_ma_long", 100),
    )
    exit_signal = compute_sector_exit_signal(
        sector_prices, benchmark_prices,
        ma_exit=cfg.get("exit_ma", 100),
        max_drawdown_pct=cfg.get("exit_max_dd", -0.10),
    )

    # Get gate values at rebalance date
    if dt in entry_gate.index:
        gate_row = entry_gate.loc[dt]
        exit_row = exit_signal.loc[dt]
    else:
        # Use last available
        gate_row = entry_gate.iloc[-1] if not entry_gate.empty else pd.Series(True, index=target_weights.index)
        exit_row = exit_signal.iloc[-1] if not exit_signal.empty else pd.Series(False, index=target_weights.index)

    # Apply: zero weight for sectors that fail entry or trigger exit
    adjusted = target_weights.copy()
    for ticker in adjusted.index:
        if ticker in gate_row.index and ticker in exit_row.index:
            if exit_row.get(ticker, False):
                adjusted[ticker] = 0.0
            elif not gate_row.get(ticker, True):
                # Failed entry gate — reduce but don't zero (might be existing position)
                adjusted[ticker] *= 0.3

    # ── Layer 2: Market risk multiplier ───────────────────────────────────
    mkt_mult = compute_market_risk_multiplier(
        benchmark_prices, vix=vix,
        ma_medium=cfg.get("market_ma_medium", 100),
        ma_long=cfg.get("market_ma_long", 200),
    )
    mkt_mult_at_dt = float(mkt_mult.loc[dt]) if dt in mkt_mult.index else float(mkt_mult.iloc[-1])
    adjusted *= mkt_mult_at_dt

    # ── Layer 3: Portfolio drawdown multiplier ────────────────────────────
    if portfolio_equity is not None and not portfolio_equity.empty:
        dd_mult = compute_drawdown_multiplier(
            portfolio_equity,
            dd_cautious=cfg.get("dd_cautious", -0.05),
            dd_defensive=cfg.get("dd_defensive", -0.08),
            dd_crisis=cfg.get("dd_crisis", -0.12),
        )
        adjusted *= dd_mult

    # Ensure non-negative
    adjusted = adjusted.clip(lower=0.0)

    logger.debug(
        f"Risk overlay at {dt.date() if hasattr(dt, 'date') else dt}: "
        f"mkt_mult={mkt_mult_at_dt:.2f}, "
        f"sum_before={target_weights.sum():.2f}, sum_after={adjusted.sum():.2f}"
    )

    return adjusted
