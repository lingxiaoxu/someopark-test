"""risk/aggregator.py tests — synthetic, tmp dirs, no network."""
import json

import numpy as np
import pandas as pd
import pytest

from crypto_trading.crypto_common import config as cfg
from crypto_trading.crypto_common import risk_kill as rk
from crypto_trading.crypto_common.risk.aggregator import (PortfolioAggregator,
                                                          StrategyState, run)

RNG = np.random.default_rng(11)


def mk_state(name, contracts, equity=1000.0, vol=5.0, **kw):
    return StrategyState(
        name=name, equity=equity,
        positions={"KXBTCPERP": contracts},
        marks={"KXBTCPERP": 6.38},
        returns=pd.Series(RNG.normal(0, vol, 200)),
        **kw)


@pytest.fixture
def isolated_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(rk, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(cfg, "SIGNALS_DIR", tmp_path / "signals")
    return tmp_path


def test_netting_long_vs_short_cancels_delta(isolated_dirs):
    agg = PortfolioAggregator([mk_state("s1", +100), mk_state("s2", -100)])
    rep = agg.compute()
    assert rep["exposure"]["net"] == pytest.approx(0.0)
    assert rep["exposure"]["gross"] == pytest.approx(2 * 100 * 6.38)
    assert rep["net_btc_delta"] == pytest.approx(0.0)


def test_limits_green_amber_red(isolated_dirs):
    # 100 contracts @ 6.38 on 1000 equity → gross lev 0.638: green
    rep = PortfolioAggregator([mk_state("s1", 100)]).compute()
    lev = next(c for c in rep["limits"] if c["name"] == "gross_leverage")
    assert lev["status"] == "green"
    # 260 contracts → lev 1.66: amber ; 320 → 2.04: red
    rep = PortfolioAggregator([mk_state("s1", 260)]).compute()
    assert next(c for c in rep["limits"]
                if c["name"] == "gross_leverage")["status"] == "amber"
    rep = PortfolioAggregator([mk_state("s1", 320)]).compute()
    assert next(c for c in rep["limits"]
                if c["name"] == "gross_leverage")["status"] == "red"


def test_liq_distance_below_direction(isolated_dirs):
    s = mk_state("s1", 100, liq_prices={"KXBTCPERP": 6.38 * 0.88})  # 12% away: red (<15)
    rep = PortfolioAggregator([s]).compute()
    liq = next(c for c in rep["limits"] if c["name"] == "min_liq_distance_pct")
    assert liq["status"] == "red"


def test_daily_loss_red_trips_kill_switch_and_halt_files(isolated_dirs):
    # equity 890 vs SOD 1000 → −11% daily loss: red → flatten-all + halt
    s = mk_state("s1", 10, equity=890.0, equity_sod=1000.0)
    rep = run([s])
    dl = next(c for c in rep["limits"] if c["name"] == "daily_loss_pct")
    assert dl["status"] == "red"
    assert rep["tripped"] == ["s1"]
    assert (rk.STATE_DIR / "halt_s1.json").exists()
    assert (rk.STATE_DIR / "halt_portfolio.json").exists()
    assert rk.RiskKill("s1").halted()


def test_green_book_trips_nothing(isolated_dirs):
    rep = run([mk_state("s1", 10, equity_sod=1000.0, equity_peak=1050.0)])
    assert rep["tripped"] == []
    assert not (rk.STATE_DIR / "halt_portfolio.json").exists()


def test_snapshot_written_and_parseable(isolated_dirs):
    agg = PortfolioAggregator([mk_state("s1", 50)])
    path = agg.snapshot()
    data = json.loads(open(path).read())
    assert "limits" in data and "var" in data and "stress" in data
    assert str(cfg.SIGNALS_DIR) in path


def test_risk_contribution_present_with_two_strategies(isolated_dirs):
    rep = PortfolioAggregator([mk_state("s1", 10, vol=10.0),
                               mk_state("s2", 10, vol=1.0)]).compute()
    rc = rep["risk_contribution"]
    assert rc and rc[0]["component"] == "s1"
    assert rep["var"]["stress_correlation_var_95"] is not None
