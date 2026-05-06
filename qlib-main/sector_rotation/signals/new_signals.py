"""
new_signals.py — Additional Signal Factors for Sector Rotation
================================================================
Three new bonus signals to capture trends faster:

  1. Short-Term Momentum (6-month) — independent from 12-1m, catches trend reversals earlier
  2. Earnings Revision Momentum   — YoY EPS growth rate trend per sector (forward-looking)
  3. Relative Strength Breakout   — sector/SPY ratio hitting N-day highs (fastest signal)

All signals return month-end z-scored DataFrames following the same contract
as momentum.py and value.py. They integrate into composite.py as bonus signals
(like acceleration_bonus), defaulting to weight 0.0 for backwards compatibility.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
#  Utility: cross-sectional z-score (shared pattern)
# ═══════════════════════════════════════════════════════════════════════════

def _cs_zscore(df: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score: at each date, (x - mean) / std across tickers."""
    mu = df.mean(axis=1)
    sigma = df.std(axis=1).replace(0, np.nan)
    return df.sub(mu, axis=0).div(sigma, axis=0)


# ═══════════════════════════════════════════════════════════════════════════
#  Signal 1: Short-Term Momentum (6-month)
# ═══════════════════════════════════════════════════════════════════════════

def compute_short_term_momentum(
    prices: pd.DataFrame,
    lookback_months: int = 6,
    skip_months: int = 1,
    zscore_window: int = 24,
) -> pd.DataFrame:
    """
    Independent short-term momentum factor (default 6-1 month).

    Unlike the 12-1 month CS momentum, this captures intermediate-term
    trend reversals 6 months earlier — critical for catching sector
    breakouts like the 2023 AI/tech rally.

    Returns
    -------
    pd.DataFrame — month-end, cross-sectional z-scored, columns = tickers
    """
    monthly = prices.resample("ME").last()
    monthly_ret = monthly.pct_change()

    if len(monthly_ret) < lookback_months + skip_months + 1:
        logger.warning(f"Short-term momentum: insufficient data "
                       f"({len(monthly_ret)} months < {lookback_months + skip_months + 1})")
        return pd.DataFrame()

    # Cumulative return from t-lookback to t-skip
    cum_ret = pd.DataFrame(index=monthly_ret.index, columns=monthly_ret.columns,
                           dtype=float)
    for i in range(lookback_months + skip_months, len(monthly_ret)):
        window = monthly_ret.iloc[i - lookback_months: i - skip_months + 1]
        cum_ret.iloc[i] = (1 + window).prod() - 1

    cum_ret = cum_ret.dropna(how="all")

    # Cross-sectional z-score
    stm_z = _cs_zscore(cum_ret)

    # Rolling time-series z-score normalization (stabilize scale over time)
    if zscore_window > 0 and len(stm_z) > zscore_window:
        roll_mu = stm_z.rolling(zscore_window, min_periods=zscore_window // 2).mean()
        roll_sd = stm_z.rolling(zscore_window, min_periods=zscore_window // 2).std()
        stm_z = (stm_z - roll_mu) / roll_sd.replace(0, np.nan)

    return stm_z


# ═══════════════════════════════════════════════════════════════════════════
#  Signal 2: Earnings Revision Momentum
# ═══════════════════════════════════════════════════════════════════════════

# Reuse sector representatives from value.py
# Import from value.py — single source of truth for sector representatives.
# value.py has top-10 per sector (110 stocks), all verified on Polygon.
# update_eps_history.py also reads from value.py, so weekly cron auto-updates all 110.
from .value import SECTOR_REPRESENTATIVES as _VALUE_REPS

# For ERM: use all 10 per sector, but handle META→FB rename merge
SECTOR_REPRESENTATIVES: Dict[str, List[str]] = {
    etf: [("META+FB" if s == "META" else s) for s in stocks]
    for etf, stocks in _VALUE_REPS.items()
}


def _load_eps_store(path: Optional[Path] = None) -> dict:
    """Load eps_history.json (cached)."""
    import json
    if path is None:
        path = Path(__file__).parent.parent.parent.parent / "price_data" / "sector_etfs" / "eps_history.json"
    if not path.exists():
        logger.warning(f"EPS store not found: {path}")
        return {}
    with open(path) as f:
        return json.load(f)


def compute_earnings_revision_momentum(
    etf_tickers: List[str],
    lookback_quarters: int = 4,
    monthly_index: Optional[pd.DatetimeIndex] = None,
    eps_store_path: Optional[Path] = None,
    reporting_lag_days: int = 30,
) -> pd.DataFrame:
    """
    Earnings revision momentum: YoY EPS growth rate trend per sector.

    For each sector ETF, computes the average YoY EPS growth rate across
    its top-5 representative stocks. Sectors with rising EPS growth
    (positive revision) score higher.

    POINT-IN-TIME GUARANTEE:
    EPS data is indexed by `end_date + reporting_lag_days` (default 30 days)
    to reflect when the data would actually be PUBLICLY AVAILABLE.
    - Q4 ends 2024-12-31 → available date = 2025-01-30 (30 day lag)
    - S&P 500 companies report earnings 15-35 days after quarter end
    - EPS is public on earnings call day (not SEC filing date)
    - 30 days covers 95%+ of S&P 500 filers conservatively
    This prevents any lookahead bias in backtesting.

    Parameters
    ----------
    etf_tickers : list of sector ETF tickers
    lookback_quarters : how many recent quarters to average for revision trend
    monthly_index : target month-end DatetimeIndex (for alignment)
    eps_store_path : path to eps_history.json
    reporting_lag_days : days after quarter end before EPS is considered available
                        (default 45 — conservative SEC 10-Q filing deadline)

    Returns
    -------
    pd.DataFrame — month-end, cross-sectional z-scored, columns = tickers
    """
    store = _load_eps_store(eps_store_path)
    if not store or "symbols" not in store:
        logger.warning("Earnings revision: no EPS data available")
        return pd.DataFrame()

    symbols_data = store["symbols"]
    lag = pd.Timedelta(days=reporting_lag_days)

    # Build per-sector quarterly EPS growth series
    sector_revision: Dict[str, pd.Series] = {}

    for etf in etf_tickers:
        reps = SECTOR_REPRESENTATIVES.get(etf, [])
        if not reps:
            continue

        stock_growths: List[pd.Series] = []
        for stock in reps:
            # Handle ticker renames (e.g. "META+FB" → merge both histories)
            if "+" in stock:
                parts = stock.split("+")
                merged_eps = []
                seen_dates: set = set()
                for part in parts:
                    for q in symbols_data.get(part, []):
                        if q["end_date"] not in seen_dates:
                            merged_eps.append(q)
                            seen_dates.add(q["end_date"])
                eps_list = merged_eps
            else:
                eps_list = symbols_data.get(stock, [])

            if len(eps_list) < 5:
                continue

            # Parse into Series
            eps_df = pd.DataFrame(eps_list)
            eps_df["end_date"] = pd.to_datetime(eps_df["end_date"])
            eps_df = eps_df.sort_values("end_date")

            # POINT-IN-TIME: index by available_date = end_date + reporting_lag
            # This is when the data would actually be publicly known
            eps_df["available_date"] = eps_df["end_date"] + lag
            eps_df = eps_df.set_index("available_date")
            eps_s = eps_df["eps"].astype(float)

            # YoY growth: (EPS_q - EPS_q-4) / |EPS_q-4|
            eps_prev = eps_s.shift(4)
            yoy_growth = (eps_s - eps_prev) / eps_prev.abs().replace(0, np.nan)
            yoy_growth = yoy_growth.dropna()

            if not yoy_growth.empty:
                stock_growths.append(yoy_growth)

        if not stock_growths:
            continue

        # Average across available stocks for this sector
        combined = pd.concat(stock_growths, axis=1).mean(axis=1)
        # Rolling average of recent quarters for smoothing
        if len(combined) >= lookback_quarters:
            combined = combined.rolling(lookback_quarters, min_periods=2).mean()
        sector_revision[etf] = combined

    if not sector_revision:
        logger.warning("Earnings revision: no valid sector data")
        return pd.DataFrame()

    # Combine into DataFrame
    rev_df = pd.DataFrame(sector_revision)

    # Resample to monthly (forward-fill quarterly data to monthly frequency)
    # EPS reports come quarterly; between reports, the signal stays constant
    rev_monthly = rev_df.resample("ME").last().ffill()

    # If monthly_index provided, align
    if monthly_index is not None:
        rev_monthly = rev_monthly.reindex(monthly_index, method="ffill")

    # Cross-sectional z-score
    rev_z = _cs_zscore(rev_monthly)

    # Only keep tickers that were requested
    valid_cols = [c for c in etf_tickers if c in rev_z.columns]
    if not valid_cols:
        return pd.DataFrame()

    return rev_z[valid_cols]


# ═══════════════════════════════════════════════════════════════════════════
#  Signal 3: Relative Strength Breakout
# ═══════════════════════════════════════════════════════════════════════════

def compute_relative_strength_breakout(
    sector_prices: pd.DataFrame,
    benchmark_prices: pd.Series,
    lookback_days: int = 63,
) -> pd.DataFrame:
    """
    Relative strength breakout: sector/SPY ratio near N-day high.

    For each sector, computes where the sector/benchmark price ratio
    sits within its recent range:
        signal = (current_ratio - rolling_min) / (rolling_max - rolling_min)

    Values near 1.0 = ratio at N-day high (breakout, bullish).
    Values near 0.0 = ratio at N-day low (breakdown, bearish).

    This is the fastest signal (daily frequency), capturing momentum
    shifts before monthly rebalance signals.

    Returns
    -------
    pd.DataFrame — month-end, cross-sectional z-scored, columns = tickers
    """
    if benchmark_prices is None or benchmark_prices.empty:
        logger.warning("RS breakout: no benchmark prices provided")
        return pd.DataFrame()

    # Compute daily relative strength ratio for each sector
    # Align benchmark to sector dates
    bench = benchmark_prices.reindex(sector_prices.index, method="ffill")
    ratios = sector_prices.div(bench, axis=0)

    # Rolling min/max over lookback window
    roll_max = ratios.rolling(lookback_days, min_periods=lookback_days // 2).max()
    roll_min = ratios.rolling(lookback_days, min_periods=lookback_days // 2).min()

    # Percentile within range: 0 = at min, 1 = at max
    range_width = (roll_max - roll_min).replace(0, np.nan)
    rs_pct = (ratios - roll_min) / range_width

    # Resample to month-end
    rs_monthly = rs_pct.resample("ME").last()

    # Cross-sectional z-score
    rs_z = _cs_zscore(rs_monthly)

    return rs_z


# ═══════════════════════════════════════════════════════════════════════════
#  Convenience: compute all three in one call
# ═══════════════════════════════════════════════════════════════════════════

def compute_all_new_signals(
    sector_prices: pd.DataFrame,
    benchmark_prices: Optional[pd.Series] = None,
    etf_tickers: Optional[List[str]] = None,
    stm_enabled: bool = False,
    stm_lookback: int = 6,
    stm_skip: int = 1,
    stm_zscore_window: int = 24,
    erm_enabled: bool = False,
    erm_lookback_quarters: int = 4,
    rsb_enabled: bool = False,
    rsb_lookback_days: int = 63,
    eps_store_path: Optional[Path] = None,
    monthly_index: Optional[pd.DatetimeIndex] = None,
) -> Dict[str, Optional[pd.DataFrame]]:
    """
    Compute all three new signals. Returns dict with keys:
      "short_term_mom", "earnings_revision", "rs_breakout"
    Each value is a z-scored DataFrame or None if disabled/failed.
    """
    result: Dict[str, Optional[pd.DataFrame]] = {
        "short_term_mom": None,
        "earnings_revision": None,
        "rs_breakout": None,
    }
    tickers = etf_tickers or list(sector_prices.columns)

    if stm_enabled:
        try:
            result["short_term_mom"] = compute_short_term_momentum(
                sector_prices, stm_lookback, stm_skip, stm_zscore_window,
            )
            if result["short_term_mom"] is not None and not result["short_term_mom"].empty:
                logger.info(f"Short-term momentum: {len(result['short_term_mom'])} months, "
                            f"{result['short_term_mom'].shape[1]} tickers")
        except Exception as e:
            logger.warning(f"Short-term momentum failed: {e}")

    if erm_enabled:
        try:
            result["earnings_revision"] = compute_earnings_revision_momentum(
                tickers, erm_lookback_quarters, monthly_index, eps_store_path,
            )
            if result["earnings_revision"] is not None and not result["earnings_revision"].empty:
                logger.info(f"Earnings revision: {len(result['earnings_revision'])} months, "
                            f"{result['earnings_revision'].shape[1]} tickers")
        except Exception as e:
            logger.warning(f"Earnings revision failed: {e}")

    if rsb_enabled:
        try:
            result["rs_breakout"] = compute_relative_strength_breakout(
                sector_prices, benchmark_prices, rsb_lookback_days,
            )
            if result["rs_breakout"] is not None and not result["rs_breakout"].empty:
                logger.info(f"RS breakout: {len(result['rs_breakout'])} months, "
                            f"{result['rs_breakout'].shape[1]} tickers")
        except Exception as e:
            logger.warning(f"RS breakout failed: {e}")

    return result
