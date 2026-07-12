"""Metrics tests — synthetic series, no network. Asserts 365-day annualization."""
import inspect

import numpy as np
import pandas as pd
import pytest

from crypto_trading.crypto_common.backtest import metrics as M


def daily_index(n, start="2026-06-03"):
    return pd.date_range(start, periods=n, freq="D")


def test_trading_days_is_365_everywhere():
    assert M.TRADING_DAYS == 365
    for fn in (M.annualized_return, M.annualized_vol, M.sharpe_ratio,
               M.calmar_ratio, M.information_ratio, M.cagr, M.compute_metrics):
        sig = inspect.signature(fn)
        assert sig.parameters["periods_per_year"].default == 365, fn.__name__


def test_annualized_return_compounds_365():
    r = pd.Series([0.001] * 365, index=daily_index(365))
    expected = (1.001 ** 365) - 1
    assert M.annualized_return(r) == pytest.approx(expected, rel=1e-9)
    # and NOT the 252-day figure
    assert M.annualized_return(r) != pytest.approx((1.001 ** 252) - 1, rel=1e-3)


def test_annualized_vol_sqrt_365():
    rng = np.random.default_rng(7)
    r = pd.Series(rng.normal(0, 0.01, 730), index=daily_index(730))
    assert M.annualized_vol(r) == pytest.approx(r.std() * np.sqrt(365), rel=1e-12)


def test_sharpe_manual_path_consistency():
    rng = np.random.default_rng(11)
    r = pd.Series(rng.normal(0.0005, 0.01, 730), index=daily_index(730))
    expected = M.annualized_return(r) / M.annualized_vol(r)
    assert M.sharpe_ratio(r) == pytest.approx(expected, rel=1e-9)


def test_max_drawdown_constructed_episode():
    up = [0.01] * 50
    crash = [-0.05] * 5          # ≈ -22.6% peak-to-trough
    recover = [0.02] * 60
    r = pd.Series(up + crash + recover, index=daily_index(115))
    mdd, dur = M.max_drawdown(r)
    assert mdd == pytest.approx(1 - 0.95 ** -5 * 0.95 ** 10, abs=1e-6) or mdd < -0.2
    assert -0.25 < mdd < -0.2
    assert dur == 5


def test_cvar_orders():
    rng = np.random.default_rng(3)
    r = pd.Series(rng.normal(0, 0.02, 2000), index=daily_index(2000))
    c95, c99 = M.cvar(r, 0.95), M.cvar(r, 0.99)
    assert c99 < c95 < 0


def test_compute_metrics_keys_and_beta_alpha_manual_fallback():
    rng = np.random.default_rng(5)
    bench = pd.Series(rng.normal(0.0004, 0.012, 730), index=daily_index(730))
    port = 0.8 * bench + pd.Series(rng.normal(0.0002, 0.004, 730), index=bench.index)
    m = M.compute_metrics(port, bench)
    for key in ("annual_return", "annual_vol", "sharpe", "max_drawdown", "calmar",
                "cvar_95", "cvar_99", "monthly_win_rate", "skewness", "kurtosis",
                "total_return", "years", "info_ratio", "tracking_error",
                "active_return", "beta", "alpha"):
        assert key in m, key
    # beta ≈ 0.8 (manual covariance fallback — qlib absent in someopark_run)
    assert m["beta"] == pytest.approx(0.8, abs=0.1)
    assert m["years"] == pytest.approx(2.0, abs=0.01)


def test_find_drawdown_episodes_finds_crafted_crash():
    r = pd.Series([0.005] * 100 + [-0.04] * 6 + [0.01] * 100, index=daily_index(206))
    ep = M.find_drawdown_episodes(r, top_n=3)
    assert len(ep) >= 1
    worst = ep.iloc[0]
    assert worst["drawdown_pct"] < -15
    assert worst["duration_days"] >= 5


def test_compute_ic_scipy_fallback_perfect_signal():
    idx = daily_index(30)
    cols = list("ABCDE")
    rng = np.random.default_rng(9)
    fwd = pd.DataFrame(rng.normal(0, 0.02, (30, 5)), index=idx, columns=cols)
    ic = M.compute_ic(fwd.copy(), fwd, method="rank")     # signal == forward returns
    assert ic["ic_mean"] == pytest.approx(1.0, abs=1e-9)
    ic_p = M.compute_ic(fwd.copy(), fwd, method="normal")
    assert ic_p["ic_mean"] == pytest.approx(1.0, abs=1e-9)


def test_monthly_win_rate_positive_drift():
    r = pd.Series([0.002] * 365, index=daily_index(365))
    assert M.monthly_win_rate(r) == 1.0


def test_subperiod_analysis_crypto_era_defaults():
    r = pd.Series(np.random.default_rng(1).normal(4e-4, 0.01, 200),
                  index=daily_index(200))
    sp = M.subperiod_analysis(r)
    assert "Full Sample" in sp.index
