"""Plan 05 alpha-research wiring tests — synthetic, no network. Verifies factor
isolation produces different composites and the regime-multiplier passthrough
actually changes behavior (the research's two load-bearing mechanisms)."""
import numpy as np
import pandas as pd
import pytest

from crypto_trading.crypto_common.regime import RISK_OFF, RISK_ON, TRANSITION_DOWN, TRANSITION_UP
from crypto_trading.crypto_strategies.perp_rotation.signals.composite import (
    compute_composite_signals)
from crypto_trading.crypto_strategies.perp_rotation.research_alpha import (
    APRIORI_MULTIPLIERS, FACTOR_WEIGHTS)


@pytest.fixture
def panels():
    idx = pd.date_range("2026-01-01", periods=120, freq="1D", tz="UTC")
    rng = np.random.default_rng(7)
    names = ["KXBTCPERP", "KXETHPERP", "KXSOLPERP", "KXXRPPERP"]
    # distinct momentum (drifts) AND distinct funding (levels) so cs-only and
    # carry-only rank differently
    drifts = {"KXBTCPERP": 0.004, "KXETHPERP": 0.001, "KXSOLPERP": -0.001,
              "KXXRPPERP": -0.004}
    fund = {"KXBTCPERP": 1e-3, "KXETHPERP": 2e-4, "KXSOLPERP": -2e-4,
            "KXXRPPERP": -1e-3}
    prices = pd.DataFrame({n: 100 * np.cumprod(1 + drifts[n] + 0.01 * rng.standard_normal(120))
                           for n in names}, index=idx)
    funding = pd.DataFrame({n: np.full(120, fund[n]) for n in names}, index=idx)
    # calm regime inputs → RISK_ON
    regime_inputs = pd.DataFrame({"btc_rvol": 35.0, "funding": 1e-4,
                                  "basis_dispersion": 30.0}, index=idx)
    return prices, funding, regime_inputs


def _kwargs():
    # carry_mode="level": cross-sectional funding-level ranking — percentile
    # mode has NO discrimination on constant per-name funding (each name sits
    # at the same percentile of its own history), which is the level mode's
    # whole reason to exist.
    return dict(signal_kwargs={"cs_lookback": 20, "ts_lookback": 20,
                               "cs_zscore_window": 0, "carry_lookback_days": 30,
                               "carry_mode": "level", "carry_level_smooth_days": 3})


def test_factor_isolation_differs(panels):
    prices, funding, regime_inputs = panels
    comps = {}
    for name in ("cs_momentum_only", "carry_only"):
        c, _, _ = compute_composite_signals(prices, funding, regime_inputs,
                                            weights=FACTOR_WEIGHTS[name], **_kwargs())
        comps[name] = c.dropna(how="all")
    tail_cs = comps["cs_momentum_only"].iloc[-1]
    tail_carry = comps["carry_only"].iloc[-1]
    # cs ranks by drift (BTC best); carry(long_receives) ranks negative funding
    # best (XRP best) → orderings must differ
    assert tail_cs.idxmax() == "KXBTCPERP"
    assert tail_carry.idxmax() == "KXXRPPERP"
    assert not np.allclose(tail_cs.to_numpy(dtype=float),
                           tail_carry.to_numpy(dtype=float))


def test_apriori_multipliers_change_composite_in_stress(panels):
    prices, funding, regime_inputs = panels
    # force a stressed regime: high rvol → RISK_OFF territory
    stressed = regime_inputs.copy()
    stressed["btc_rvol"] = 95.0
    base_c, reg, _ = compute_composite_signals(prices, funding, stressed, **_kwargs())
    cond_c, _, _ = compute_composite_signals(prices, funding, stressed,
                                             regime_multipliers=APRIORI_MULTIPLIERS,
                                             **_kwargs())
    assert (reg == RISK_OFF).mean() > 0.5          # the stress actually registered
    a = base_c.dropna(how="all").iloc[-1].to_numpy(dtype=float)
    b = cond_c.dropna(how="all").iloc[-1].to_numpy(dtype=float)
    assert not np.allclose(a, b)                   # momentum-off rule changed scores


def test_apriori_rule_covers_all_states():
    assert set(APRIORI_MULTIPLIERS) == {RISK_ON, TRANSITION_UP, TRANSITION_DOWN, RISK_OFF}
    assert APRIORI_MULTIPLIERS[RISK_OFF]["cross_sectional_momentum"] == 0.0
