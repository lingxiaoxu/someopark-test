"""
Composite Signal
================
Multi-factor signal aggregation with regime conditioning.

Architecture
------------
1. Compute individual signals (cs_mom, ts_mult, value, accel).
2. Apply time-series momentum as a crash filter multiplier.
3. Apply regime-conditional weight adjustments.
4. Add defensive sector bonus in risk-off regimes.
5. Final composite z-score → portfolio weight input.

Signal Weights (default from config.yaml)
------------------------------------------
    cross_sectional_momentum : 0.40
    ts_momentum (via multiplier): 0.15 (reduces cs_mom in crash states)
    relative_value           : 0.20
    regime_adjustment        : 0.25

The regime adjustment is NOT a standalone signal — it modifies the weights of
the other signals. So the realized weight attribution is:
    - In RISK_ON : cs_mom ~40%, value ~20%, no adjustment needed → ~60% from signals
    - In RISK_OFF: cs_mom ~24%, value ~24%, regime bonus pushes defensives → regime matters more

Signal → Portfolio Flow
------------------------
    compute_composite_signals() →  monthly z-score DataFrame
    → portfolio/optimizer.py uses z-scores to determine allocations
"""

from __future__ import annotations

import logging
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

from .momentum import compute_all_momentum
from .value import compute_value_signal_full
from .regime import compute_regime, regime_to_monthly, RISK_OFF, RISK_ON, TRANSITION_DOWN, TRANSITION_UP

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default signal weights (can be overridden via config)
# ---------------------------------------------------------------------------

DEFAULT_WEIGHTS = {
    "cross_sectional_momentum": 0.40,
    "ts_momentum": 0.15,
    "relative_value": 0.20,
    "regime_adjustment": 0.25,
}

DEFAULT_REGIME_WEIGHT_MULTIPLIERS = {
    RISK_ON: {
        "cross_sectional_momentum": 1.0,
        "ts_momentum": 1.0,
        "relative_value": 1.0,
    },
    TRANSITION_UP: {
        "cross_sectional_momentum": 1.1,
        "ts_momentum": 1.0,
        "relative_value": 0.9,
    },
    TRANSITION_DOWN: {
        "cross_sectional_momentum": 0.7,
        "ts_momentum": 0.9,
        "relative_value": 1.1,
    },
    RISK_OFF: {
        "cross_sectional_momentum": 0.6,
        "ts_momentum": 0.8,
        "relative_value": 1.2,
    },
}

DEFENSIVE_TICKERS = ["XLU", "XLP", "XLV"]
DEFENSIVE_BONUS_RISK_OFF = 0.30   # Added to composite z-score in RISK_OFF


# ---------------------------------------------------------------------------
# Core composite function
# ---------------------------------------------------------------------------

def compute_composite_signals(
    prices: pd.DataFrame,
    macro: pd.DataFrame,
    weights: Optional[Dict[str, float]] = None,
    regime_multipliers: Optional[Dict[str, Dict[str, float]]] = None,
    defensive_tickers: Optional[list] = None,
    defensive_bonus: float = DEFENSIVE_BONUS_RISK_OFF,
    regime_method: str = "rules",
    value_source: str = "constituents",
    value_cache_dir=None,
    polygon_api_key: Optional[str] = None,
    regime_kwargs: Optional[dict] = None,
    signal_kwargs: Optional[dict] = None,
) -> Tuple[pd.DataFrame, pd.Series, Dict[str, pd.DataFrame]]:
    """
    Compute regime-conditioned composite signals for all sector ETFs.

    Parameters
    ----------
    prices : pd.DataFrame
        Daily adjusted close prices. Columns = ETF tickers (no benchmark).
    macro : pd.DataFrame
        Daily macro indicators (output of loader.load_macro_data).
    weights : dict, optional
        Signal weights. Defaults to DEFAULT_WEIGHTS.
    regime_multipliers : dict, optional
        Regime-conditional signal weight multipliers.
    defensive_tickers : list, optional
        Tickers to receive bonus in RISK_OFF. Default: XLU, XLP, XLV.
    defensive_bonus : float
        Z-score bonus for defensives in RISK_OFF (default 0.30).
    regime_method : str
        "rules" or "hmm".
    value_source : str
        "constituents" (default) | "proxy" | "external" | "yfinance_info".
        "constituents" builds monthly TTM P/E from yfinance quarterly earnings
        of representative stocks per sector (see SECTOR_REPRESENTATIVES in value.py).
        "proxy" uses price-to-5yr-avg as a rough fallback (no earnings data needed).
    value_cache_dir : Path or str, optional
        Cache directory for constituent P/E data (passed to compute_value_signal_full).
    regime_kwargs : dict, optional
        Extra kwargs passed to compute_regime().
    signal_kwargs : dict, optional
        Extra kwargs for individual signal computation (cs_lookback, etc.).

    Returns
    -------
    composite : pd.DataFrame
        Month-end composite z-scores. DatetimeIndex (ME), columns = tickers.
    regime_monthly : pd.Series
        Month-end regime labels.
    components : dict
        Individual signal DataFrames for attribution/debugging:
        keys: "cs_mom", "ts_mult", "value", "accel", "regime_daily"
    """
    weights = weights or DEFAULT_WEIGHTS
    _validate_weights(weights)
    regime_multipliers = regime_multipliers or DEFAULT_REGIME_WEIGHT_MULTIPLIERS
    defensive_tickers = defensive_tickers or DEFENSIVE_TICKERS
    signal_kwargs = signal_kwargs or {}
    regime_kwargs = regime_kwargs or {}

    tickers = list(prices.columns)

    # -------------------------------------------------------------------------
    # Step 1: Compute individual signals
    # -------------------------------------------------------------------------
    logger.info("Computing momentum signals...")
    mom_signals = compute_all_momentum(
        prices,
        cs_lookback=signal_kwargs.get("cs_lookback", 12),
        cs_skip=signal_kwargs.get("cs_skip", 1),
        cs_zscore_window=signal_kwargs.get("cs_zscore_window", 36),
        ts_lookback=signal_kwargs.get("ts_lookback", 12),
        ts_skip=signal_kwargs.get("ts_skip", 1),
        ts_crash_mult=signal_kwargs.get("ts_crash_mult", 0.0),
        accel_enabled=signal_kwargs.get("accel_enabled", True),
        accel_short=signal_kwargs.get("accel_short", 3),
        accel_long=signal_kwargs.get("accel_long", 12),
    )
    cs_mom = mom_signals["cs_mom"]           # Monthly z-scores
    ts_mult = mom_signals["ts_mult"]         # Monthly multipliers
    accel = mom_signals["accel"]             # Monthly z-scores or None

    logger.info("Computing value signal...")
    value_sig = compute_value_signal_full(
        prices,
        source=value_source,
        lookback_years=signal_kwargs.get("value_lookback_years", 10.0),
        missing_data_weight=signal_kwargs.get("value_missing_weight", 0.0),
        cache_dir=value_cache_dir,
        polygon_api_key=polygon_api_key,
    )

    # Align value to monthly index of cs_mom
    value_aligned = _align_to_monthly_index(value_sig, cs_mom.index)

    logger.info("Computing regime...")
    regime_daily = compute_regime(macro, method=regime_method, **regime_kwargs)
    regime_monthly = regime_to_monthly(regime_daily)
    # Align to cs_mom monthly index
    regime_monthly = regime_monthly.reindex(cs_mom.index, method="ffill").fillna(RISK_ON)

    # -------------------------------------------------------------------------
    # Step 2: Build composite signal month by month
    # -------------------------------------------------------------------------
    composite = pd.DataFrame(index=cs_mom.index, columns=tickers, dtype=float)

    w_cs = weights.get("cross_sectional_momentum", 0.40)
    w_ts = weights.get("ts_momentum", 0.15)
    w_val = weights.get("relative_value", 0.20)
    # Regime adjustment weight is distributed proportionally — it multiplies the
    # effective weights of other signals, so we don't allocate it to a fixed signal.

    for dt in cs_mom.index:
        # Get regime at this date
        regime = regime_monthly.get(dt, RISK_ON)
        rm = regime_multipliers.get(regime, regime_multipliers[RISK_ON])

        # Regime-adjusted weights
        w_cs_adj = w_cs * rm.get("cross_sectional_momentum", 1.0)
        w_ts_adj = w_ts * rm.get("ts_momentum", 1.0)
        w_val_adj = w_val * rm.get("relative_value", 1.0)

        # Normalize adjusted weights to sum to (w_cs + w_ts + w_val)
        total_raw = w_cs + w_ts + w_val
        total_adj = w_cs_adj + w_ts_adj + w_val_adj
        if total_adj > 0:
            scale = total_raw / total_adj
            w_cs_adj *= scale
            w_ts_adj *= scale
            w_val_adj *= scale

        # Get signal row for this date
        cs_row = cs_mom.loc[dt] if dt in cs_mom.index else pd.Series(np.nan, index=tickers)
        ts_row = ts_mult.loc[dt] if dt in ts_mult.index else pd.Series(1.0, index=tickers)
        val_row = value_aligned.loc[dt] if dt in value_aligned.index else pd.Series(0.0, index=tickers)

        # Apply TS crash filter to cs_mom: cs_mom * ts_mult
        # ts_mult is 1.0 (full weight) or 0.0 (crash filter = exclude)
        cs_filtered = cs_row * ts_row

        # Weighted composite
        score = pd.Series(0.0, index=tickers)
        score += cs_filtered * w_cs_adj
        # TS contributes as a weight modifier (multiplier applied above),
        # but we also give it a direct small contribution as a standalone signal
        score += (ts_row - 0.5) * 2 * w_ts_adj  # Map {0,1} to {-1,+1} * weight
        score += val_row * w_val_adj

        # Acceleration bonus (small tilt, not primary)
        if accel is not None and dt in accel.index:
            accel_row = accel.loc[dt]
            score += accel_row * weights.get("acceleration_bonus", 0.05)

        # Regime-conditional defensive bonus
        if regime == RISK_OFF:
            for def_tick in defensive_tickers:
                if def_tick in score.index:
                    score[def_tick] += defensive_bonus

        # Cross-sectional z-score of the composite
        valid = score.dropna()
        if len(valid) >= 2:
            mu = valid.mean()
            sigma = valid.std()
            if sigma > 0:
                score = (score - mu) / sigma

        composite.loc[dt] = score.values

    composite.columns = tickers
    composite = composite.astype(float)

    # -------------------------------------------------------------------------
    # Build components dict for attribution
    # -------------------------------------------------------------------------
    components = {
        "cs_mom": cs_mom,
        "ts_mult": ts_mult,
        "value": value_aligned,
        "accel": accel,
        "regime_daily": regime_daily,
    }

    logger.info(
        f"Composite signals computed: {composite.dropna(how='all').shape[0]} months, "
        f"{composite.shape[1]} tickers"
    )

    return composite, regime_monthly, components


def _validate_weights(weights: Dict[str, float]) -> None:
    """Check that signal weights are non-negative and sum to approximately 1."""
    total = sum(weights.values())
    if abs(total - 1.0) > 0.01:
        logger.warning(
            f"Signal weights sum to {total:.3f} (expected 1.0). "
            "Check config.yaml signals.weights."
        )
    for k, v in weights.items():
        if v < 0:
            raise ValueError(f"Signal weight '{k}' is negative ({v}). Weights must be >= 0.")


def _align_to_monthly_index(df: pd.DataFrame, target_index: pd.DatetimeIndex) -> pd.DataFrame:
    """
    Align a monthly DataFrame to the target month-end index.
    Forward-fills for any gaps.
    """
    return df.reindex(target_index, method="ffill").fillna(0.0)


# ---------------------------------------------------------------------------
# Convenience: get current signals (for live trading / dashboard)
# ---------------------------------------------------------------------------

def get_current_signals(
    config: Optional[dict] = None,
    config_path=None,
    force_refresh: bool = False,
) -> Dict:
    """
    Load data and compute current (latest month-end) composite signals.

    Returns a dict with:
        "date"      : latest signal date
        "composite" : dict {ticker: z_score}
        "regime"    : current regime state
        "components": per-component signal values for the latest date
    """
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))

    from sector_rotation.data.loader import load_all, load_config
    from sector_rotation.data.universe import get_tickers

    cfg = config or load_config(config_path)
    prices, macro = load_all(config=cfg, force_refresh=force_refresh)

    etf_tickers = get_tickers(include_benchmark=False)
    etf_prices = prices[[t for t in etf_tickers if t in prices.columns]]

    composite, regime_monthly, components = compute_composite_signals(
        etf_prices,
        macro,
        weights=cfg.get("signals", {}).get("weights"),
        regime_method=cfg.get("signals", {}).get("regime", {}).get("method", "rules"),
    )

    # Get latest valid date
    valid_rows = composite.dropna(how="all")
    if valid_rows.empty:
        return {"error": "No valid composite signals computed"}

    latest_dt = valid_rows.index[-1]
    latest_composite = composite.loc[latest_dt].to_dict()
    latest_regime = regime_monthly.loc[latest_dt] if latest_dt in regime_monthly.index else RISK_ON

    # Latest component values
    comp_latest = {}
    for k, v in components.items():
        if v is None or k == "regime_daily":
            continue
        if isinstance(v, pd.DataFrame) and latest_dt in v.index:
            comp_latest[k] = v.loc[latest_dt].to_dict()

    return {
        "date": str(latest_dt.date()),
        "composite": latest_composite,
        "regime": latest_regime,
        "components": comp_latest,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent.parent))

    from sector_rotation.data.loader import load_all
    from sector_rotation.data.universe import get_tickers

    prices, macro = load_all()
    etf_tickers = get_tickers(include_benchmark=False)
    etf_prices = prices[[t for t in etf_tickers if t in prices.columns]]

    composite, regime_monthly, components = compute_composite_signals(etf_prices, macro)

    print("\n=== Composite Signals (last 6 months) ===")
    print(composite.tail(6).round(3).to_string())

    print("\n=== Regime (last 12 months) ===")
    print(regime_monthly.tail(12))

    print("\n=== Latest Ranking ===")
    latest = composite.iloc[-1].sort_values(ascending=False)
    print(latest.round(3).to_string())
