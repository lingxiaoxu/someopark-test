"""Regime tests — synthetic inputs driving each state; API-shape checks. No network."""
import numpy as np
import pandas as pd
import pytest

from crypto_trading.crypto_common import regime as R


def test_state_constants_api_unchanged():
    assert R.REGIME_STATES == ["risk_on", "transition_up", "transition_down", "risk_off"]
    assert R.REGIME_NUMERIC[R.RISK_ON] == 3 and R.REGIME_NUMERIC[R.RISK_OFF] == 0
    assert R.REGIME_FROM_NUMERIC[2] == R.TRANSITION_UP
    assert set(R.REGIME_COLORS) == set(R.REGIME_STATES)


# ── _classify_regime_row: drive each of the 4 states deterministically ──────

def test_row_risk_on():
    s = R._classify_regime_row(
        rvol=-1.5, funding_raw=1e-4, basis_disp=-1.5, dom_chg=-2.0,
        rvol_raw=20.0, basis_disp_raw=5.0)
    # +2 (calm) +1 (vol falling) +1 (healthy funding) +1 (tight basis)
    # +1 (basis easing) +1 (alt breadth) = +7
    assert s == R.RISK_ON


def test_row_risk_off_hard_stop_rvol():
    assert R._classify_regime_row(
        rvol=0.0, funding_raw=1e-4, basis_disp=0.0, dom_chg=0.0,
        rvol_raw=95.0, basis_disp_raw=5.0) == R.RISK_OFF


def test_row_risk_off_hard_stop_basis_blowout():
    assert R._classify_regime_row(
        rvol=0.0, funding_raw=1e-4, basis_disp=0.0, dom_chg=0.0,
        rvol_raw=40.0, basis_disp_raw=80.0) == R.RISK_OFF   # > 50 × 1.3


def test_row_risk_off_by_score():
    s = R._classify_regime_row(
        rvol=2.0, funding_raw=-4e-4, basis_disp=2.0, dom_chg=3.0,
        rvol_raw=80.0, basis_disp_raw=60.0)
    # -1 (high vol) -1 (spiking) -1 (funding panic) -1 (wide basis)
    # -1 (basis stress) -1 (flight to quality) = -6
    assert s == R.RISK_OFF


def test_row_transition_up():
    s = R._classify_regime_row(
        rvol=0.0, funding_raw=1e-4, basis_disp=0.0, dom_chg=0.0,
        rvol_raw=40.0, basis_disp_raw=20.0)
    # +1 (normal vol) +1 (healthy funding) = +2 → TRANSITION_UP
    assert s == R.TRANSITION_UP


def test_row_transition_down():
    s = R._classify_regime_row(
        rvol=0.0, funding_raw=8e-4, basis_disp=0.0, dom_chg=0.0,
        rvol_raw=50.0, basis_disp_raw=20.0)
    # 0 (elevated vol) -1 (froth funding) = -1 → TRANSITION_DOWN
    assert s == R.TRANSITION_DOWN


def test_row_nan_rvol_defaults_risk_on():
    assert R._classify_regime_row(
        rvol=np.nan, funding_raw=np.nan, basis_disp=np.nan, dom_chg=None,
        rvol_raw=np.nan, basis_disp_raw=np.nan) == R.RISK_ON


def test_row_optional_slots_none_safe():
    base = dict(rvol=0.0, funding_raw=1e-4, basis_disp=0.0, dom_chg=0.0,
                rvol_raw=40.0, basis_disp_raw=20.0)
    s_none = R._classify_regime_row(**base)
    s_stress = R._classify_regime_row(**base, offshore_funding_z=2.0,
                                      stress_level=1.5, flow_level=1.0)
    # +2 baseline vs +2-3=-1 with all three stress slots firing
    assert s_none == R.TRANSITION_UP and s_stress == R.TRANSITION_DOWN


# ── series-level pipeline ────────────────────────────────────────────────────

def synthetic_macro(n_calm=250, n_mid=60, n_crisis=60):
    n = n_calm + n_mid + n_crisis
    idx = pd.date_range("2026-06-03", periods=n, freq="D", tz="UTC")
    return pd.DataFrame({
        "btc_rvol": np.r_[np.full(n_calm, 25.0), np.full(n_mid, 50.0), np.full(n_crisis, 95.0)],
        "funding": np.r_[np.full(n_calm, 1e-4), np.full(n_mid, 6e-4), np.full(n_crisis, -4e-4)],
        "basis_dispersion": np.r_[np.full(n_calm, 8.0), np.full(n_mid, 30.0), np.full(n_crisis, 70.0)],
        "btc_dominance": np.r_[np.linspace(55, 53, n_calm), np.linspace(53, 60, n_mid + n_crisis)],
    }, index=idx)


def test_compute_regime_rules_phases():
    macro = synthetic_macro()
    reg = R.compute_regime(macro, method="rules")
    assert isinstance(reg, pd.Series) and len(reg) == len(macro)
    assert set(reg.unique()) <= set(R.REGIME_STATES)
    # calm phase settles into RISK_ON; crisis phase (rvol 95 > extreme) is RISK_OFF
    assert (reg.iloc[50:250] == R.RISK_ON).mean() > 0.9
    assert (reg.iloc[-50:] == R.RISK_OFF).all()


def test_smoothing_reduces_chatter():
    macro = synthetic_macro()
    rng = np.random.default_rng(0)
    macro["btc_rvol"] += rng.normal(0, 8, len(macro))   # inject flip noise
    raw = R.compute_regime_rules(macro, smoothing_days=1)
    smooth = R.compute_regime_rules(macro, smoothing_days=5)
    assert (smooth != smooth.shift()).sum() <= (raw != raw.shift()).sum()


def test_regime_to_monthly_mode():
    macro = synthetic_macro()
    monthly = R.regime_to_monthly(R.compute_regime(macro))
    assert monthly.iloc[0] in R.REGIME_STATES
    assert monthly.index.is_monotonic_increasing


def test_regime_summary_shape():
    macro = synthetic_macro()
    summ = R.regime_summary(R.compute_regime(macro))
    assert list(summ.index) == R.REGIME_STATES
    assert summ["count_days"].sum() == len(macro)


def test_realized_vol_helper_annualizes_365():
    rng = np.random.default_rng(2)
    px = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.02, 400))),
                   index=pd.date_range("2026-01-01", periods=400, freq="D", tz="UTC"))
    rv = R.realized_vol(px, window=30)
    daily_std = px.pct_change().rolling(30, min_periods=10).std()
    expected = daily_std * np.sqrt(365) * 100
    pd.testing.assert_series_equal(rv.dropna(), expected.dropna(), rtol=1e-10)


def test_compute_regime_hmm_raises_without_hmmlearn():
    with pytest.raises((ImportError, ValueError)):
        R.compute_regime(synthetic_macro(), method="hmm")


def test_unknown_method_raises():
    with pytest.raises(ValueError):
        R.compute_regime(synthetic_macro(), method="nope")
