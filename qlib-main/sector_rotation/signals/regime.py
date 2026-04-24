"""
Regime Detection
================
Four-state macro regime detection based on VIX, yield curve, credit spreads, and ISM.

Regime States
-------------
- RISK_ON       : Expansion, low vol, tight spreads, upward-sloping yield curve.
                  Full risk exposure, momentum-friendly.
- RISK_OFF      : Recession/panic, high vol, wide spreads, ISM declining.
                  Defensive posture, momentum less reliable.
- TRANSITION_UP : Recovery from risk-off, conditions improving.
                  Increasing risk appetite.
- TRANSITION_DOWN: Late cycle or early stress, conditions deteriorating.
                  Reduce risk gradually.

Detection Method
----------------
Phase 1: Rules-based (default, interpretable, no training required).
    - Uses normalized indicators: VIX z-score, yield curve slope, HY spread z-score,
      ISM direction (above/below 50, rising/falling).
    - Regime determined by a scoring matrix at each observation date.

Phase 2: HMM (optional, requires hmmlearn ≥ 0.3).
    - 4-state Gaussian HMM trained on the same normalized macro features.
    - More statistically rigorous; requires parameter stability across regimes.
    - Activated by setting method="hmm" in config.

References
----------
Guidolin, M., & Timmermann, A. (2007). Asset allocation under multivariate regime
    switching. Journal of Economic Dynamics and Control, 31(11), 3503-3544.
Nystrup, P., et al. (2020). Dynamic portfolio optimization across hidden market regimes.
    Quantitative Finance, 20(6), 941-953.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regime state constants
# ---------------------------------------------------------------------------

RISK_ON = "risk_on"
RISK_OFF = "risk_off"
TRANSITION_UP = "transition_up"
TRANSITION_DOWN = "transition_down"

REGIME_STATES = [RISK_ON, TRANSITION_UP, TRANSITION_DOWN, RISK_OFF]

# Numeric encoding for HMM / plotting
REGIME_NUMERIC = {RISK_ON: 3, TRANSITION_UP: 2, TRANSITION_DOWN: 1, RISK_OFF: 0}
REGIME_FROM_NUMERIC = {v: k for k, v in REGIME_NUMERIC.items()}

# Colors for plotting
REGIME_COLORS = {
    RISK_ON: "#2ca02c",        # Green
    TRANSITION_UP: "#98df8a",  # Light green
    TRANSITION_DOWN: "#ffbb78", # Light orange
    RISK_OFF: "#d62728",       # Red
}


# ---------------------------------------------------------------------------
# Macro indicator normalization
# ---------------------------------------------------------------------------

def normalize_macro(
    macro: pd.DataFrame,
    rolling_window: int = 252,
    min_periods: int = 63,
) -> pd.DataFrame:
    """
    Normalize macro indicators to z-scores using rolling statistics.

    Parameters
    ----------
    macro : pd.DataFrame
        Raw macro indicators (daily). Expected columns: vix, yield_curve,
        hy_spread, ism_mfg, breakeven_10y, fed_rate.
    rolling_window : int
        Look-back window for rolling mean/std (default 252 = 1 year).
    min_periods : int
        Minimum periods for rolling stats.

    Returns
    -------
    pd.DataFrame
        Z-scored indicators. Same columns as input.
    """
    result = pd.DataFrame(index=macro.index)
    for col in macro.columns:
        s = macro[col].copy()
        roll_mean = s.rolling(rolling_window, min_periods=min_periods).mean()
        roll_std = s.rolling(rolling_window, min_periods=min_periods).std()
        result[col] = (s - roll_mean) / roll_std.replace(0, np.nan)
    return result


# ---------------------------------------------------------------------------
# Rules-Based Regime Detection
# ---------------------------------------------------------------------------

def _classify_regime_row(
    vix: float,
    yield_curve: float,
    hy_spread: float,
    ism_mfg: Optional[float],
    vix_raw: float,
    hy_spread_raw: float,
    # Optional additional indicators (from FRED — use if available)
    fin_stress: float = np.nan,    # St. Louis FSI z-score (STLFSI4)
    ig_spread: float = np.nan,     # IG OAS z-score (BAMLC0A0CM)
    nfci: float = np.nan,          # National Financial Conditions z-score (NFCI)
    # Thresholds (raw values)
    vix_high: float = 25.0,
    vix_extreme: float = 35.0,
    hy_spread_high_bps: float = 450.0,
    yield_curve_inversion: float = -0.10,
    ism_expansion: float = 50.0,
) -> str:
    """
    Classify a single observation into a regime state.

    Uses raw VIX and HY spread for absolute thresholds, and z-scores for
    relative context (identifying transitions vs persistent regime changes).
    Optionally uses fin_stress (STLFSI4), ig_spread, and nfci when available.

    Returns one of: RISK_ON, RISK_OFF, TRANSITION_UP, TRANSITION_DOWN
    """
    if np.isnan(vix_raw) or np.isnan(hy_spread_raw):
        return RISK_ON  # Default when data unavailable

    # Hard stops → RISK_OFF
    if vix_raw > vix_extreme or hy_spread_raw > hy_spread_high_bps * 1.3:
        return RISK_OFF

    # Score-based classification
    # Start with 0, add/subtract points, threshold at end
    score = 0  # Higher = more risk-on

    # VIX contribution (raw threshold + z-score for direction)
    if vix_raw < 15:
        score += 2   # Very calm
    elif vix_raw < 20:
        score += 1   # Normal
    elif vix_raw < vix_high:
        score += 0   # Elevated but not alarming
    elif vix_raw < vix_extreme:
        score -= 1   # High stress
    else:
        score -= 2   # Crisis

    # VIX direction (z-score rising = getting worse)
    if not np.isnan(vix):
        if vix > 1.5:
            score -= 1   # VIX spiking (z > +1.5σ)
        elif vix < -1.0:
            score += 1   # VIX falling (z < -1σ)

    # Yield curve contribution
    if not np.isnan(yield_curve):
        if yield_curve > 0.5:  # Healthy slope (50+ bps)
            score += 1
        elif yield_curve < yield_curve_inversion:
            score -= 1

    # HY spread contribution
    if not np.isnan(hy_spread_raw):
        if hy_spread_raw < 300:        # < 300 bps = tight = risk-on
            score += 1
        elif hy_spread_raw > hy_spread_high_bps:
            score -= 1

    # HY spread direction (z-score)
    if not np.isnan(hy_spread):
        if hy_spread > 1.5:
            score -= 1   # Credit stress rising
        elif hy_spread < -1.0:
            score += 1   # Credit conditions easing

    # ISM contribution
    if ism_mfg is not None and not np.isnan(ism_mfg):
        if ism_mfg > ism_expansion + 3:   # > 53: expansion with momentum
            score += 1
        elif ism_mfg < ism_expansion - 5:  # < 45: contraction
            score -= 1

    # Financial Stress Index (STLFSI4): z-score > +1 = stress, < -1 = calm
    if not np.isnan(fin_stress):
        if fin_stress > 1.5:
            score -= 1
        elif fin_stress < -1.0:
            score += 1

    # National Financial Conditions (NFCI): positive = tighter = worse
    if not np.isnan(nfci):
        if nfci > 1.0:
            score -= 1
        elif nfci < -1.0:
            score += 1

    # IG spread direction (corroborates HY)
    if not np.isnan(ig_spread):
        if ig_spread > 1.5:
            score -= 1
        elif ig_spread < -1.0:
            score += 1

    # Map score to regime
    if score >= 3:
        return RISK_ON
    elif score >= 1:
        return TRANSITION_UP
    elif score >= -1:
        return TRANSITION_DOWN
    else:
        return RISK_OFF


def compute_regime_rules(
    macro: pd.DataFrame,
    vix_high_threshold: float = 25.0,
    vix_extreme_threshold: float = 35.0,
    hy_spread_high_bps: float = 450.0,
    yield_curve_inversion: float = -0.10,
    ism_expansion: float = 50.0,
    smoothing_days: int = 5,
) -> pd.Series:
    """
    Compute regime classifications using rule-based scoring.

    Parameters
    ----------
    macro : pd.DataFrame
        Daily macro indicators. Must have columns: vix, yield_curve, hy_spread.
        Optional: ism_mfg. All expected to be in raw (un-normalized) units.
        HY spread in bps (already multiplied by 100 by loader.py).
    vix_high_threshold : float
        VIX level marking elevated stress.
    vix_extreme_threshold : float
        VIX level marking crisis / emergency de-risk.
    hy_spread_high_bps : float
        HY OAS threshold in bps.
    yield_curve_inversion : float
        10Y-2Y threshold for inversion (default -0.10%).
    ism_expansion : float
        ISM threshold separating expansion from contraction (50).
    smoothing_days : int
        Rolling mode smoothing to reduce regime chatter (days).

    Returns
    -------
    pd.Series
        Daily regime labels (string). DatetimeIndex.
    """
    # Compute z-scores for directional signals
    macro_z = normalize_macro(macro)

    regimes = []
    for i, dt in enumerate(macro.index):
        row = macro.iloc[i]
        row_z = macro_z.iloc[i]

        regime = _classify_regime_row(
            vix=row_z.get("vix", np.nan),
            yield_curve=row_z.get("yield_curve", np.nan),
            hy_spread=row_z.get("hy_spread", np.nan),
            ism_mfg=row.get("ism_mfg", np.nan),
            vix_raw=row.get("vix", np.nan),
            hy_spread_raw=row.get("hy_spread", np.nan),
            fin_stress=row_z.get("fin_stress", np.nan),
            ig_spread=row_z.get("ig_spread", np.nan),
            nfci=row_z.get("nfci", np.nan),
            vix_high=vix_high_threshold,
            vix_extreme=vix_extreme_threshold,
            hy_spread_high_bps=hy_spread_high_bps,
            yield_curve_inversion=yield_curve_inversion,
            ism_expansion=ism_expansion,
        )
        regimes.append(regime)

    regime_series = pd.Series(regimes, index=macro.index, name="regime")

    # Smooth: rolling mode over smoothing_days to reduce whipsawing
    if smoothing_days > 1:
        numeric = regime_series.map(REGIME_NUMERIC)
        smoothed_numeric = (
            numeric.rolling(window=smoothing_days, min_periods=1)
            .apply(lambda x: pd.Series(x).mode().iloc[0], raw=False)
            .round()
            .astype(int)
        )
        regime_series = smoothed_numeric.map(REGIME_FROM_NUMERIC)
        regime_series.name = "regime"

    # Fill any NaN with RISK_ON (default)
    regime_series = regime_series.fillna(RISK_ON)

    return regime_series


# ---------------------------------------------------------------------------
# HMM-Based Regime Detection
# ---------------------------------------------------------------------------

def compute_regime_hmm(
    macro: pd.DataFrame,
    n_states: int = 4,
    n_iter: int = 200,
    covariance_type: str = "full",
    random_state: int = 42,
) -> pd.Series:
    """
    Compute regime states using a Gaussian Hidden Markov Model.

    Requires: hmmlearn >= 0.3

    The HMM is trained on normalized macro features. States are mapped to
    regime labels by sorting on VIX level (state with lowest avg VIX = RISK_ON).

    Parameters
    ----------
    macro : pd.DataFrame
        Daily macro indicators (raw values).
    n_states : int
        Number of hidden states (4 for our 4-regime model).
    n_iter : int
        HMM EM iterations.
    covariance_type : str
        HMM covariance type ('full', 'diag', 'tied').
    random_state : int
        Random seed for reproducibility.

    Returns
    -------
    pd.Series
        Daily regime labels.
    """
    try:
        from hmmlearn import hmm
    except ImportError:
        raise ImportError(
            "hmmlearn is required for HMM regime detection. "
            "Install: conda run -n qlib_run pip install hmmlearn>=0.3"
        )

    features = ["vix", "yield_curve", "hy_spread"]
    available = [f for f in features if f in macro.columns]
    if not available:
        raise ValueError(f"No macro features available. Expected: {features}")

    macro_z = normalize_macro(macro[available])
    X = macro_z.ffill().bfill().values

    # Drop rows with any NaN
    valid_mask = ~np.isnan(X).any(axis=1)
    X_valid = X[valid_mask]

    model = hmm.GaussianHMM(
        n_components=n_states,
        covariance_type=covariance_type,
        n_iter=n_iter,
        random_state=random_state,
    )
    model.fit(X_valid)

    hidden_states = np.full(len(X), -1, dtype=int)
    hidden_states[valid_mask] = model.predict(X_valid)

    # Map HMM states to regime labels:
    # Sort states by mean VIX feature (index 0 = vix): lower VIX → more risk-on
    vix_idx = available.index("vix") if "vix" in available else 0
    state_vix_means = [model.means_[s][vix_idx] for s in range(n_states)]
    state_order = sorted(range(n_states), key=lambda s: state_vix_means[s])
    # state_order[0] = lowest VIX = RISK_ON, ..., [3] = highest VIX = RISK_OFF
    state_labels = {
        state_order[0]: RISK_ON,
        state_order[1]: TRANSITION_UP,
        state_order[2]: TRANSITION_DOWN,
        state_order[3]: RISK_OFF,
    }

    regime_series = pd.Series(
        [state_labels.get(s, RISK_ON) for s in hidden_states],
        index=macro.index,
        name="regime",
    ).fillna(RISK_ON)

    logger.info(
        f"HMM regime model: {n_states} states, "
        f"VIX means by state: {[f'{model.means_[s][vix_idx]:.2f}' for s in range(n_states)]}"
    )
    return regime_series


# ---------------------------------------------------------------------------
# Monthly regime resampling (for backtest use)
# ---------------------------------------------------------------------------

def regime_to_monthly(regime_daily: pd.Series) -> pd.Series:
    """
    Downsample daily regime to monthly (end-of-month) for monthly backtest.
    Uses the most frequent regime in each month.
    """
    monthly = regime_daily.resample("ME").apply(
        lambda x: x.mode().iloc[0] if len(x) > 0 else RISK_ON
    )
    monthly.name = "regime"
    return monthly


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def compute_regime(
    macro: pd.DataFrame,
    method: str = "rules",
    **kwargs,
) -> pd.Series:
    """
    Unified regime detection entry point.

    Parameters
    ----------
    macro : pd.DataFrame
        Daily macro indicators (output of loader.load_macro_data).
    method : str
        "rules" or "hmm".
    **kwargs :
        Passed to the underlying computation function.

    Returns
    -------
    pd.Series
        Daily regime labels.
    """
    if method == "rules":
        return compute_regime_rules(macro, **kwargs)
    elif method == "hmm":
        return compute_regime_hmm(macro, **kwargs)
    else:
        raise ValueError(f"Unknown regime method: {method}. Use 'rules' or 'hmm'.")


# ---------------------------------------------------------------------------
# Regime statistics summary
# ---------------------------------------------------------------------------

def regime_summary(regime: pd.Series) -> pd.DataFrame:
    """
    Compute frequency, duration, and transition statistics for regime series.

    Returns pd.DataFrame with columns:
        count, frequency_pct, avg_duration_days, max_duration_days, transitions
    """
    rows = []
    for state in REGIME_STATES:
        mask = (regime == state)
        count = mask.sum()
        freq = count / len(regime) * 100

        # Compute run lengths
        runs = []
        current_run = 0
        for val in mask:
            if val:
                current_run += 1
            else:
                if current_run > 0:
                    runs.append(current_run)
                    current_run = 0
        if current_run > 0:
            runs.append(current_run)

        avg_dur = np.mean(runs) if runs else 0
        max_dur = np.max(runs) if runs else 0
        n_episodes = len(runs)

        rows.append({
            "regime": state,
            "count_days": count,
            "frequency_pct": round(freq, 1),
            "n_episodes": n_episodes,
            "avg_duration_days": round(avg_dur, 1),
            "max_duration_days": max_dur,
        })

    return pd.DataFrame(rows).set_index("regime")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent.parent))
    from sector_rotation.data.loader import load_all

    _, macro = load_all()
    regime = compute_regime(macro, method="rules")

    print("\n=== Regime Summary ===")
    print(regime_summary(regime))

    print("\n=== Recent Regime Labels ===")
    print(regime.tail(30).value_counts())

    monthly = regime_to_monthly(regime)
    print("\n=== Monthly Regime (last 12) ===")
    print(monthly.tail(12))
