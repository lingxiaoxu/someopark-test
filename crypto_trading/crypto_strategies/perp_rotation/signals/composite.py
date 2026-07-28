"""Composite Signal — crypto perp rotation (Plan 05 §6).

COPIED from qlib-main/sector_rotation/signals/composite.py (read-only
template), V1 scoring path (the plan §9 weight set IS the V1 shape). The
architecture is preserved exactly:
  1. individual signals → 2. TS crash-filter multiplier → 3. regime-conditional
  weight multipliers (renormalized) → 4. defensive bonus in RISK_OFF →
  5. final cross-sectional z-score.

Adaptations (plan §5/§6 "Change" column only):
  * relative_value → carry (funding percentile, signals/carry.py).
  * NEW low_volatility bonus factor (plan §6 table) via risk_overlay.
  * Regime: crypto_common.regime.compute_regime over the crypto input frame
    (btc_rvol / funding / basis_dispersion / btc_dominance) — same 4-state API;
    ``regime_to_monthly`` → daily regimes used directly (daily rebalance clock).
  * DEFENSIVE_TICKERS → ["KXBTCPERP"] (flight-to-quality within crypto).
  * Equity v2 path + earnings/new_signals plumbing not copied (no earnings on
    perps); the v2 regime-weight-matrix idea survives as the multiplier dicts.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

from crypto_trading.crypto_common.regime import (RISK_OFF, RISK_ON, TRANSITION_DOWN,
                                                 TRANSITION_UP, compute_regime)
from crypto_trading.crypto_strategies.perp_rotation.signals.carry import compute_carry_signal
from crypto_trading.crypto_strategies.perp_rotation.signals.momentum import compute_all_momentum
from crypto_trading.crypto_strategies.perp_rotation.signals.risk_overlay import compute_low_vol_signal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Signal weights (plan §9) — template DEFAULT_WEIGHTS shape
# ---------------------------------------------------------------------------

DEFAULT_WEIGHTS = {
    "cross_sectional_momentum": 0.40,
    "ts_momentum": 0.15,
    "carry": 0.20,
    "regime_adjustment": 0.25,
}

# Regime-conditional weight multipliers — template structure; crypto tilts per
# plan §6 (risk-off → down-weight momentum, up-weight carry+low-vol).
# Starting points; calibrate on recorded data.
DEFAULT_REGIME_WEIGHT_MULTIPLIERS = {
    RISK_ON: {
        "cross_sectional_momentum": 1.0,
        "ts_momentum": 1.0,
        "carry": 1.0,
    },
    TRANSITION_UP: {
        "cross_sectional_momentum": 1.1,
        "ts_momentum": 1.0,
        "carry": 0.9,
    },
    TRANSITION_DOWN: {
        "cross_sectional_momentum": 0.7,
        "ts_momentum": 0.9,
        "carry": 1.2,
    },
    RISK_OFF: {
        "cross_sectional_momentum": 0.6,
        "ts_momentum": 0.8,
        "carry": 1.3,
    },
}

DEFENSIVE_TICKERS = ["KXBTCPERP"]        # flight-to-quality within crypto (plan §9)
DEFENSIVE_BONUS_RISK_OFF = 0.30          # z-score bonus in RISK_OFF (template value)
LOW_VOL_BONUS_DEFENSIVE = 0.20           # low-vol factor weight in defensive states


# ---------------------------------------------------------------------------
# Core composite function — template flow preserved
# ---------------------------------------------------------------------------

def compute_composite_signals(
    prices: pd.DataFrame,
    funding_panel: pd.DataFrame,
    regime_inputs: pd.DataFrame,
    weights: Optional[Dict[str, float]] = None,
    regime_multipliers: Optional[Dict[str, Dict[str, float]]] = None,
    defensive_tickers: Optional[list] = None,
    defensive_bonus: float = DEFENSIVE_BONUS_RISK_OFF,
    regime_method: str = "rules",
    regime_kwargs: Optional[dict] = None,
    signal_kwargs: Optional[dict] = None,
) -> Tuple[pd.DataFrame, pd.Series, Dict[str, pd.DataFrame]]:
    """Regime-conditioned composite z-scores for all perps (DAILY index).

    Returns (composite, regime_daily, components) — template contract with
    the monthly index replaced by the daily 24/7 index.
    """
    weights = weights or DEFAULT_WEIGHTS
    _validate_weights(weights)
    regime_multipliers = regime_multipliers or DEFAULT_REGIME_WEIGHT_MULTIPLIERS
    defensive_tickers = defensive_tickers or DEFENSIVE_TICKERS
    signal_kwargs = signal_kwargs or {}
    regime_kwargs = regime_kwargs or {}

    tickers = list(prices.columns)

    # Step 1: individual signals (template order)
    logger.info("Computing momentum signals...")
    mom_signals = compute_all_momentum(
        prices,
        cs_lookback=signal_kwargs.get("cs_lookback", 30),
        cs_skip=signal_kwargs.get("cs_skip", 1),
        cs_zscore_window=signal_kwargs.get("cs_zscore_window", 90),
        ts_lookback=signal_kwargs.get("ts_lookback", 30),
        ts_skip=signal_kwargs.get("ts_skip", 1),
        ts_crash_mult=signal_kwargs.get("ts_crash_mult", 0.0),
        accel_enabled=signal_kwargs.get("accel_enabled", True),
        accel_short=signal_kwargs.get("accel_short", 7),
        accel_long=signal_kwargs.get("accel_long", 30),
    )
    cs_mom = mom_signals["cs_mom"]
    ts_mult = mom_signals["ts_mult"]
    accel = mom_signals["accel"]

    logger.info("Computing carry signal...")
    carry_sig = compute_carry_signal(
        funding_panel.reindex(columns=tickers),
        lookback_days=signal_kwargs.get("carry_lookback_days", 90),
        missing_data_weight=signal_kwargs.get("carry_missing_weight", 0.0),
        favor=signal_kwargs.get("carry_favor", "long_receives"),
        mode=signal_kwargs.get("carry_mode", "percentile"),
        level_smooth_days=signal_kwargs.get("carry_level_smooth_days", 7),
    )
    carry_aligned = carry_sig.reindex(cs_mom.index, method="ffill").fillna(0.0)

    logger.info("Computing low-vol signal...")
    low_vol = compute_low_vol_signal(prices, window=signal_kwargs.get("low_vol_window", 30))

    logger.info("Computing regime...")
    regime_daily = compute_regime(regime_inputs, method=regime_method, **regime_kwargs)
    regime_aligned = regime_daily.reindex(cs_mom.index, method="ffill").fillna(RISK_ON)

    # Step 2: build composite day by day (template loop preserved)
    composite = pd.DataFrame(index=cs_mom.index, columns=tickers, dtype=float)

    w_cs = weights.get("cross_sectional_momentum", 0.40)
    w_ts = weights.get("ts_momentum", 0.15)
    w_carry = weights.get("carry", 0.20)
    # regime_adjustment weight multiplies the others (template semantics)

    for dt in cs_mom.index:
        regime = regime_aligned.get(dt, RISK_ON)
        rm = regime_multipliers.get(regime, regime_multipliers[RISK_ON])

        w_cs_adj = w_cs * rm.get("cross_sectional_momentum", 1.0)
        w_ts_adj = w_ts * rm.get("ts_momentum", 1.0)
        w_carry_adj = w_carry * rm.get("carry", 1.0)

        total_raw = w_cs + w_ts + w_carry
        total_adj = w_cs_adj + w_ts_adj + w_carry_adj
        if total_adj > 0:
            scale = total_raw / total_adj
            w_cs_adj *= scale
            w_ts_adj *= scale
            w_carry_adj *= scale

        cs_row = cs_mom.loc[dt] if dt in cs_mom.index else pd.Series(np.nan, index=tickers)
        ts_row = ts_mult.loc[dt] if dt in ts_mult.index else pd.Series(1.0, index=tickers)
        carry_row = carry_aligned.loc[dt] if dt in carry_aligned.index else pd.Series(0.0, index=tickers)

        cs_filtered = cs_row * ts_row

        score = pd.Series(0.0, index=tickers)
        score += cs_filtered * w_cs_adj
        score += (ts_row - 0.5) * 2 * w_ts_adj      # map {0,1} → {−1,+1} (template)
        score += carry_row * w_carry_adj

        if accel is not None and dt in accel.index:
            score += accel.loc[dt] * weights.get("acceleration_bonus", 0.05)

        # Low-vol bonus in defensive states (plan §6 "reward lower realized-vol
        # perps in defensive regimes")
        if regime in (RISK_OFF, TRANSITION_DOWN) and dt in low_vol.index:
            lv_row = low_vol.loc[dt].reindex(tickers).fillna(0.0)
            score += lv_row * weights.get("low_vol_bonus", LOW_VOL_BONUS_DEFENSIVE)

        # Regime-conditional defensive bonus (template)
        if regime == RISK_OFF:
            for def_tick in defensive_tickers:
                if def_tick in score.index:
                    score[def_tick] += defensive_bonus

        valid = score.dropna()
        if len(valid) >= 2:
            mu, sigma = valid.mean(), valid.std()
            if sigma > 0:
                score = (score - mu) / sigma

        composite.loc[dt] = score.values

    composite.columns = tickers
    composite = composite.astype(float)

    components = {
        "cs_mom": cs_mom,
        "ts_mult": ts_mult,
        "carry": carry_aligned,
        "accel": accel,
        "low_vol": low_vol,
        "regime_daily": regime_daily,
    }

    logger.info(
        f"Composite signals computed: {composite.dropna(how='all').shape[0]} days, "
        f"{composite.shape[1]} tickers"
    )
    return composite, regime_aligned, components


_BONUS_WEIGHT_KEYS = {"acceleration_bonus", "low_vol_bonus"}


def _validate_weights(weights: Dict[str, float]) -> None:
    """Template check: core weights ≈ 1, all non-negative."""
    core_total = sum(v for k, v in weights.items() if k not in _BONUS_WEIGHT_KEYS)
    if abs(core_total - 1.0) > 0.01:
        logger.warning(
            f"Core signal weights sum to {core_total:.3f} (expected 1.0). "
            "Check config.yaml signals.weights."
        )
    for k, v in weights.items():
        if v < 0:
            raise ValueError(f"Signal weight '{k}' is negative ({v}). Weights must be >= 0.")
