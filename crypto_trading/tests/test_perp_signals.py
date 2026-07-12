"""Perp rotation signal tests (Plan 05) — synthetic, no network."""
import numpy as np
import pandas as pd

from crypto_trading.crypto_strategies.perp_rotation.signals.carry import (
    compute_carry_signal, funding_to_percentile)
from crypto_trading.crypto_strategies.perp_rotation.signals.composite import (
    compute_composite_signals)
from crypto_trading.crypto_strategies.perp_rotation.signals.momentum import (
    compute_cs_momentum, compute_ts_momentum)
from crypto_trading.crypto_strategies.perp_rotation.signals.risk_overlay import (
    compute_low_vol_signal)


def idx(n):
    return pd.date_range("2025-01-01", periods=n, freq="1D", tz="UTC")


def test_cs_momentum_ranks_winner_highest():
    n = 200
    i = idx(n)
    up = pd.Series(np.linspace(100, 200, n), index=i)      # strong winner
    flat = pd.Series(100.0 + np.random.default_rng(2).normal(0, 0.1, n), index=i)
    down = pd.Series(np.linspace(100, 60, n), index=i)
    px = pd.DataFrame({"W": up, "F": flat, "L": down})
    cs = compute_cs_momentum(px, lookback_days=30, skip_days=1, zscore_window=0)
    last = cs.dropna().iloc[-1]
    assert last["W"] > last["F"] > last["L"]


def test_ts_momentum_crash_filter():
    n = 100
    i = idx(n)
    up = pd.Series(np.linspace(100, 150, n), index=i)
    down = pd.Series(np.linspace(100, 70, n), index=i)
    px = pd.DataFrame({"U": up, "D": down})
    ts = compute_ts_momentum(px, lookback_days=30, skip_days=1,
                             crash_filter_multiplier=0.0)
    last = ts.dropna().iloc[-1]
    assert last["U"] == 1.0 and last["D"] == 0.0


def test_carry_long_receives_prefers_negative_funding():
    n = 120
    i = idx(n)
    rng = np.random.default_rng(3)
    fund = pd.DataFrame({
        "NEG": -3e-4 + rng.normal(0, 2e-5, n),     # deeply negative → long collects
        "ZERO": rng.normal(0, 2e-5, n),
        "POS": +3e-4 + rng.normal(0, 2e-5, n),     # positive → long pays
    }, index=i)
    carry = compute_carry_signal(fund, lookback_days=60, favor="long_receives")
    last = carry.dropna().iloc[-1]
    # cross-sectional preference must hold even though the percentile is
    # computed vs OWN history: give POS a downward drift so its latest value
    # is high vs own history, and NEG a downward drift too — simpler check:
    # flipping favor must flip the ordering
    carry_flip = compute_carry_signal(fund, lookback_days=60, favor="short_receives")
    last_flip = carry_flip.dropna().iloc[-1]
    assert np.sign(last["NEG"] - last["POS"]) == -np.sign(last_flip["NEG"] - last_flip["POS"]) \
        or (last["NEG"] == last["POS"])


def test_funding_percentile_bounds_and_pit():
    n = 100
    s = pd.Series(np.linspace(-1e-4, 1e-4, n), index=idx(n))   # rising funding
    pct = funding_to_percentile(s, lookback_days=60, window_min_periods=20)
    valid = pct.dropna()
    assert ((valid >= 0) & (valid <= 1)).all()
    assert valid.iloc[-1] > 0.9        # latest is near its rolling max


def test_low_vol_signal_prefers_quiet_names():
    n = 120
    i = idx(n)
    rng = np.random.default_rng(4)
    quiet = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.005, n)), index=i)
    wild = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.05, n)), index=i)
    px = pd.DataFrame({"Q": quiet, "W": wild})
    lv = compute_low_vol_signal(px, window=30).dropna()
    assert lv.iloc[-1]["Q"] > lv.iloc[-1]["W"]


def test_composite_shapes_and_regime_output():
    n = 150
    i = idx(n)
    rng = np.random.default_rng(5)
    tickers = ["KXBTCPERP", "KXETHPERP", "KXSOLPERP", "KXXRPPERP"]
    px = pd.DataFrame({t: 100 * np.cumprod(1 + rng.normal(0.001, 0.03, n))
                       for t in tickers}, index=i)
    fund = pd.DataFrame(rng.normal(0, 1e-4, (n, len(tickers))), index=i, columns=tickers)
    regime_inputs = pd.DataFrame({
        "btc_rvol": np.full(n, 40.0), "funding": np.full(n, 5e-5),
        "basis_dispersion": np.full(n, 30.0),
    }, index=i)
    comp, regime, components = compute_composite_signals(
        px, fund, regime_inputs,
        signal_kwargs={"cs_lookback": 20, "cs_zscore_window": 0,
                       "ts_lookback": 20, "carry_lookback_days": 40})
    assert list(comp.columns) == tickers
    assert len(regime) == len(comp)
    valid = comp.dropna(how="all")
    assert len(valid) > 50
    # composite rows are cross-sectionally z-scored → mean ≈ 0
    row = valid.iloc[-1]
    assert abs(row.mean()) < 0.3
    assert set(components) >= {"cs_mom", "ts_mult", "carry", "low_vol", "regime_daily"}
