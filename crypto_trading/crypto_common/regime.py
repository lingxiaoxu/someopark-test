"""
Regime Detection (crypto)
=========================
COPIED from qlib-main/sector_rotation/signals/regime.py (read-only template,
Plan 00 §5) and adapted for crypto-native inputs. The 4-state API — state
names, constants, numeric encoding, colors, function inventory and signatures'
shape — is IDENTICAL to the template so strategy code ports 1:1.

Input swap (Plan 00 §5):
    VIX               → BTC realized vol   (column ``btc_rvol``, annualized %)
    ISM / yield curve → funding level/sign (column ``funding``, per-8h-cycle rate)
    HY spread         → mark-vs-index basis dispersion (column ``basis_dispersion``, bps)
    breadth           → BTC dominance      (column ``btc_dominance``, %, direction used)
    (optional, None-safe, mirroring the template's ig_spread/fin_stress/nfci slots:
     ``offshore_funding`` z, ``stress`` level, ``flow`` level)

This module is pure computation: it accepts pandas Series/DataFrames and never
fetches data (loader wires the inputs later). ``realized_vol`` is an ADDED
helper (the template's VIX came pre-made from the loader; crypto rvol must be
derived from a price series).

Regime States
-------------
- RISK_ON       : Calm vol, tight basis, healthy funding — full risk, momentum-friendly.
- RISK_OFF      : Vol spike / basis blowout / funding panic — defensive posture.
- TRANSITION_UP : Recovery from risk-off, conditions improving.
- TRANSITION_DOWN: Conditions deteriorating — reduce risk gradually.

Detection Method
----------------
Phase 1: Rules-based (default, interpretable, no training required).
Phase 2: HMM (optional, requires hmmlearn ≥ 0.3 — NOT installed; guarded).

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
# Regime state constants (IDENTICAL to template)
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
# Crypto default thresholds — calibrate on recorded data (Plan 00 §5).
# Structure mirrors the template's VIX/HY/curve/ISM raw thresholds; the
# starting values below are crypto-plausible priors, NOT calibrated results.
# ---------------------------------------------------------------------------

RVOL_HIGH_DEFAULT = 60.0          # ann. % — "elevated stress"      (VIX 25 analog)
RVOL_EXTREME_DEFAULT = 90.0       # ann. % — "crisis, hard stop"    (VIX 35 analog)
BASIS_DISP_HIGH_BPS_DEFAULT = 50.0   # bps — venue dislocated       (HY 450bps analog)
FUNDING_FROTH_DEFAULT = 5e-4      # per 8h cycle (~+73%/yr) — overheated longs
FUNDING_PANIC_DEFAULT = -2.5e-4   # per 8h cycle — crowded shorts / stress
DOM_FLIGHT_PP_DEFAULT = 2.0       # 30d BTC-dominance rise (pp) = flight-to-quality
DOM_BREADTH_PP_DEFAULT = -1.0     # 30d BTC-dominance fall (pp) = alt breadth (risk-on)


# ---------------------------------------------------------------------------
# Derived-input helper (ADDED — template's VIX arrived pre-made from loader)
# ---------------------------------------------------------------------------

def realized_vol(prices: pd.Series, window: int = 30,
                 periods_per_year: int = 365) -> pd.Series:
    """Rolling annualized realized volatility in PERCENT from a price series.

    24/7 daily bars → 365 periods/year (Plan 00 §5). ``btc_rvol`` input feed.
    """
    rets = prices.pct_change()
    return rets.rolling(window, min_periods=max(2, window // 3)).std() \
               * np.sqrt(periods_per_year) * 100.0


# ---------------------------------------------------------------------------
# Macro indicator normalization (IDENTICAL logic; window 252→365, min 63→90)
# ---------------------------------------------------------------------------

def normalize_macro(
    macro: pd.DataFrame,
    rolling_window: int = 365,
    min_periods: int = 90,
) -> pd.DataFrame:
    """
    Normalize indicators to z-scores using rolling statistics.

    Parameters
    ----------
    macro : pd.DataFrame
        Raw indicators (daily, 24/7). Expected columns: btc_rvol, funding,
        basis_dispersion, btc_dominance (optional extras pass through).
    rolling_window : int
        Look-back window for rolling mean/std (default 365 = 1 year of 24/7 days).
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
# Rules-Based Regime Detection (scoring skeleton IDENTICAL to template)
# ---------------------------------------------------------------------------

def _classify_regime_row(
    rvol: float,
    funding_raw: float,
    basis_disp: float,
    dom_chg: Optional[float],
    rvol_raw: float,
    basis_disp_raw: float,
    # Optional additional indicators — all None-safe (template convention:
    # never np.isnan() on these; use `is not None`)
    offshore_funding_z: Optional[float] = None,  # cross-venue funding stress (IG-spread slot)
    stress_level: Optional[float] = None,        # centred crypto stress index, 0=avg (STLFSI slot)
    flow_level: Optional[float] = None,          # centred flow/conditions index, 0=neutral (NFCI slot)
    # Thresholds (raw values) — calibrate on recorded data (Plan 00 §5)
    rvol_high: float = RVOL_HIGH_DEFAULT,
    rvol_extreme: float = RVOL_EXTREME_DEFAULT,
    basis_disp_high_bps: float = BASIS_DISP_HIGH_BPS_DEFAULT,
    funding_froth: float = FUNDING_FROTH_DEFAULT,
    funding_panic: float = FUNDING_PANIC_DEFAULT,
    dom_flight_pp: float = DOM_FLIGHT_PP_DEFAULT,
    dom_breadth_pp: float = DOM_BREADTH_PP_DEFAULT,
) -> str:
    """
    Classify a single observation into a regime state.

    Uses raw realized vol and basis dispersion for absolute thresholds, and
    z-scores for relative context (identifying transitions vs persistent
    regime changes) — the exact scoring skeleton of the template with the
    crypto input swap:

        rvol_raw brackets   ← vix_raw brackets
        rvol z direction    ← vix z direction
        funding_raw band    ← yield-curve slope
        basis_disp_raw band ← HY spread level
        basis_disp z dir.   ← HY spread z direction
        dominance 30d chg   ← ISM expansion/contraction
        3 optional slots    ← ig_spread_z / fin_stress / nfci

    Returns one of: RISK_ON, RISK_OFF, TRANSITION_UP, TRANSITION_DOWN
    """
    if pd.isna(rvol_raw):
        return RISK_ON  # rvol is primary indicator; fall back only if unavailable

    # Hard stops → RISK_OFF
    # Realized vol alone is sufficient for crisis detection (basis may be NaN / delayed)
    if rvol_raw > rvol_extreme:
        return RISK_OFF
    if not pd.isna(basis_disp_raw) and basis_disp_raw > basis_disp_high_bps * 1.3:
        return RISK_OFF

    # Score-based classification
    # Start with 0, add/subtract points, threshold at end
    score = 0  # Higher = more risk-on

    # Realized-vol contribution (raw brackets mirror VIX 15/20/25/35)
    if rvol_raw < 30:
        score += 2   # Very calm
    elif rvol_raw < 45:
        score += 1   # Normal
    elif rvol_raw < rvol_high:
        score += 0   # Elevated but not alarming
    elif rvol_raw < rvol_extreme:
        score -= 1   # High stress
    else:
        score -= 2   # Crisis

    # Vol direction (z-score rising = getting worse)
    if not pd.isna(rvol):
        if rvol > 1.5:
            score -= 1   # vol spiking (z > +1.5σ)
        elif rvol < -1.0:
            score += 1   # vol falling (z < -1σ)

    # Funding contribution (yield-curve slot): mildly positive = healthy demand;
    # extreme either side = crowding/stress
    if not pd.isna(funding_raw):
        if 0.0 < funding_raw <= funding_froth:
            score += 1
        elif funding_raw > funding_froth or funding_raw < funding_panic:
            score -= 1

    # Basis-dispersion contribution (HY-spread slot)
    if not pd.isna(basis_disp_raw):
        if basis_disp_raw < 15:                    # tight = venue efficient = risk-on
            score += 1
        elif basis_disp_raw > basis_disp_high_bps:
            score -= 1

    # Basis-dispersion direction (z-score)
    if not pd.isna(basis_disp):
        if basis_disp > 1.5:
            score -= 1   # dislocation stress rising
        elif basis_disp < -1.0:
            score += 1   # dislocation easing

    # Dominance contribution (ISM slot): falling dominance = alt breadth =
    # expansion analog; sharply rising = flight-to-quality = contraction analog
    if dom_chg is not None and not pd.isna(dom_chg):
        if dom_chg < dom_breadth_pp:
            score += 1
        elif dom_chg > dom_flight_pp:
            score -= 1

    # Offshore-funding z-score (corroborates funding; template ig_spread slot)
    if offshore_funding_z is not None:
        if offshore_funding_z > 1.5:
            score -= 1   # cross-venue crowding rising
        elif offshore_funding_z < -1.0:
            score += 1   # easing

    # Centred crypto stress index — raw level (0=avg; template STLFSI thresholds)
    if stress_level is not None:
        if stress_level > 1.0:
            score -= 1
        elif stress_level < -0.5:
            score += 1

    # Centred flow/conditions index — raw level (0=neutral; template NFCI thresholds)
    if flow_level is not None:
        if flow_level > 0.5:
            score -= 1
        elif flow_level < -0.5:
            score += 1

    # Map score to regime (IDENTICAL to template)
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
    rvol_high_threshold: float = RVOL_HIGH_DEFAULT,
    rvol_extreme_threshold: float = RVOL_EXTREME_DEFAULT,
    basis_disp_high_bps: float = BASIS_DISP_HIGH_BPS_DEFAULT,
    funding_froth: float = FUNDING_FROTH_DEFAULT,
    funding_panic: float = FUNDING_PANIC_DEFAULT,
    dominance_change_days: int = 30,
    smoothing_days: int = 5,
) -> pd.Series:
    """
    Compute regime classifications using rule-based scoring.

    Parameters
    ----------
    macro : pd.DataFrame
        Daily (24/7) indicators. Must have columns: btc_rvol, funding,
        basis_dispersion. Optional: btc_dominance, offshore_funding, stress,
        flow. All in raw (un-normalized) units: rvol ann. %, funding per-cycle
        rate, dispersion bps, dominance %.
    rvol_high_threshold : float
        Realized-vol level marking elevated stress.
    rvol_extreme_threshold : float
        Realized-vol level marking crisis / emergency de-risk.
    basis_disp_high_bps : float
        Mark-vs-index basis dispersion threshold in bps.
    funding_froth / funding_panic : float
        Per-cycle funding band edges (healthy in (0, froth]; stress outside
        [panic, froth]).
    dominance_change_days : int
        Look-back for the BTC-dominance direction (pp change).
    smoothing_days : int
        Rolling mode smoothing to reduce regime chatter (days).

    Returns
    -------
    pd.Series
        Daily regime labels (string). DatetimeIndex.
    """
    # Compute z-scores for directional signals
    macro_z = normalize_macro(macro)

    # Dominance direction series (pp change over look-back) — ISM analog
    dom_chg_series = (
        macro["btc_dominance"].diff(dominance_change_days)
        if "btc_dominance" in macro.columns else None
    )

    regimes = []
    for i, dt in enumerate(macro.index):
        row = macro.iloc[i]
        row_z = macro_z.iloc[i]

        # Helper: convert pandas scalar to None if NaN/NA (template convention)
        def _val(series, key):
            v = series.get(key)
            return None if (v is None or pd.isna(v)) else float(v)

        regime = _classify_regime_row(
            rvol=row_z.get("btc_rvol", np.nan),
            funding_raw=row.get("funding", np.nan),
            basis_disp=row_z.get("basis_dispersion", np.nan),
            dom_chg=(None if dom_chg_series is None
                     else _val(dom_chg_series, dt)),
            rvol_raw=row.get("btc_rvol", np.nan),
            basis_disp_raw=row.get("basis_dispersion", np.nan),
            # offshore funding: use z-score (direction matters — template ig_spread convention)
            offshore_funding_z=_val(row_z, "offshore_funding"),
            # stress / flow: use raw level (already centred indices, not z-scored again)
            stress_level=_val(row, "stress"),
            flow_level=_val(row, "flow"),
            rvol_high=rvol_high_threshold,
            rvol_extreme=rvol_extreme_threshold,
            basis_disp_high_bps=basis_disp_high_bps,
            funding_froth=funding_froth,
            funding_panic=funding_panic,
        )
        regimes.append(regime)

    regime_series = pd.Series(regimes, index=macro.index, name="regime")

    # Smooth: rolling mode over smoothing_days to reduce whipsawing (IDENTICAL)
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
# HMM-Based Regime Detection (guarded exactly like the template; hmmlearn NOT
# installed in someopark_run — method="rules" is the default/live path)
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

    The HMM is trained on normalized features. States are mapped to regime
    labels by sorting on realized-vol level (lowest avg rvol = RISK_ON).

    Parameters
    ----------
    macro : pd.DataFrame
        Daily indicators (raw values).
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
            "Install: conda run -n someopark_run pip install hmmlearn>=0.3"
        )

    features = ["btc_rvol", "funding", "basis_dispersion"]
    available = [f for f in features if f in macro.columns]
    if not available:
        raise ValueError(f"No regime features available. Expected: {features}")

    macro_z = normalize_macro(macro[available])
    # PIT: ffill only — bfill would seed early rows with future values; leading
    # NaN rows are dropped by valid_mask below
    X = macro_z.ffill().values

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
    # Sort states by mean rvol feature: lower rvol → more risk-on
    rvol_idx = available.index("btc_rvol") if "btc_rvol" in available else 0
    state_rvol_means = [model.means_[s][rvol_idx] for s in range(n_states)]
    state_order = sorted(range(n_states), key=lambda s: state_rvol_means[s])
    # state_order[0] = lowest rvol = RISK_ON, ..., [3] = highest rvol = RISK_OFF
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
        f"rvol means by state: {[f'{model.means_[s][rvol_idx]:.2f}' for s in range(n_states)]}"
    )
    return regime_series


# ---------------------------------------------------------------------------
# Monthly regime resampling (kept for API parity; crypto sleeves may also
# consume the daily series directly — Plans 01/05)
# ---------------------------------------------------------------------------

def regime_to_monthly(regime_daily: pd.Series) -> pd.Series:
    """
    Downsample daily regime to monthly (end-of-month).
    Uses the most frequent regime in each month.
    """
    monthly = regime_daily.resample("ME").apply(
        lambda x: x.mode().iloc[0] if len(x) > 0 else RISK_ON
    )
    monthly.name = "regime"
    return monthly


# ---------------------------------------------------------------------------
# Main entry point (IDENTICAL surface)
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
        Daily indicators (loader provides: btc_rvol, funding, basis_dispersion,
        btc_dominance, plus optional offshore_funding/stress/flow).
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
# Regime statistics summary (IDENTICAL)
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

    # Synthetic smoke test (template's __main__ loaded the sector_rotation
    # loader — replaced: crypto loader lands later; regime stays pure)
    n = 400
    idx = pd.date_range("2026-06-03", periods=n, freq="D", tz="UTC")
    rng = np.random.default_rng(42)
    rvol = pd.Series(np.r_[np.full(200, 25.0), np.full(100, 55.0), np.full(100, 95.0)]
                     + rng.normal(0, 2, n), index=idx)
    macro = pd.DataFrame({
        "btc_rvol": rvol,
        "funding": np.r_[np.full(200, 1e-4), np.full(100, 6e-4), np.full(100, -4e-4)],
        "basis_dispersion": np.r_[np.full(200, 8.0), np.full(100, 30.0), np.full(100, 70.0)],
        "btc_dominance": np.r_[np.linspace(55, 52, 200), np.linspace(52, 58, 200)],
    }, index=idx)

    regime = compute_regime(macro, method="rules")
    print("\n=== Regime Summary ===")
    print(regime_summary(regime))
    print("\n=== Monthly Regime ===")
    print(regime_to_monthly(regime))
