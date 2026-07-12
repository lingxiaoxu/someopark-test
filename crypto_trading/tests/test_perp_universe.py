"""Perp universe tests (Plan 05) — synthetic panels, no network."""
import numpy as np
import pandas as pd

from crypto_trading.crypto_strategies.perp_rotation.data.universe import (depth_qualified,
                                                                          get_universe,
                                                                          listing_floor_ok)


def panel(n_days=60, tickers=("KXBTCPERP", "KXETHPERP", "KXZECPERP")):
    idx = pd.date_range("2026-06-03", periods=n_days, freq="1D", tz="UTC")
    rng = np.random.default_rng(1)
    df = pd.DataFrame({t: 100 + rng.standard_normal(n_days).cumsum() for t in tickers},
                      index=idx)
    return df


def test_listing_floor_is_pit():
    px = panel(60)
    px.loc[px.index[:40], "KXZECPERP"] = np.nan     # ZEC lists on day 41
    asof_early = px.index[45]                        # ZEC has 5 days
    ok = listing_floor_ok(px, asof_early, floor_days=30)
    assert "KXZECPERP" not in ok and "KXBTCPERP" in ok
    asof_late = px.index[-1]                         # ZEC has 20 days — still short
    ok2 = listing_floor_ok(px, asof_late, floor_days=30)
    assert "KXZECPERP" not in ok2
    ok3 = listing_floor_ok(px, asof_late, floor_days=15)
    assert "KXZECPERP" in ok3                        # passes a lower floor


def test_depth_gate_filters_thin_names():
    px = panel(30)
    vol = pd.DataFrame(200_000.0, index=px.index, columns=px.columns)
    vol["KXZECPERP"] = 10_000.0                      # thin
    q = depth_qualified(vol, None, px.index[-1], min_daily_notional_usd=100_000)
    assert "KXZECPERP" not in q and "KXBTCPERP" in q


def test_activation_gate():
    px = panel(60)
    tickers, activated = get_universe(px, floor_days=30, min_perps_to_activate=6)
    assert not activated and len(tickers) == 3       # only 3 names → below gate
    tickers2, activated2 = get_universe(px, floor_days=30, min_perps_to_activate=3)
    assert activated2 and tickers2 == sorted(px.columns)
