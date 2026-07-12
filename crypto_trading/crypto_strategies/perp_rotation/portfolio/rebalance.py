"""Rebalancing Logic — crypto perps (Plan 05 §7).

COPIED from qlib-main/sector_rotation/portfolio/rebalance.py (read-only
template). Preserved verbatim: z-score threshold filter, turnover computation,
turnover cap (binary-search blend), emergency-rebalance cooldown logic,
schedule generator shape.

Adaptations (plan "Change" column only):
  * Calendar: NYSE/pandas_market_calendars → 24/7 — every day is a trading
    day (plain pd.date_range, tz-aware UTC). Monthly cadence → daily|weekly
    (plan §9 rebalance.frequency).
  * Emergency trigger: VIX → btc_rvol column of the crypto regime-input frame
    (threshold 60 = the regime module's crisis bracket; calibrate).
"""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Trading calendar utilities — 24/7 adaptation
# ---------------------------------------------------------------------------

def get_trading_days(start: str, end: str) -> pd.DatetimeIndex:
    """All days are trading days on a 24/7 venue (template: NYSE calendar)."""
    return pd.date_range(start=start, end=end, freq="1D", tz="UTC")


def get_rebalance_dates(
    start: str,
    end: str,
    frequency: str = "daily",
) -> List[pd.Timestamp]:
    """Rebalance dates in [start, end] (template: first-trading-day-of-month).

    frequency: "daily" (every UTC day) | "weekly" (Mondays UTC).
    """
    days = get_trading_days(start, end)
    if frequency == "daily":
        return list(days)
    if frequency == "weekly":
        return [d for d in days if d.dayofweek == 0]
    raise ValueError(f"unknown rebalance frequency {frequency!r} (daily|weekly)")


# ---------------------------------------------------------------------------
# Threshold filter — template verbatim
# ---------------------------------------------------------------------------

def apply_zscore_threshold_filter(
    new_scores: pd.Series,
    prev_scores: pd.Series,
    new_weights: pd.Series,
    prev_weights: pd.Series,
    threshold: float = 0.5,
) -> Tuple[pd.Series, List[str], List[str]]:
    """Skip rebalancing perps whose |z change| < threshold (turnover saver)."""
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

    target_sum = new_weights.sum()
    if w_out.sum() > 0:
        w_out = w_out / w_out.sum() * target_sum

    if held:
        logger.debug(f"Threshold filter held {len(held)} perps: {held}")

    return w_out, rebalanced, held


# ---------------------------------------------------------------------------
# Turnover computation — template verbatim
# ---------------------------------------------------------------------------

def compute_turnover(
    new_weights: pd.Series,
    prev_weights: pd.Series,
) -> float:
    """Single-side turnover = 0.5 × Σ|Δw|."""
    all_tickers = new_weights.index.union(prev_weights.index)
    new_aligned = new_weights.reindex(all_tickers, fill_value=0.0)
    prev_aligned = prev_weights.reindex(all_tickers, fill_value=0.0)
    return float(0.5 * (new_aligned - prev_aligned).abs().sum())


def cap_turnover(
    new_weights: pd.Series,
    prev_weights: pd.Series,
    max_turnover: float = 0.80,
) -> pd.Series:
    """Blend toward previous weights when turnover exceeds the cap (verbatim
    binary search on the blend α)."""
    to = compute_turnover(new_weights, prev_weights)
    if to <= max_turnover:
        return new_weights

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
# Rebalance decision — template cooldown mechanics, btc_rvol input
# ---------------------------------------------------------------------------

def should_emergency_rebalance(
    regime_inputs: pd.DataFrame,
    current_weights: pd.Series,
    rvol_threshold: float = 60.0,
    emergency_active: bool = False,
    rvol_recovery_factor: float = 0.80,
) -> bool:
    """Emergency de-risk trigger with cooldown (template logic verbatim;
    VIX → btc_rvol, threshold 60 = crisis bracket, calibrate on recorded data).

    Triggers on the FIRST crossing above threshold; suppressed while
    emergency_active until rvol recovers below threshold × recovery_factor.
    """
    if "btc_rvol" not in regime_inputs.columns or len(regime_inputs) == 0:
        return False
    rvol_series = regime_inputs["btc_rvol"].dropna()
    if len(rvol_series) == 0:
        return False
    current_rvol = float(rvol_series.iloc[-1])

    if current_rvol > rvol_threshold:
        if not emergency_active:
            logger.warning(
                f"Emergency rebalance triggered: btc_rvol={current_rvol:.1f} > {rvol_threshold}"
            )
            return True
        else:
            return False
    return False


# ---------------------------------------------------------------------------
# Main rebalance event generator — template shape, 24/7
# ---------------------------------------------------------------------------

def generate_rebalance_schedule(
    start: str,
    end: str,
    regime_inputs: pd.DataFrame,
    rvol_emergency_threshold: float = 60.0,
    frequency: str = "daily",
) -> pd.DataFrame:
    """Full schedule: date × {rebalance_type, rvol_at_date, is_scheduled}."""
    all_days = get_trading_days(start, end)
    scheduled = set(get_rebalance_dates(start, end, frequency))

    rows = []
    for dt in all_days:
        rvol_val = float("nan")
        if "btc_rvol" in regime_inputs.columns and dt in regime_inputs.index:
            v = regime_inputs.loc[dt, "btc_rvol"]
            rvol_val = float(v) if not pd.isna(v) else float("nan")

        is_emergency = (not np.isnan(rvol_val)) and (rvol_val > rvol_emergency_threshold)
        is_scheduled = dt in scheduled

        if is_emergency:
            rtype = "emergency"
        elif is_scheduled:
            rtype = "scheduled"
        else:
            rtype = "none"

        rows.append({"date": dt, "rebalance_type": rtype,
                     "rvol_at_date": rvol_val, "is_scheduled": is_scheduled})

    return pd.DataFrame(rows).set_index("date")
