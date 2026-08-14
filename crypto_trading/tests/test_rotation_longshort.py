"""Plan 05 long-short mode tests — synthetic, no network."""
import numpy as np
import pandas as pd
import pytest

from crypto_trading.crypto_common.backtest.daily_engine import PerpRotationBacktest
from crypto_trading.crypto_strategies.perp_rotation.long_short import (
    build_long_short_weights, ls_risk_scale)


def scores(**kv):
    return pd.Series(kv, dtype=float)


def test_dollar_neutral_and_gross():
    s = scores(A=2.0, B=1.0, C=0.2, D=-0.1, E=-1.0, F=-2.0)
    w = build_long_short_weights(s, k=2, gross=1.0)
    assert abs(w.sum()) < 1e-9                       # Σw ≈ 0
    assert abs(w.abs().sum() - 1.0) < 1e-9           # Σ|w| = gross
    assert set(w[w > 0].index) == {"A", "B"}         # top-2 long
    assert set(w[w < 0].index) == {"E", "F"}         # bottom-2 short
    assert (w.abs() <= 0.45 + 1e-9).all()            # per-name cap


def test_max_weight_cap_binds():
    s = scores(A=2.0, B=-2.0, C=0.1, D=-0.1)
    w = build_long_short_weights(s, k=1, gross=2.0, max_weight=0.45)
    # gross/2 = 1.0 per leg but cap 0.45 binds
    assert w["A"] == pytest.approx(0.45) and w["B"] == pytest.approx(-0.45)


def test_band_hysteresis_reduces_turnover():
    rng = np.random.default_rng(7)
    names = list("ABCDEFGH")
    base = pd.Series(np.linspace(2, -2, len(names)), index=names)
    prev0, prevB = None, None
    to0 = toB = 0.0
    for i in range(60):
        s = base + rng.normal(0, 0.6, len(names))    # rank jitter
        w0 = build_long_short_weights(pd.Series(s, index=names), prev0, k=2, band=0)
        wB = build_long_short_weights(pd.Series(s, index=names), prevB, k=2, band=2)
        if prev0 is not None:
            to0 += (w0 - prev0).abs().sum()
            toB += (wB - prevB).abs().sum()
        prev0, prevB = w0, wB
    assert toB < to0                                  # band cuts churn
    assert abs(wB.sum()) < 1e-9                       # still neutral


def _mini_panels(n_days=120, funding_on="C"):
    """3 flat-price perps; one pays persistent POSITIVE funding (crowded long).
    Flat prices → momentum≈0; carry ranks C worst → LS shorts C → collects."""
    idx = pd.date_range("2025-01-01", periods=n_days, freq="1D", tz="UTC")
    prices = pd.DataFrame(100.0, index=idx, columns=["A", "B", "C"])
    prices += np.random.default_rng(3).normal(0, 1e-4, prices.shape)  # cov non-degenerate
    funding = pd.DataFrame(0.0, index=idx, columns=["A", "B", "C"])
    funding[funding_on] = 3e-3                        # heavy positive funding
    funding["A"] = -1e-3                              # A pays shorts → long A collects
    regime = pd.DataFrame({"btc_rvol": 30.0}, index=idx)
    return prices, funding, regime


def _ls_cfg():
    return {
        "universe": {"listing_history_floor_days": 5, "min_perps_to_activate": 2,
                     "depth_qualify": {"min_daily_notional_usd": 0.0}},
        "signals": {"weights": {"cross_sectional_momentum": 0.0, "ts_momentum": 0.0,
                                "carry": 1.0, "regime_adjustment": 0.0},
                    "carry_mode": "level"},
        "portfolio": {"long_short": True, "ls": {"k_per_side": 1, "gross": 1.0},
                      "constraints": {"max_weight": 0.6}},
        "rebalance": {"frequency": "weekly", "emergency_derisk_rvol": 999.0},
        "risk": {"target_vol_annual": 0.50, "vol_target_mode": "absolute",
                 "dd_halve_threshold": -0.50, "dd_flat_threshold": None},
        "costs": {"fee_scenario": "zero"},
        "backtest": {"initial_capital": 1000.0},
        "stop_loss": {"enabled": False},
    }


def test_engine_ls_short_collects_positive_funding():
    prices, funding, regime = _mini_panels()
    res = PerpRotationBacktest(_ls_cfg()).run(prices, funding, regime)
    w = res.weights_history
    assert (w["C"].dropna() <= 0).all() and (w["C"].dropna() < 0).any()  # C shorted
    assert (w["A"].dropna() >= 0).all() and (w["A"].dropna() > 0).any()  # A long
    # flat prices ⇒ P&L ≈ funding; short C collects +3e-3, long A collects +1e-3
    assert res.metrics["funding_pnl_usd"] > 0
    assert res.equity_curve.iloc[-1] > res.equity_curve.iloc[0]


def test_engine_long_only_default_unchanged():
    prices, funding, regime = _mini_panels()
    cfg = _ls_cfg()
    cfg["portfolio"] = {"top_n": 2, "constraints": {"max_weight": 0.6},
                        "min_zscore": -5.0}           # long_short absent
    res = PerpRotationBacktest(cfg).run(prices, funding, regime)
    w = res.weights_history
    assert (w.fillna(0.0) >= -1e-12).all().all()      # no shorts on default path


def test_ls_risk_scale_levers():
    idx = pd.date_range("2025-01-01", periods=120, freq="1D", tz="UTC")
    calm = pd.Series(0.001, index=idx)
    regime = pd.DataFrame({"btc_rvol": 30.0}, index=idx)
    s, _ = ls_risk_scale(calm, regime, None, vol_target=0.20)
    assert s == pytest.approx(1.0)                    # calm & low vol → full gross
    # high realized vol → absolute target binds
    hot = pd.Series(np.random.default_rng(1).normal(0, 0.05, 120), index=idx)
    s2, f2 = ls_risk_scale(hot, regime, None, vol_target=0.20)
    assert s2 < 0.5 and f2.vol_scaling_triggered
    # DD flat tier
    eq = pd.Series(np.linspace(1000, 700, 120), index=idx)      # −30% DD
    s3, f3 = ls_risk_scale(calm, regime, eq, dd_flat_threshold=-0.25)
    assert s3 <= 0.10 + 1e-9 and f3.dd_circuit_triggered
    # rvol emergency halves
    panic = pd.DataFrame({"btc_rvol": 80.0}, index=idx)
    s4, f4 = ls_risk_scale(calm, panic, None, rvol_emergency_threshold=60.0)
    assert s4 == pytest.approx(0.5) and f4.emergency_triggered
