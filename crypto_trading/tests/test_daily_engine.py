"""Daily engine tests (Plan 05) — synthetic 3-perp panel, no network."""
import numpy as np
import pandas as pd
import pytest

from crypto_trading.crypto_common.backtest.daily_engine import PerpRotationBacktest


def make_inputs(n=150, funding_btc=0.0):
    i = pd.date_range("2025-01-01", periods=n, freq="1D", tz="UTC")
    rng = np.random.default_rng(7)
    px = pd.DataFrame({
        "KXBTCPERP": 100 * np.cumprod(1 + rng.normal(0.002, 0.02, n)),   # winner
        "KXETHPERP": 100 * np.cumprod(1 + rng.normal(0.000, 0.02, n)),
        "KXSOLPERP": 100 * np.cumprod(1 + rng.normal(-0.002, 0.02, n)),  # loser
    }, index=i)
    fund = pd.DataFrame(0.0, index=i, columns=px.columns)
    fund["KXBTCPERP"] = funding_btc
    regime_inputs = pd.DataFrame({
        "btc_rvol": np.full(n, 35.0), "funding": np.full(n, 0.0),
        "basis_dispersion": np.full(n, 30.0),
    }, index=i)
    return px, fund, regime_inputs


def cfg(**over):
    base = {
        "universe": {"min_perps_to_activate": 3, "listing_history_floor_days": 10},
        "signals": {"cs_lookback": 20, "cs_zscore_window": 0, "ts_lookback": 20,
                    "carry_lookback_days": 40},
        "portfolio": {"optimizer": "inv_vol", "top_n": 2, "min_zscore": -0.5,
                      "cov": {"lookback_days": 90, "min_periods": 15},
                      "constraints": {"max_weight": 0.6}},
        "rebalance": {"frequency": "weekly", "zscore_change_threshold": 0.0,
                      "max_turnover": 1.0},
        "risk": {"target_vol_annual": 0.60, "vol_scaling_enabled": False,
                 "dd_halve_threshold": -0.50},
        "stop_loss": {"enabled": False},
        "costs": {"fee_scenario": "zero", "slippage_bps": 0.0},
        "backtest": {"initial_capital": 1000.0},
    }
    base.update(over)
    return base


def test_engine_runs_and_tilts_to_winner():
    px, fund, ri = make_inputs()
    res = PerpRotationBacktest(cfg()).run(px, fund, ri)
    assert len(res.equity_curve) > 100
    w = res.weights_history
    assert len(w) > 5
    # momentum tilt: BTC (winner) average weight should beat SOL (loser)
    assert w["KXBTCPERP"].mean() > w["KXSOLPERP"].mean()
    # both benchmarks computed
    assert res.benchmark_equity is not None and res.benchmark_ew_equity is not None
    assert "ew_total_return" in res.metrics


def test_weights_respect_max_weight_and_long_only():
    px, fund, ri = make_inputs()
    res = PerpRotationBacktest(cfg()).run(px, fund, ri)
    w = res.weights_history
    assert (w.values >= -1e-9).all()
    assert (w.values <= 0.6 + 1e-6).all()


def test_funding_accrual_sign_long_pays_positive_rate():
    """Same panel ± a large positive BTC funding rate: the funding-charged run
    must end LOWER (long pays when funding positive — costs.funding_payment
    convention single-sourced)."""
    px, fund0, ri = make_inputs(funding_btc=0.0)
    pxb, fund_pos, rib = make_inputs(funding_btc=+3e-3)   # 30 bps/day — large
    res0 = PerpRotationBacktest(cfg()).run(px, fund0, ri)
    res1 = PerpRotationBacktest(cfg()).run(pxb, fund_pos, rib)
    assert res1.metrics["funding_pnl_usd"] < 0            # long paid
    assert res1.equity_curve.iloc[-1] < res0.equity_curve.iloc[-1]
    # and negative funding must CREDIT the long book
    pxc, fund_neg, ric = make_inputs(funding_btc=-3e-3)
    res2 = PerpRotationBacktest(cfg()).run(pxc, fund_neg, ric)
    assert res2.metrics["funding_pnl_usd"] > 0


def test_costs_reduce_equity_and_flow_through_returns():
    px, fund, ri = make_inputs()
    free = PerpRotationBacktest(cfg()).run(px, fund, ri)
    costly = PerpRotationBacktest(
        cfg(costs={"fee_scenario": "projected", "slippage_bps": 20.0})).run(px, fund, ri)
    assert costly.equity_curve.iloc[-1] < free.equity_curve.iloc[-1]
    # equity and compounded daily_returns must AGREE (the template landmine fix)
    recompounded = 1000.0 * (1 + costly.daily_returns).prod()
    assert abs(recompounded - costly.equity_curve.iloc[-1]) < 1e-6


def test_activation_gate_failure_raises_nothing_but_warns():
    px, fund, ri = make_inputs()
    c = cfg()
    c["universe"]["min_perps_to_activate"] = 10          # can't be met with 3
    res = PerpRotationBacktest(c).run(px, fund, ri)      # still runs (scaffold)
    assert len(res.equity_curve) > 0


def test_no_tickers_raises():
    px, fund, ri = make_inputs(n=15)                     # shorter than floor
    c = cfg()
    c["universe"]["listing_history_floor_days"] = 30
    with pytest.raises(ValueError):
        PerpRotationBacktest(c).run(px, fund, ri)
