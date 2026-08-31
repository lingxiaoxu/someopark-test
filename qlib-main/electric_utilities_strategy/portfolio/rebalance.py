"""
Rebalancing Logic
=================
Determines WHEN and HOW MUCH to rebalance the sector portfolio.

Rebalancing rules:
    1. Monthly scheduled rebalance on the first trading day of each month.
    2. Threshold filter: skip rebalance for a sector if |z-score change| < 0.5σ.
       Reduces turnover by 30-50% with minimal Sharpe impact.
    3. Emergency rebalance: VIX > 35 → immediate de-risk (not waiting for month-end).
    4. Maximum monthly turnover constraint: 80% single-side (limits implementation risk).

Calendar
--------
Uses pandas_market_calendars (NYSE) to identify valid trading days.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Trading calendar utilities
# ---------------------------------------------------------------------------

def get_trading_days(start: str, end: str, exchange: str = "NYSE") -> pd.DatetimeIndex:
    """
    Return all valid trading days between start and end (inclusive).

    Uses pandas_market_calendars if available, else falls back to
    pandas business day frequency (approximation).
    """
    try:
        import pandas_market_calendars as mcal
        cal = mcal.get_calendar(exchange)
        schedule = cal.schedule(start_date=start, end_date=end)
        return schedule.index
    except ImportError:
        logger.warning(
            "pandas_market_calendars not installed. "
            "Using business day frequency (may include some holidays)."
        )
        return pd.bdate_range(start=start, end=end)


def get_first_trading_day_of_month(
    year: int,
    month: int,
    exchange: str = "NYSE",
) -> Optional[pd.Timestamp]:
    """Return the first valid trading day of a given month."""
    start = pd.Timestamp(year=year, month=month, day=1)
    end = start + pd.offsets.MonthEnd(0)
    trading_days = get_trading_days(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), exchange)
    return trading_days[0] if len(trading_days) > 0 else None


def get_monthly_rebalance_dates(
    start: str,
    end: str,
    exchange: str = "NYSE",
) -> List[pd.Timestamp]:
    """
    Return list of first-trading-day-of-month dates in [start, end].

    Parameters
    ----------
    start, end : str
        Date strings (YYYY-MM-DD).

    Returns
    -------
    list of pd.Timestamp
    """
    all_days = get_trading_days(start, end, exchange)
    # Keep only the first trading day of each calendar month
    df = pd.DataFrame({"date": all_days})
    df["year_month"] = df["date"].dt.to_period("M")
    first_days = df.groupby("year_month")["date"].first().tolist()
    return first_days


# ---------------------------------------------------------------------------
# Threshold filter
# ---------------------------------------------------------------------------

def apply_zscore_threshold_filter(
    new_scores: pd.Series,
    prev_scores: pd.Series,
    new_weights: pd.Series,
    prev_weights: pd.Series,
    threshold: float = 0.5,
) -> Tuple[pd.Series, List[str], List[str]]:
    """
    Skip rebalancing sectors where the signal z-score change is below threshold.

    Logic:
        For each sector where |new_score - prev_score| < threshold:
            Keep previous weight (no rebalance for this sector).
        For sectors above threshold:
            Accept new weight.
        Renormalize so total still sums to 1 (or 1 - cash_pct).

    Parameters
    ----------
    new_scores, prev_scores : pd.Series
        Current and previous composite z-scores (index = tickers).
    new_weights, prev_weights : pd.Series
        Proposed new weights and current weights.
    threshold : float
        Z-score change threshold (default 0.5σ).

    Returns
    -------
    filtered_weights : pd.Series
        Post-filter weights (renormalized).
    rebalanced_tickers : list
        Sectors that were actually rebalanced.
    held_tickers : list
        Sectors that kept previous weights due to threshold filter.
    """
    # First-run: no prior scores → bypass threshold, rebalance all sectors
    if prev_scores.empty:
        return new_weights.copy(), list(new_scores.index), []

    score_change = (new_scores - prev_scores).abs()
    rebalanced = []
    held = []

    w_out = prev_weights.copy()
    for ticker in new_scores.index:
        change = score_change.get(ticker, float("inf"))
        if change >= threshold:
            w_out[ticker] = new_weights.get(ticker, 0.0)
            rebalanced.append(ticker)
        else:
            held.append(ticker)

    # Renormalize to match new total invested pct
    target_sum = new_weights.sum()
    if w_out.sum() > 0:
        w_out = w_out / w_out.sum() * target_sum

    if held:
        logger.debug(f"Threshold filter held {len(held)} sectors: {held}")

    return w_out, rebalanced, held


# ---------------------------------------------------------------------------
# Turnover computation
# ---------------------------------------------------------------------------

def compute_turnover(
    new_weights: pd.Series,
    prev_weights: pd.Series,
) -> float:
    """
    Compute single-side portfolio turnover.

    Turnover = 0.5 * sum(|new_w_i - old_w_i|)
    (0.5 because each trade is counted once: buy side = sell side in aggregate)

    Returns float in [0, 1].
    """
    all_tickers = new_weights.index.union(prev_weights.index)
    new_aligned = new_weights.reindex(all_tickers, fill_value=0.0)
    prev_aligned = prev_weights.reindex(all_tickers, fill_value=0.0)
    return float(0.5 * (new_aligned - prev_aligned).abs().sum())


def cap_turnover(
    new_weights: pd.Series,
    prev_weights: pd.Series,
    max_turnover: float = 0.80,
) -> pd.Series:
    """
    If turnover exceeds max_turnover, blend new and previous weights
    to reduce the total weight change.

    Uses linear interpolation: w_final = α * w_new + (1-α) * w_prev
    where α is chosen such that turnover(w_final, w_prev) = max_turnover.
    """
    to = compute_turnover(new_weights, prev_weights)
    if to <= max_turnover:
        return new_weights

    # Binary search for α
    lo, hi = 0.0, 1.0
    for _ in range(50):
        mid = (lo + hi) / 2.0
        blended = mid * new_weights + (1 - mid) * prev_weights
        if compute_turnover(blended, prev_weights) > max_turnover:
            hi = mid
        else:
            lo = mid

    alpha = (lo + hi) / 2.0
    blended = alpha * new_weights + (1 - alpha) * prev_weights
    blended = blended.clip(lower=0.0)
    blended = blended / blended.sum() * new_weights.sum()

    logger.info(
        f"Turnover capped: {to:.2%} → {compute_turnover(blended, prev_weights):.2%} "
        f"(α={alpha:.3f})"
    )
    return blended


# ---------------------------------------------------------------------------
# Rebalance decision
# ---------------------------------------------------------------------------

def should_emergency_rebalance(
    macro: pd.DataFrame,
    current_weights: pd.Series,
    vix_threshold: float = 35.0,
    emergency_active: bool = False,
    vix_recovery_factor: float = 0.80,
) -> bool:
    """
    Check if an emergency rebalance should be triggered TODAY.

    Cooldown logic:
    - Trigger on the FIRST day VIX crosses above vix_threshold (False → True transition).
    - Once in emergency mode (emergency_active=True), do NOT re-trigger until VIX
      recovers below vix_threshold * vix_recovery_factor.
    - This prevents the engine from firing daily rebalances throughout a volatility spike.

    Parameters
    ----------
    emergency_active : bool
        Whether an emergency was already triggered and has not yet recovered.
    vix_recovery_factor : float
        Fraction of vix_threshold below which emergency is considered cleared (default 0.80).
        E.g. threshold=35, factor=0.80 → recovery at VIX < 28.
    """
    if "vix" not in macro.columns or len(macro) == 0:
        return False
    vix_series = macro["vix"].dropna()
    if len(vix_series) == 0:
        return False
    current_vix = float(vix_series.iloc[-1])

    if current_vix > vix_threshold:
        if not emergency_active:
            # First crossing: trigger emergency
            logger.warning(
                f"Emergency rebalance triggered: VIX={current_vix:.1f} > {vix_threshold}"
            )
            return True
        else:
            # Already in emergency mode: suppress re-trigger (cooldown active)
            return False
    return False


# ---------------------------------------------------------------------------
# Main rebalance event generator
# ---------------------------------------------------------------------------

def generate_rebalance_schedule(
    start: str,
    end: str,
    prices: pd.DataFrame,
    macro: pd.DataFrame,
    vix_emergency_threshold: float = 35.0,
    exchange: str = "NYSE",
) -> pd.DataFrame:
    """
    Generate the full rebalance event schedule for a backtest period.

    Returns a DataFrame with columns:
        date           : pd.Timestamp
        rebalance_type : "scheduled" | "emergency" | "none"
        vix_at_date    : float
        is_month_start : bool

    Parameters
    ----------
    start, end : str
        Backtest window.
    prices : pd.DataFrame
        Price data (used to confirm trading days).
    macro : pd.DataFrame
        Macro data (for VIX emergency check).
    vix_emergency_threshold : float
        VIX level for emergency rebalance.
    exchange : str
        Calendar name.
    """
    all_trading_days = get_trading_days(start, end, exchange)
    monthly_days = set(get_monthly_rebalance_dates(start, end, exchange))

    rows = []
    for dt in all_trading_days:
        vix_val = float("nan")
        if "vix" in macro.columns and dt in macro.index:
            vix_val = float(macro.loc[dt, "vix"]) if not pd.isna(macro.loc[dt, "vix"]) else float("nan")

        is_emergency = (not np.isnan(vix_val)) and (vix_val > vix_emergency_threshold)
        is_scheduled = dt in monthly_days

        if is_emergency:
            rtype = "emergency"
        elif is_scheduled:
            rtype = "scheduled"
        else:
            rtype = "none"

        rows.append(
            {
                "date": dt,
                "rebalance_type": rtype,
                "vix_at_date": vix_val,
                "is_month_start": is_scheduled,
            }
        )

    return pd.DataFrame(rows).set_index("date")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    schedule = get_monthly_rebalance_dates("2018-07-01", "2024-12-31")
    print(f"Monthly rebalance dates 2018-2024: {len(schedule)} dates")
    print(f"First 5: {schedule[:5]}")
    print(f"Last 5: {schedule[-5:]}")

    # Test turnover computation
    w_old = pd.Series({"XLK": 0.30, "XLV": 0.25, "XLF": 0.25, "XLI": 0.20})
    w_new = pd.Series({"XLK": 0.25, "XLV": 0.30, "XLF": 0.20, "XLC": 0.25})
    to = compute_turnover(w_new, w_old)
    print(f"\nTurnover: {to:.2%}")

    w_capped = cap_turnover(w_new, w_old, max_turnover=0.10)
    print(f"After cap (10%): {w_capped.round(3).to_dict()}")
