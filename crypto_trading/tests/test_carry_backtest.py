"""Plan 03 carry backtest + signal tests — synthetic funding/price, no network."""
import numpy as np
import pandas as pd

from crypto_trading.crypto_strategies.funding_carry.backtest import backtest_perp
from crypto_trading.crypto_strategies.funding_carry.signals.funding import (CarryParams,
                                                                            carry_signal,
                                                                            forecast_next_funding)


def cycles(n, start="2026-06-03"):
    return pd.date_range(start, periods=n, freq="8h", tz="UTC")


def test_collect_side_sign_positive_funding():
    # positive funding → longs pay → collect by SHORTING (sign −1)
    rs = pd.Series([1e-3] * 8)
    ps = pd.Series([100.0] * 8)
    sig = carry_signal(rs, ps, CarryParams(min_carry_edge_per_cycle=1e-4))
    assert sig["position_sign"] == -1 and sig["gate_pass"]


def test_negative_funding_collects_long():
    rs = pd.Series([-1e-3] * 8)
    ps = pd.Series([100.0] * 8)
    sig = carry_signal(rs, ps, CarryParams(min_carry_edge_per_cycle=1e-4))
    assert sig["position_sign"] == +1


def test_gate_blocks_thin_skew():
    # 0.5 bps skew vs 5 bps gate → never trade
    rs = pd.Series([5e-5] * 8)
    ps = pd.Series([100.0] * 8)
    sig = carry_signal(rs, ps, CarryParams(min_carry_edge_per_cycle=5e-4))
    assert sig["position_sign"] == 0 and not sig["gate_pass"]


def test_forecast_recency_weighted():
    rs = pd.Series([0.0, 0.0, 1e-3, 1e-3, 2e-3])
    f = forecast_next_funding(rs, window=5)
    assert 5e-4 < f < 2e-3          # weighted toward the recent higher values


def test_backtest_funding_harvest_flat_price():
    # constant +funding, FLAT price → naive collects funding cleanly, zero price P&L
    n = 40
    rates = pd.DataFrame({"funding_rate": [1e-3] * n}, index=cycles(n))
    price = pd.Series([100.0] * n, index=cycles(n))
    r = backtest_perp(rates, price, gated=False, fee_scenario="zero", ticker="KXBTCPERP")
    assert r["funding_collected"] > 0
    assert abs(r["price_pnl"]) < 1e-9            # flat price → no directional P&L
    # collecting side held every cycle → funding ≈ rate × (n−1)
    assert r["funding_collected"] == pytest_approx(1e-3 * (n - 1))


def test_backtest_directional_bleed_shows_in_price_pnl():
    # +funding but price trends UP → short (collect) side bleeds on price
    n = 40
    rates = pd.DataFrame({"funding_rate": [1e-3] * n}, index=cycles(n))
    price = pd.Series(100.0 * (1.01 ** np.arange(n)), index=cycles(n))  # +1%/cycle uptrend
    r = backtest_perp(rates, price, gated=False, fee_scenario="zero", ticker="KXBTCPERP")
    assert r["price_pnl"] < 0                    # short into an uptrend loses
    assert r["funding_collected"] > 0            # still collected funding


def pytest_approx(x, tol=1e-9):
    import pytest
    return pytest.approx(x, abs=tol)
