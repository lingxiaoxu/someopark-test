"""Momentum Signals — crypto perps (Plan 05 §6).

COPIED from qlib-main/sector_rotation/signals/momentum.py (read-only template).
All function names, structure, and the References/semantics preserved.
Adaptations (plan §5 "Change" column only):
  * Time base: month-end bars → 24/7 DAILY bars; every ``*_months`` parameter
    becomes ``*_days`` (defaults per plan §9: CS lookback 30d skip 1d, z-window
    90d; TS lookback 30d; accel 7d vs 30d).
  * No resample("ME") — the input panel is already daily.
The row-loop in ``_cumulative_return`` is kept verbatim (faithful copy; fine
at daily scale for a 13-perp panel).

References (template): Moskowitz/Ooi/Pedersen (2012) time-series momentum;
Gupta & Kelly (2019) factor momentum; Jegadeesh & Titman (1993) reversal skip.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Rolling return utilities  (template structure preserved)
# ---------------------------------------------------------------------------

def _cumulative_return(daily_returns: pd.DataFrame, start_lag: int, end_lag: int) -> pd.DataFrame:
    """Rolling cumulative return from end_lag to start_lag days ago.

    For day t: product(1 + r_{t-end_lag} … r_{t-start_lag}) − 1.
    NaN where < end_lag days of data exist. (Template loop kept verbatim.)
    """
    assert end_lag > start_lag >= 0, "end_lag must be > start_lag >= 0"

    result = pd.DataFrame(index=daily_returns.index, columns=daily_returns.columns,
                          dtype=float)
    for i in range(len(daily_returns)):
        row_idx = daily_returns.index[i]
        r_start = i - end_lag
        r_end = i - start_lag  # inclusive
        if r_start < 0 or r_end < 0 or r_end < r_start:
            result.loc[row_idx] = np.nan
            continue
        window = daily_returns.iloc[r_start: r_end + 1]
        cum_ret = (1 + window).prod() - 1
        result.loc[row_idx] = cum_ret.values

    return result.astype(float)


# ---------------------------------------------------------------------------
# Cross-Sectional Momentum
# ---------------------------------------------------------------------------

def compute_cs_momentum(
    prices: pd.DataFrame,
    lookback_days: int = 30,
    skip_days: int = 1,
    zscore_window: int = 90,
) -> pd.DataFrame:
    """Cross-sectional momentum z-score (template steps preserved):
    1. daily returns; 2. cumulative return [lookback_days → skip_days] ago
    (skip avoids short-term reversal); 3. cross-sectional z across perps;
    4. rolling time-series z-normalization over ``zscore_window`` days."""
    daily_returns = prices.pct_change()

    raw_mom = _cumulative_return(daily_returns, start_lag=skip_days, end_lag=lookback_days)

    cs_zscore = raw_mom.sub(raw_mom.mean(axis=1), axis=0).div(
        raw_mom.std(axis=1).replace(0, np.nan), axis=0
    )

    if zscore_window > 0:
        rolling_mean = cs_zscore.rolling(window=zscore_window, min_periods=zscore_window // 2).mean()
        rolling_std = cs_zscore.rolling(window=zscore_window, min_periods=zscore_window // 2).std()
        cs_zscore = (cs_zscore - rolling_mean) / rolling_std.replace(0, np.nan)

    logger.debug(
        f"CS momentum computed: {lookback_days}-{skip_days}d, "
        f"zscore_window={zscore_window}d, "
        f"valid rows={cs_zscore.dropna(how='all').shape[0]}"
    )
    return cs_zscore


# ---------------------------------------------------------------------------
# Time-Series Momentum (crash filter)
# ---------------------------------------------------------------------------

def compute_ts_momentum(
    prices: pd.DataFrame,
    lookback_days: int = 30,
    skip_days: int = 1,
    crash_filter_multiplier: float = 0.0,
) -> pd.DataFrame:
    """Own-asset trend multiplier: 1.0 if the perp's return over
    [lookback_days, skip_days] is positive, else ``crash_filter_multiplier``
    (AQR crash-filter approach — template semantics verbatim)."""
    daily_returns = prices.pct_change()
    self_return = _cumulative_return(daily_returns, start_lag=skip_days, end_lag=lookback_days)

    ts_mult = self_return.copy()
    ts_mult[self_return > 0] = 1.0
    ts_mult[self_return <= 0] = crash_filter_multiplier
    ts_mult[self_return.isna()] = np.nan

    logger.debug(
        f"TS momentum multiplier: lookback={lookback_days}d skip={skip_days}d "
        f"crash_mult={crash_filter_multiplier}"
    )
    return ts_mult.astype(float)


# ---------------------------------------------------------------------------
# Momentum Acceleration
# ---------------------------------------------------------------------------

def compute_acceleration(
    prices: pd.DataFrame,
    short_days: int = 7,
    long_days: int = 30,
    skip_days: int = 1,
) -> pd.DataFrame:
    """Short-minus-long momentum, cross-sectionally z-scored (template)."""
    daily_returns = prices.pct_change()
    short_ret = _cumulative_return(daily_returns, start_lag=skip_days, end_lag=short_days)
    long_ret = _cumulative_return(daily_returns, start_lag=skip_days, end_lag=long_days)

    accel_raw = short_ret - long_ret
    accel_z = accel_raw.sub(accel_raw.mean(axis=1), axis=0).div(
        accel_raw.std(axis=1).replace(0, np.nan), axis=0
    )
    return accel_z.astype(float)


# ---------------------------------------------------------------------------
# Combined momentum output for composite signal  (template interface)
# ---------------------------------------------------------------------------

def compute_all_momentum(
    prices: pd.DataFrame,
    cs_lookback: int = 30,
    cs_skip: int = 1,
    cs_zscore_window: int = 90,
    ts_lookback: int = 30,
    ts_skip: int = 1,
    ts_crash_mult: float = 0.0,
    accel_enabled: bool = True,
    accel_short: int = 7,
    accel_long: int = 30,
) -> dict[str, pd.DataFrame]:
    """All momentum signals at once — keys identical to the template:
    "cs_mom" (daily z), "ts_mult" (daily multiplier), "accel" (daily z|None)."""
    cs = compute_cs_momentum(prices, lookback_days=cs_lookback, skip_days=cs_skip,
                             zscore_window=cs_zscore_window)
    ts = compute_ts_momentum(prices, lookback_days=ts_lookback, skip_days=ts_skip,
                             crash_filter_multiplier=ts_crash_mult)
    accel = None
    if accel_enabled:
        accel = compute_acceleration(prices, short_days=accel_short,
                                     long_days=accel_long, skip_days=cs_skip)
    return {"cs_mom": cs, "ts_mult": ts, "accel": accel}
