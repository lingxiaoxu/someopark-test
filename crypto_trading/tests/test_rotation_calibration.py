"""Plan 05 risk-calibration tests — synthetic, no network."""
import numpy as np
import pandas as pd
import pytest

from crypto_trading.crypto_strategies.perp_rotation.calibrate_risk import (apply_config,
                                                                           sweep_grid)
from crypto_trading.crypto_strategies.perp_rotation.portfolio.risk import (
    apply_risk_controls, vol_scaling_factor)
from crypto_trading.crypto_strategies.perp_rotation.signals.carry import compute_carry_signal


def _regime(rvol: float, n=30):
    idx = pd.date_range("2026-01-01", periods=n, freq="1D", tz="UTC")
    return pd.DataFrame({"btc_rvol": rvol}, index=idx)


def _weights():
    return pd.Series({"A": 0.5, "B": 0.5})


def test_spike_mode_does_not_bind_when_vol_persistently_high():
    # the diagnosed non-bug-but-design-mismatch: realized≈historical at 60% —
    # spike mode returns 1.0 (target 0.40 ignored) → the −51% maxDD root cause
    assert vol_scaling_factor(0.60, 0.58, target_vol=0.40, mode="spike") == 1.0


def test_absolute_mode_binds_on_high_vol():
    f = vol_scaling_factor(0.60, 0.58, target_vol=0.30, mode="absolute")
    assert f == pytest.approx(0.5)                      # 0.30/0.60
    # and does not lever up when vol is low
    assert vol_scaling_factor(0.20, 0.25, target_vol=0.40, mode="absolute") == 1.0


def test_absolute_mode_shrinks_exposure_in_pipeline():
    # high realized vol (~63%) synthetic daily returns → weights scaled down
    rng = np.random.default_rng(7)
    rets = pd.Series(rng.normal(0, 0.033, 60),
                     index=pd.date_range("2026-01-01", periods=60, freq="1D", tz="UTC"))
    w, cash, flags = apply_risk_controls(
        weights=_weights(), portfolio_returns=rets, regime_inputs=_regime(30.0),
        vol_target=0.30, vol_target_mode="absolute", vol_scaling_enabled=True)
    assert flags.vol_scaling_triggered and w.sum() < 0.75 and cash > 0.2


def test_dd_flat_tier_triggers_at_depth():
    eq = pd.Series(np.linspace(1.0, 0.78, 30),        # −22% drawdown
                   index=pd.date_range("2026-01-01", periods=30, freq="1D", tz="UTC"))
    w, cash, flags = apply_risk_controls(
        weights=_weights(), portfolio_returns=pd.Series(dtype=float),
        regime_inputs=_regime(30.0), equity_curve=eq,
        dd_halve_threshold=-0.10, dd_flat_threshold=-0.20)
    assert flags.dd_circuit_triggered and cash == pytest.approx(0.90)
    assert w.sum() == pytest.approx(0.10, abs=1e-6)   # ~flat
    # shallower dd (−12%) → halve tier, not flat
    eq2 = pd.Series(np.linspace(1.0, 0.88, 30), index=eq.index)
    w2, cash2, _ = apply_risk_controls(
        weights=_weights(), portfolio_returns=pd.Series(dtype=float),
        regime_inputs=_regime(30.0), equity_curve=eq2,
        dd_halve_threshold=-0.10, dd_flat_threshold=-0.20)
    assert 0.4 < cash2 < 0.6


def test_regime_cashout_flattens_on_high_rvol():
    w, cash, flags = apply_risk_controls(
        weights=_weights(), portfolio_returns=pd.Series(dtype=float),
        regime_inputs=_regime(85.0), rvol_emergency_threshold=80.0,
        emergency_cash_pct=0.50)
    assert flags.emergency_triggered and cash == pytest.approx(0.50)


def test_carry_level_mode_ranks_most_negative_funding_top():
    idx = pd.date_range("2026-01-01", periods=30, freq="1D", tz="UTC")
    fp = pd.DataFrame({"BCH": -0.0004, "BTC": 0.0002, "ETH": -0.0001}, index=idx)
    sig = compute_carry_signal(fp, mode="level", level_smooth_days=3)
    last = sig.iloc[-1]
    assert last["BCH"] > last["ETH"] > last["BTC"]     # most negative funding wins


def test_is_oos_split_no_leakage():
    grid = sweep_grid(quick=True)
    assert len(grid) >= 4
    base = {"universe": {}, "signals": {}, "risk": {}, "rebalance": {}, "costs": {},
            "portfolio": {}, "backtest": {}, "stop_loss": {}}
    cfg = apply_config(base, grid[0], carry_mode="level")
    # frozen params carried verbatim; rvol None encoded as disabled (999)
    assert cfg["risk"]["vol_target_mode"] == "absolute"
    assert cfg["signals"]["carry_mode"] == "level"
    g_none = [g for g in grid if g["emergency_derisk_rvol"] is None][0]
    assert apply_config(base, g_none)["rebalance"]["emergency_derisk_rvol"] == 999.0
    # base dict not mutated (deepcopy discipline = no cross-config leakage)
    assert base["risk"] == {} and base["signals"] == {}
