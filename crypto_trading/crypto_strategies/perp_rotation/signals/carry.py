"""Carry (funding) Signal — replaces the equity value factor (Plan 05 §5/§6).

COPIED STRUCTURE from qlib-main/sector_rotation/signals/value.py (read-only
template): ``pe_to_percentile`` → ``funding_to_percentile`` (identical rolling
percentile-of-last-observation), ``compute_value_signal`` →
``compute_carry_signal`` (identical invert + cross-sectional z + missing-data
fill). The earnings/EPS plumbing (SECTOR_REPRESENTATIVES, yfinance/polygon
fetchers) does not apply to funding and is not copied.

SIGN LOGIC (explicit and configurable — measured reality 2026-07-07: Kalshi
funding skew is cross-sectionally rich, BTC +5.4%/yr vs BCH −12.8%/yr):
  A LONG position's funding P&L per cycle = −rate × notional
  (crypto_common.costs.funding_payment). So a long RECEIVES when funding is
  NEGATIVE. This is a LONG-TILT rotation (plan §1) →
    favor="long_receives" (default): LOW funding percentile → HIGH signal
      (identical inversion shape to the template's low-P/E → high-value).
    favor="short_receives": flips the sign (for a future short-enabled book).
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def funding_to_percentile(
    funding_series: pd.Series,
    lookback_days: int = 90,
    window_min_periods: int = 21,
) -> pd.Series:
    """Rolling historical percentile of the last observation (template:
    ``pe_to_percentile``, window in days instead of months).

    Percentile = fraction of trailing window values BELOW current value.
    HIGHER percentile = funding rich vs own history (bad for longs)."""
    window = int(lookback_days)
    window_min_periods = min(window_min_periods, window)   # short-history clamp

    def pct_rank(x):
        if len(x) < window_min_periods or np.isnan(x.iloc[-1]):
            return np.nan
        return (x.iloc[:-1] < x.iloc[-1]).mean()

    return funding_series.rolling(window=window, min_periods=window_min_periods).apply(
        pct_rank, raw=False
    )


def compute_carry_signal(
    funding_panel: pd.DataFrame,
    lookback_days: int = 90,
    missing_data_weight: float = 0.0,
    favor: str = "long_receives",
    min_history_days: int = 21,
    mode: str = "percentile",
    level_smooth_days: int = 7,
) -> pd.DataFrame:
    """Carry signal from the daily funding panel (template:
    ``compute_value_signal`` with P/E → funding).

    mode="percentile" (template shape): signal = 1 − rolling percentile vs the
      ticker's OWN history — a time-series-relative tilt.
    mode="level" (upgrade — measured Kalshi cross-section is rich: BTC +5.4%/yr
      vs BCH −12.8%/yr): signal = −funding LEVEL (smoothed over
      ``level_smooth_days``), so the perp whose funding a LONG collects most
      (most negative funding) scores highest ACROSS the panel — the long-short-
      aware ranking, using the cross-section directly instead of own-history.
    Both are then cross-sectionally z-scored; insufficient history →
    ``missing_data_weight``.
    """
    if favor not in ("long_receives", "short_receives"):
        raise ValueError(f"unknown favor={favor!r}")
    if mode not in ("percentile", "level"):
        raise ValueError(f"unknown mode={mode!r}")

    if mode == "level":
        smooth = funding_panel.rolling(level_smooth_days,
                                       min_periods=max(2, level_smooth_days // 2)).mean()
        # long collects −funding: most-negative funding → highest raw score
        carry_raw = -smooth if favor == "long_receives" else smooth
    else:
        pct = pd.DataFrame(index=funding_panel.index, columns=funding_panel.columns,
                           dtype=float)
        for col in funding_panel.columns:
            if funding_panel[col].notna().sum() >= min_history_days:
                pct[col] = funding_to_percentile(funding_panel[col],
                                                 lookback_days=lookback_days,
                                                 window_min_periods=min_history_days)
            else:
                pct[col] = np.nan
                logger.debug(f"Insufficient funding history for {col}; missing_data_weight.")
        carry_raw = (1.0 - pct) if favor == "long_receives" else pct

    def cs_zscore_row(row):
        valid = row.dropna()
        if len(valid) < 2:
            return row
        return (row - valid.mean()) / valid.std()

    carry_z = carry_raw.apply(cs_zscore_row, axis=1)
    carry_z = carry_z.fillna(missing_data_weight)

    logger.debug(f"Carry signal computed: {carry_z.dropna(how='all').shape[0]} valid days")
    return carry_z
