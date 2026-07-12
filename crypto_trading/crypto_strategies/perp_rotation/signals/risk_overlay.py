"""risk_overlay.py — Independent Risk Control Layer (crypto perps).

COPIED from qlib-main/sector_rotation/signals/risk_overlay.py (read-only
template). Three multipliers preserved verbatim in structure:
  1. Entry/Exit Filter: per-perp trend gate (MA50/100 + relative strength)
  2. Market Risk Multiplier: BTC-perp trend (was SPY) + btc_rvol (was VIX)
  3. Drawdown Multiplier: portfolio-level protection
Final position = score_weight × entry_gate × market_multiplier × dd_multiplier.

Adaptations (plan §5 "Change" column): benchmark = KXBTCPERP price series
(BTC market filter); vix → btc_rvol with crypto brackets (60 = crisis, matching
crypto_common.regime's calibrate-me defaults); crypto-scale drawdown tiers
exposed as the same parameters. PLUS the plan §6 low-volatility factor
(``compute_low_vol_signal``) lives here per the plan's reuse map.

References (template): Antonacci (2014); Faber (2007); Clare et al. (2016).
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

TRADING_DAYS = 365  # 24/7 (crypto adaptation; template used implicit 252)


# ═══════════════════════════════════════════════════════════════════════════
#  1. Entry/Exit Filter (per-perp trend gate) — template verbatim
# ═══════════════════════════════════════════════════════════════════════════

def compute_sector_entry_gate(
    sector_prices: pd.DataFrame,
    benchmark_prices: pd.Series,
    ma_short: int = 50,
    ma_long: int = 100,
) -> pd.DataFrame:
    """Per-perp entry gate: (above short MA AND RS trending up) OR above long MA."""
    ma_s = sector_prices.rolling(ma_short, min_periods=ma_short // 2).mean()
    ma_l = sector_prices.rolling(ma_long, min_periods=ma_long // 2).mean()

    rs = sector_prices.div(benchmark_prices, axis=0)
    rs_ma = rs.rolling(ma_short, min_periods=ma_short // 2).mean()

    above_short_ma = sector_prices > ma_s
    above_long_ma = sector_prices > ma_l
    rs_trending_up = rs > rs_ma

    return (above_short_ma & rs_trending_up) | above_long_ma


def compute_sector_exit_signal(
    sector_prices: pd.DataFrame,
    benchmark_prices: pd.Series,
    ma_exit: int = 100,
    max_drawdown_pct: float = -0.20,
    lookback_high: int = 63,
) -> pd.DataFrame:
    """Force-exit mask: trend destruction OR severe drawdown from rolling high.
    (Template verbatim; default DD threshold widened −10% → −20% for crypto vol.)"""
    ma_l = sector_prices.rolling(ma_exit, min_periods=ma_exit // 2).mean()

    rs = sector_prices.div(benchmark_prices, axis=0)
    rs_ma = rs.rolling(50, min_periods=25).mean()

    rolling_high = sector_prices.rolling(lookback_high, min_periods=lookback_high // 2).max()
    drawdown = sector_prices / rolling_high - 1

    trend_broken = (sector_prices < ma_l) & (rs < rs_ma)
    severe_dd = drawdown < max_drawdown_pct

    return trend_broken | severe_dd


# ═══════════════════════════════════════════════════════════════════════════
#  2. Market Risk Multiplier (portfolio-level) — BTC benchmark
# ═══════════════════════════════════════════════════════════════════════════

def compute_market_risk_multiplier(
    benchmark_prices: pd.Series,
    btc_rvol: Optional[pd.Series] = None,
    ma_medium: int = 100,
    ma_long: int = 200,
    rvol_crisis: float = 60.0,
) -> pd.Series:
    """BTC-market multiplier (template rules, SPY→KXBTCPERP, VIX→btc_rvol %):
      BTC > MA200 AND > MA100      → 1.0
      BTC > MA200 but < MA100      → 0.75
      BTC < MA200                  → 0.50
      BTC < MA200 AND rvol > 60    → 0.35 (crisis; calibrate on recorded data)
    """
    ma_med = benchmark_prices.rolling(ma_medium, min_periods=ma_medium // 2).mean()
    ma_lng = benchmark_prices.rolling(ma_long, min_periods=ma_long // 2).mean()

    multiplier = pd.Series(1.0, index=benchmark_prices.index)

    below_ma200 = benchmark_prices < ma_lng
    below_ma100 = benchmark_prices < ma_med
    above_ma200 = ~below_ma200

    multiplier[above_ma200 & below_ma100] = 0.75
    multiplier[below_ma200] = 0.50

    if btc_rvol is not None:
        rvol_aligned = btc_rvol.reindex(benchmark_prices.index, method="ffill")
        crisis = below_ma200 & (rvol_aligned > rvol_crisis)
        multiplier[crisis] = 0.35

    return multiplier


# ═══════════════════════════════════════════════════════════════════════════
#  3. Portfolio Drawdown Multiplier — template verbatim, crypto tiers
# ═══════════════════════════════════════════════════════════════════════════

def compute_drawdown_multiplier(
    portfolio_equity: pd.Series,
    dd_cautious: float = -0.08,
    dd_defensive: float = -0.12,
    dd_crisis: float = -0.20,
) -> float:
    """Current-DD tier multiplier (template mechanics; crypto-wide thresholds
    aligned with Plan 06 §3 drawdown limits: −10% halve … −20% halt)."""
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
#  Low-volatility factor (plan §6 table — lives here per the reuse map)
# ═══════════════════════════════════════════════════════════════════════════

def compute_low_vol_signal(
    prices: pd.DataFrame,
    window: int = 30,
) -> pd.DataFrame:
    """Reward LOWER realized-vol perps: cross-sectional z of −vol (365-ann.).
    Used by the composite with a regime-conditional weight (defensive states)."""
    rets = prices.pct_change()
    vol = rets.rolling(window, min_periods=window // 2).std() * np.sqrt(TRADING_DAYS)
    neg_vol = -vol
    return neg_vol.sub(neg_vol.mean(axis=1), axis=0).div(
        neg_vol.std(axis=1).replace(0, np.nan), axis=0
    )


# ═══════════════════════════════════════════════════════════════════════════
#  Convenience: apply full risk overlay to weights — template verbatim
# ═══════════════════════════════════════════════════════════════════════════

def apply_risk_overlay(
    target_weights: pd.Series,
    sector_prices: pd.DataFrame,
    benchmark_prices: pd.Series,
    portfolio_equity: Optional[pd.Series] = None,
    btc_rvol: Optional[pd.Series] = None,
    rebalance_date: Optional[pd.Timestamp] = None,
    config: Optional[dict] = None,
) -> pd.Series:
    """Full 3-layer overlay (template flow preserved; vix→btc_rvol)."""
    cfg = config or {}
    dt = rebalance_date or sector_prices.index[-1]

    entry_gate = compute_sector_entry_gate(
        sector_prices, benchmark_prices,
        ma_short=cfg.get("entry_ma_short", 50),
        ma_long=cfg.get("entry_ma_long", 100),
    )
    exit_signal = compute_sector_exit_signal(
        sector_prices, benchmark_prices,
        ma_exit=cfg.get("exit_ma", 100),
        max_drawdown_pct=cfg.get("exit_max_dd", -0.20),
    )

    if dt in entry_gate.index:
        gate_row = entry_gate.loc[dt]
        exit_row = exit_signal.loc[dt]
    else:
        gate_row = entry_gate.iloc[-1] if not entry_gate.empty else pd.Series(True, index=target_weights.index)
        exit_row = exit_signal.iloc[-1] if not exit_signal.empty else pd.Series(False, index=target_weights.index)

    adjusted = target_weights.copy()
    for ticker in adjusted.index:
        if ticker in gate_row.index and ticker in exit_row.index:
            if exit_row.get(ticker, False):
                adjusted[ticker] = 0.0
            elif not gate_row.get(ticker, True):
                adjusted[ticker] *= 0.3

    mkt_mult = compute_market_risk_multiplier(
        benchmark_prices, btc_rvol=btc_rvol,
        ma_medium=cfg.get("market_ma_medium", 100),
        ma_long=cfg.get("market_ma_long", 200),
        rvol_crisis=cfg.get("rvol_crisis", 60.0),
    )
    mkt_mult_at_dt = float(mkt_mult.loc[dt]) if dt in mkt_mult.index else float(mkt_mult.iloc[-1])
    adjusted *= mkt_mult_at_dt

    if portfolio_equity is not None and not portfolio_equity.empty:
        dd_mult = compute_drawdown_multiplier(
            portfolio_equity,
            dd_cautious=cfg.get("dd_cautious", -0.08),
            dd_defensive=cfg.get("dd_defensive", -0.12),
            dd_crisis=cfg.get("dd_crisis", -0.20),
        )
        adjusted *= dd_mult

    adjusted = adjusted.clip(lower=0.0)

    logger.debug(
        f"Risk overlay at {dt.date() if hasattr(dt, 'date') else dt}: "
        f"mkt_mult={mkt_mult_at_dt:.2f}, "
        f"sum_before={target_weights.sum():.2f}, sum_after={adjusted.sum():.2f}"
    )
    return adjusted
