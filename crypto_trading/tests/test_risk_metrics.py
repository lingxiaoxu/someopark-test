"""risk/metrics.py tests — synthetic distributions, no network."""
import numpy as np
import pandas as pd
import pytest

from crypto_trading.crypto_common.risk import metrics as m

RNG = np.random.default_rng(7)


@pytest.fixture(scope="module")
def normal_returns():
    return pd.Series(RNG.normal(0.0, 0.02, 5000))


def test_var_parametric_close_to_historical_on_normal(normal_returns):
    vp = m.var_parametric(normal_returns, 0.95)
    vh = m.var_historical(normal_returns, 0.95)
    assert vp is not None and vh is not None
    assert abs(vp - vh) / vh < 0.10          # same tail on a normal sample


def test_cvar_exceeds_var(normal_returns):
    var = m.var_historical(normal_returns, 0.95)
    cvar = m.cvar_historical(normal_returns, 0.95)
    assert cvar > var > 0


def test_cornish_fisher_penalizes_negative_skew():
    neg_skew = pd.Series(-RNG.lognormal(0, 0.5, 5000))
    neg_skew = (neg_skew - neg_skew.mean()) / neg_skew.std() * 0.02
    cf = m.var_cornish_fisher(neg_skew, 0.95)
    assert cf["skew"] < -0.5
    assert cf["var_cf"] >= cf["var_param"]   # fat left tail ⇒ CF ≥ Gaussian


def test_cornish_fisher_floor_never_below_gaussian(normal_returns):
    cf = m.var_cornish_fisher(normal_returns, 0.95)
    assert cf["var_cf"] >= cf["var_param"] - 1e-12


def test_risk_contribution_euler_sums_to_100pct():
    df = pd.DataFrame({"s1": RNG.normal(0, 10, 300), "s2": RNG.normal(0, 5, 300),
                       "s3": RNG.normal(0, 1, 300)})
    rows = m.risk_contribution(df)
    total = sum(r["risk_contribution_pct"] for r in rows)
    assert abs(total - 100.0) < 1e-6
    assert rows[0]["component"] == "s1"      # biggest vol dominates


def test_stress_correlation_var_dominates_normal_var():
    a = pd.Series(RNG.normal(0, 10, 500))
    b = -a + pd.Series(RNG.normal(0, 1, 500))          # strongly hedged pair
    df = pd.DataFrame({"a": a, "b": b})
    normal_var = m.var_parametric(df.sum(axis=1), 0.95)
    stressed = m.stress_correlation_var(df, 0.95)
    assert stressed > normal_var * 3          # hedge dies when corr → 1


def test_cdar_geq_dar_and_bounded_by_maxdd():
    nav = pd.Series(100 * np.cumprod(1 + RNG.normal(0, 0.02, 500)))
    blk = m.cdar_block(nav, 0.95)
    assert blk["max_drawdown_pct"] >= blk["cdar_pct"] >= blk["dar_pct"] >= 0
    assert 0 <= blk["time_under_water_pct"] <= 100


def test_netting_helpers_and_deltas():
    pos = {"KXBTCPERP": 100, "KXETHPERP": -200}
    marks = {"KXBTCPERP": 6.38, "KXETHPERP": 1.79}
    e = m.exposures(pos, marks)
    assert e["gross"] == pytest.approx(100 * 6.38 + 200 * 1.79)
    assert e["net"] == pytest.approx(100 * 6.38 - 200 * 1.79)
    d = m.per_asset_delta(pos, marks)
    assert set(d) == {"BTC", "ETH"} and d["ETH"] < 0
    # beta map: ETH beta 0.8 to BTC
    nbd = m.net_btc_delta(d, {"ETH": 0.8})
    assert nbd == pytest.approx(d["BTC"] + 0.8 * d["ETH"])


def test_liquidation_distance_sign_by_side():
    assert m.liquidation_distance_pct(6.38, 5.74, +10) == pytest.approx(0.1003, abs=1e-3)
    assert m.liquidation_distance_pct(6.38, 7.02, -10) == pytest.approx(0.1003, abs=1e-3)
    assert m.liquidation_distance_pct(6.38, 5.74, 0) is None


def test_funding_and_basis_exposure():
    pos = {"KXBTCPERP": 100}
    marks = {"KXBTCPERP": 6.38}
    f = m.funding_exposure(pos, marks, {"KXBTCPERP": 1e-4})
    assert f["total_next_cycle"] == pytest.approx(-1e-4 * 100 * 6.38)  # long pays
    b = m.basis_exposure(pos, marks)
    assert b["net_per_bp"] == pytest.approx(100 * 6.38 * 1e-4)


def test_time_to_flatten_participation():
    assert m.time_to_flatten_days(1000, adv_contracts=1000) == pytest.approx(5.0)


def test_stress_table_btc_scenarios_scale_with_delta():
    t = m.stress_table(net_btc_delta_usd=1000.0, gross_usd=2000.0, net_usd=1000.0,
                       var95_usd=50.0, equity=1000.0)
    by = {r["scenario"]: r["est_pnl"] for r in t}
    assert by["BTC -10%"] == pytest.approx(-100.0)
    assert by["BTC -30%"] == pytest.approx(-300.0)
    assert by["Vol jump 2x"] == pytest.approx(-50.0)
    assert all(len(r) == 3 for r in t)
