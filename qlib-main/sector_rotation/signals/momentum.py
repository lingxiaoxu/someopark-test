"""
Momentum Signals
================
Cross-sectional and time-series momentum for sector ETFs.

References
----------
Moskowitz, T., Ooi, Y. H., & Pedersen, L. H. (2012).
    Time series momentum. Journal of Financial Economics, 104(2), 228-250.

Gupta, T., & Kelly, B. (2019). Factor Momentum Everywhere (AQR).
    Journal of Portfolio Management, 45(3), 13-36.

Signal Definitions
------------------
1. Cross-Sectional Momentum (cs_mom):
   - For each month t, compute the return from t-12 to t-1 (skip last month to
     avoid short-term reversal, Jegadeesh & Titman 1993).
   - z-score across sectors relative to a rolling 36-month window of cs_mom.
   - Positive z-score = outperformer = higher composite signal.

2. Time-Series Momentum (ts_mom):
   - For each sector, compute the 12-month cumulative return to last month.
   - Binary signal: +1 if positive, 0 otherwise (crash filter).
   - Can be used as a multiplier on cs_mom (AQR-style) or standalone.

3. Momentum Acceleration (accel):
   - 3-month return minus 12-month return (recent acceleration).
   - Used as a secondary tilt, not the primary signal.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Rolling return utilities
# ---------------------------------------------------------------------------

def _monthly_prices_to_returns(monthly_prices: pd.DataFrame) -> pd.DataFrame:
    """Compute simple monthly returns from end-of-month prices."""
    return monthly_prices.pct_change()


def _cumulative_return(monthly_returns: pd.DataFrame, start_lag: int, end_lag: int) -> pd.DataFrame:
    """
    Compute rolling cumulative return from start_lag to end_lag months ago.

    For a given month t:
        Return = product(1 + r_t-end_lag ... r_t-start_lag) - 1

    Parameters
    ----------
    monthly_returns : pd.DataFrame
        Monthly returns (ME-indexed).
    start_lag : int
        Most recent month included (e.g., 1 = skip last month).
    end_lag : int
        Furthest month included (e.g., 12 = go back 12 months).

    Returns
    -------
    pd.DataFrame
        Rolling cumulative returns, same index as monthly_returns.
        NaN for dates where < end_lag months of data exist.
    """
    assert end_lag > start_lag >= 0, "end_lag must be > start_lag >= 0"

    result = pd.DataFrame(index=monthly_returns.index, columns=monthly_returns.columns,
                          dtype=float)
    for i in range(len(monthly_returns)):
        row_idx = monthly_returns.index[i]
        # We need rows from (i - end_lag) to (i - start_lag) inclusive
        r_start = i - end_lag
        r_end = i - start_lag  # inclusive
        if r_start < 0 or r_end < 0 or r_end < r_start:
            result.loc[row_idx] = np.nan
            continue
        window = monthly_returns.iloc[r_start: r_end + 1]  # end_lag - start_lag months
        # Compound: (1+r1)(1+r2)...(1+rN) - 1
        cum_ret = (1 + window).prod() - 1
        result.loc[row_idx] = cum_ret.values

    return result.astype(float)


# ---------------------------------------------------------------------------
# Cross-Sectional Momentum
# ---------------------------------------------------------------------------

def compute_cs_momentum(
    prices: pd.DataFrame,
    lookback_months: int = 12,
    skip_months: int = 1,
    zscore_window: int = 36,
) -> pd.DataFrame:
    """
    Compute cross-sectional momentum signal (z-scored).

    Steps:
        1. Resample daily prices to month-end.
        2. Compute (lookback_months - skip_months) period return, skipping last
           skip_months to avoid short-term reversal.
        3. Cross-sectionally z-score across sectors at each date.
        4. Apply rolling z-score normalization over zscore_window months.

    Parameters
    ----------
    prices : pd.DataFrame
        Daily adjusted close prices. DatetimeIndex, columns = tickers.
    lookback_months : int
        Total lookback window (default 12 months).
    skip_months : int
        Months to skip at the recent end (default 1 for reversal avoidance).
    zscore_window : int
        Rolling months window for time-series z-score normalization (default 36).

    Returns
    -------
    pd.DataFrame
        Month-end z-scored CS momentum signals. DatetimeIndex (ME), columns = tickers.
        NaN for periods with insufficient history.
    """
    monthly_prices = prices.resample("ME").last()
    monthly_returns = monthly_prices.pct_change()

    # Raw momentum: cumulative return from lookback_months ago to skip_months ago
    raw_mom = _cumulative_return(monthly_returns, start_lag=skip_months, end_lag=lookback_months)

    # Cross-sectional z-score at each date (across sectors)
    cs_zscore = raw_mom.sub(raw_mom.mean(axis=1), axis=0).div(
        raw_mom.std(axis=1).replace(0, np.nan), axis=0
    )

    # Rolling time-series z-score normalization (optional, makes scale stable)
    if zscore_window > 0:
        rolling_mean = cs_zscore.rolling(window=zscore_window, min_periods=zscore_window // 2).mean()
        rolling_std = cs_zscore.rolling(window=zscore_window, min_periods=zscore_window // 2).std()
        cs_zscore = (cs_zscore - rolling_mean) / rolling_std.replace(0, np.nan)

    logger.debug(
        f"CS momentum computed: {lookback_months}-{skip_months}m, "
        f"zscore_window={zscore_window}m, "
        f"valid rows={cs_zscore.dropna(how='all').shape[0]}"
    )

    return cs_zscore


# ---------------------------------------------------------------------------
# Time-Series Momentum
# ---------------------------------------------------------------------------

def compute_ts_momentum(
    prices: pd.DataFrame,
    lookback_months: int = 12,
    skip_months: int = 1,
    crash_filter_multiplier: float = 0.0,
) -> pd.DataFrame:
    """
    Compute time-series momentum signal (crash filter).

    Returns a multiplier DataFrame: 1.0 if the sector's own return over
    [lookback_months, skip_months] is positive, else crash_filter_multiplier.

    This is used as a MULTIPLIER on cs_momentum to switch off trending signals
    during sector drawdowns (AQR crash filter approach).

    Parameters
    ----------
    prices : pd.DataFrame
        Daily adjusted close prices.
    lookback_months : int
        Lookback for self-return (default 12).
    skip_months : int
        Skip most recent N months (default 1).
    crash_filter_multiplier : float
        Weight to apply when ts_mom < 0. 0 = fully exclude; 0.5 = half weight.

    Returns
    -------
    pd.DataFrame
        Month-end multipliers (values in {crash_filter_multiplier, 1.0}).
        NaN for insufficient history.
    """
    monthly_prices = prices.resample("ME").last()
    monthly_returns = monthly_prices.pct_change()

    # Self-return: same as cs_mom but for individual sectors
    self_return = _cumulative_return(
        monthly_returns, start_lag=skip_months, end_lag=lookback_months
    )

    # Crash filter multiplier
    ts_mult = self_return.copy()
    ts_mult[self_return > 0] = 1.0
    ts_mult[self_return <= 0] = crash_filter_multiplier
    # Keep NaN as NaN
    ts_mult[self_return.isna()] = np.nan

    logger.debug(
        f"TS momentum multiplier: lookback={lookback_months}m skip={skip_months}m "
        f"crash_mult={crash_filter_multiplier}"
    )

    return ts_mult.astype(float)


# ---------------------------------------------------------------------------
# Momentum Acceleration
# ---------------------------------------------------------------------------

def compute_acceleration(
    prices: pd.DataFrame,
    short_months: int = 3,
    long_months: int = 12,
    skip_months: int = 1,
) -> pd.DataFrame:
    """
    Compute momentum acceleration: short-term momentum minus long-term momentum.

    Sectors with rising momentum (positive acceleration) get a signal boost.
    Sectors with decelerating momentum (negative acceleration) get reduced weight.

    Parameters
    ----------
    prices : pd.DataFrame
        Daily adjusted close prices.
    short_months : int
        Short-term lookback (default 3).
    long_months : int
        Long-term lookback (default 12).
    skip_months : int
        Skip most recent N months.

    Returns
    -------
    pd.DataFrame
        Cross-sectionally z-scored acceleration signal.
    """
    monthly_prices = prices.resample("ME").last()
    monthly_returns = monthly_prices.pct_change()

    short_ret = _cumulative_return(monthly_returns, start_lag=skip_months, end_lag=short_months)
    long_ret = _cumulative_return(monthly_returns, start_lag=skip_months, end_lag=long_months)

    accel_raw = short_ret - long_ret

    # Cross-sectional z-score
    accel_z = accel_raw.sub(accel_raw.mean(axis=1), axis=0).div(
        accel_raw.std(axis=1).replace(0, np.nan), axis=0
    )

    return accel_z.astype(float)


# ---------------------------------------------------------------------------
# Combined momentum output for composite signal
# ---------------------------------------------------------------------------

def compute_all_momentum(
    prices: pd.DataFrame,
    cs_lookback: int = 12,
    cs_skip: int = 1,
    cs_zscore_window: int = 36,
    ts_lookback: int = 12,
    ts_skip: int = 1,
    ts_crash_mult: float = 0.0,
    accel_enabled: bool = True,
    accel_short: int = 3,
    accel_long: int = 12,
) -> dict[str, pd.DataFrame]:
    """
    Compute all momentum-based signals at once.

    Returns
    -------
    dict with keys:
        "cs_mom"    : Cross-sectional momentum z-score (month-end)
        "ts_mult"   : Time-series momentum crash filter multiplier (month-end)
        "accel"     : Acceleration z-score (month-end), or None if disabled
    """
    cs = compute_cs_momentum(
        prices,
        lookback_months=cs_lookback,
        skip_months=cs_skip,
        zscore_window=cs_zscore_window,
    )
    ts = compute_ts_momentum(
        prices,
        lookback_months=ts_lookback,
        skip_months=ts_skip,
        crash_filter_multiplier=ts_crash_mult,
    )
    accel = None
    if accel_enabled:
        accel = compute_acceleration(
            prices,
            short_months=accel_short,
            long_months=accel_long,
            skip_months=cs_skip,
        )

    return {"cs_mom": cs, "ts_mult": ts, "accel": accel}


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s: %(message)s")

    # Quick smoke test with sample data
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent.parent))
    from sector_rotation.data.loader import load_all

    prices, _ = load_all()
    etf_prices = prices.drop(columns=["SPY"], errors="ignore")

    signals = compute_all_momentum(etf_prices)

    print("\n=== CS Momentum (last 3 months) ===")
    print(signals["cs_mom"].tail(3).to_string())

    print("\n=== TS Momentum Crash Filter (last 3 months) ===")
    print(signals["ts_mult"].tail(3).to_string())

    print("\n=== Acceleration (last 3 months) ===")
    if signals["accel"] is not None:
        print(signals["accel"].tail(3).to_string())
