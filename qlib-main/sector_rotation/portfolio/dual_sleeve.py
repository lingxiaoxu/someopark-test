"""
dual_sleeve.py — Core + Tactical Dual Sleeve Portfolio Construction
====================================================================
Splits the portfolio into two independent sleeves with different
rebalance frequencies and signal sources:

  Core Sleeve (70%, monthly):
    - CS_MOM_12_1 (30%) + STM_6m (25%) + RV (20%) + ERM (10%) + LowVol (15%)
    - Top 3-4 sectors
    - Low turnover, stable allocation
    - Captures long-term factor premia

  Tactical Sleeve (30%, weekly):
    - MOM_3m (30%) + RSB_63d (35%) + Acceleration (20%) + STM_6m (15%)
    - Top 1-2 sectors
    - High turnover, fast reaction
    - Catches sector breakouts (XLK/AI rallies)

Final weights = 0.70 * core_weights + 0.30 * tactical_weights

Usage:
    from sector_rotation.portfolio.dual_sleeve import compute_dual_sleeve_weights
    weights = compute_dual_sleeve_weights(
        signals=components,
        regime=regime,
        prices=etf_prices,
        benchmark=spy_prices,
        rebalance_date=dt,
        is_weekly_rebal=True,
    )
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
#  Sleeve weight configurations
# ═══════════════════════════════════════════════════════════════════════════

CORE_WEIGHTS = {
    "cross_sectional_momentum": 0.30,
    "short_term_momentum":      0.25,
    "relative_value":           0.20,
    "earnings_revision":        0.10,
    "low_volatility":           0.15,
}

TACTICAL_WEIGHTS = {
    "momentum_3m":              0.30,
    "relative_strength":        0.35,
    "acceleration":             0.20,
    "short_term_momentum":      0.15,
}

# Regime adjustments for core sleeve
CORE_REGIME_ADJ = {
    "risk_on": {"cross_sectional_momentum": 1.0, "short_term_momentum": 1.2, "relative_value": 0.8, "low_volatility": 0.5},
    "transition_up": {"cross_sectional_momentum": 0.9, "short_term_momentum": 1.3, "relative_value": 0.7, "low_volatility": 0.6},
    "transition_down": {"cross_sectional_momentum": 1.0, "short_term_momentum": 0.7, "relative_value": 1.3, "low_volatility": 1.5},
    "risk_off": {"cross_sectional_momentum": 0.8, "short_term_momentum": 0.5, "relative_value": 1.4, "low_volatility": 2.0},
}

# Regime adjustments for tactical sleeve
TACTICAL_REGIME_ADJ = {
    "risk_on": {"momentum_3m": 1.2, "relative_strength": 1.2, "acceleration": 1.0, "short_term_momentum": 1.0},
    "transition_up": {"momentum_3m": 1.3, "relative_strength": 1.3, "acceleration": 1.2, "short_term_momentum": 1.0},
    "transition_down": {"momentum_3m": 0.5, "relative_strength": 0.5, "acceleration": 0.5, "short_term_momentum": 0.8},
    "risk_off": {"momentum_3m": 0.3, "relative_strength": 0.3, "acceleration": 0.3, "short_term_momentum": 0.5},
}


def _score_sleeve(
    signals: Dict[str, Optional[pd.DataFrame]],
    weights: Dict[str, float],
    regime_adj: Dict[str, Dict[str, float]],
    regime: str,
    dt: pd.Timestamp,
    tickers: List[str],
    top_n: int,
) -> pd.Series:
    """Score and select top-N sectors for one sleeve."""
    # Get regime multipliers
    adj = regime_adj.get(regime, regime_adj.get("risk_on", {}))

    score = pd.Series(0.0, index=tickers)

    signal_map = {
        "cross_sectional_momentum": signals.get("cs_mom"),
        "short_term_momentum": signals.get("short_term_mom"),
        "momentum_3m": signals.get("momentum_3m"),
        "relative_strength": signals.get("rs_breakout"),
        "relative_value": signals.get("value"),
        "earnings_revision": signals.get("earnings_revision"),
        "low_volatility": signals.get("low_volatility"),
        "acceleration": signals.get("accel"),
    }

    for signal_name, weight in weights.items():
        sig_df = signal_map.get(signal_name)
        if sig_df is None or sig_df.empty:
            continue
        if dt not in sig_df.index:
            # Use nearest prior date
            prior = sig_df.index[sig_df.index <= dt]
            if prior.empty:
                continue
            dt_use = prior[-1]
        else:
            dt_use = dt

        row = sig_df.loc[dt_use].reindex(tickers, fill_value=0.0)
        regime_mult = adj.get(signal_name, 1.0)
        score += row * weight * regime_mult

    # Select top-N
    ranked = score.sort_values(ascending=False)
    selected = ranked.head(top_n)

    # Allocate proportional to score (only positive scores)
    selected = selected[selected > 0]
    if selected.empty:
        # Fallback: equal weight top-N
        selected = ranked.head(top_n)
        result = pd.Series(0.0, index=tickers)
        if not selected.empty:
            result[selected.index] = 1.0 / len(selected)
        return result

    # Score-proportional weights
    result = pd.Series(0.0, index=tickers)
    total_score = selected.sum()
    if total_score > 0:
        result[selected.index] = selected / total_score
    else:
        result[selected.index] = 1.0 / len(selected)

    return result


def compute_dual_sleeve_weights(
    signals: Dict[str, Optional[pd.DataFrame]],
    regime: str,
    dt: pd.Timestamp,
    tickers: List[str],
    core_pct: float = 0.70,
    tactical_pct: float = 0.30,
    core_top_n: int = 4,
    tactical_top_n: int = 2,
    is_monthly_rebal: bool = True,
    is_weekly_rebal: bool = False,
    prev_core_weights: Optional[pd.Series] = None,
    prev_tactical_weights: Optional[pd.Series] = None,
) -> pd.Series:
    """
    Compute combined dual-sleeve portfolio weights.

    Core sleeve (70%): rebalances monthly.
    Tactical sleeve (30%): rebalances weekly.

    Parameters
    ----------
    signals : dict of signal DataFrames (from composite components)
    regime : current regime label
    dt : rebalance date
    tickers : ETF tickers
    core_pct / tactical_pct : sleeve allocation (must sum to 1.0)
    core_top_n : number of sectors in core
    tactical_top_n : number of sectors in tactical
    is_monthly_rebal : True on monthly rebalance dates
    is_weekly_rebal : True on weekly rebalance dates
    prev_core_weights / prev_tactical_weights : carry-over if not rebalancing

    Returns
    -------
    pd.Series — combined weights (tickers → float, sum ≈ 1.0)
    """
    # Core sleeve: only rebalance monthly
    if is_monthly_rebal or prev_core_weights is None:
        core_w = _score_sleeve(
            signals, CORE_WEIGHTS, CORE_REGIME_ADJ, regime, dt, tickers, core_top_n
        )
    else:
        core_w = prev_core_weights

    # Tactical sleeve: rebalance weekly (or monthly)
    if is_weekly_rebal or is_monthly_rebal or prev_tactical_weights is None:
        tactical_w = _score_sleeve(
            signals, TACTICAL_WEIGHTS, TACTICAL_REGIME_ADJ, regime, dt, tickers, tactical_top_n
        )
    else:
        tactical_w = prev_tactical_weights

    # Combine
    combined = core_pct * core_w + tactical_pct * tactical_w

    # Normalize (should already be close to 1.0)
    total = combined.sum()
    if total > 0:
        combined = combined / total

    logger.debug(
        f"Dual sleeve at {dt.date() if hasattr(dt, 'date') else dt}: "
        f"core_top={core_w[core_w > 0.01].index.tolist()}, "
        f"tactical_top={tactical_w[tactical_w > 0.01].index.tolist()}, "
        f"regime={regime}"
    )

    return combined
