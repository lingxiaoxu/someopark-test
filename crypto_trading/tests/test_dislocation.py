"""Plan 02 dislocation-factor tests — synthetic, no network."""
import numpy as np
import pandas as pd

from crypto_trading.crypto_strategies.event_perp.signals.dislocation import (
    DislocationParams, arb_violation_raw, composite, fair_value_gap_raw,
    rolling_z, skew_gap_raw, snapshot_factors, vol_gap_raw)
from crypto_trading.crypto_strategies.event_perp.signals.implied_dist import StrikeQuote


def test_fair_value_gap_sign():
    # implied above perp → perp cheap → positive gap (expect perp to rise)
    assert fair_value_gap_raw(60_500, 60_000) > 0
    assert fair_value_gap_raw(59_500, 60_000) < 0
    assert fair_value_gap_raw(60_000, 0) is None          # bad perp
    # carry adjustment shifts the implied mean
    base = fair_value_gap_raw(60_000, 60_000, funding_carry=0.0)
    up = fair_value_gap_raw(60_000, 60_000, funding_carry=0.01)
    assert base == 0.0 and up > 0


def test_vol_gap_and_skew_gap():
    # implied CoV 0.05 vs realized 0.02 → positive vol gap
    assert vol_gap_raw(3_000, 60_000, 0.02) > 0
    assert vol_gap_raw(3_000, 60_000, None) is None
    # skew gap: implied skew minus funding-sign prior
    assert skew_gap_raw(0.5, funding_rate=1e-4, k=1.0) == 0.5 - 1.0    # positive funding
    assert skew_gap_raw(0.5, funding_rate=-1e-4, k=1.0) == 0.5 + 1.0
    assert skew_gap_raw(0.5, funding_rate=0.0) == 0.5


def test_arb_violation_lights_on_injected_crossing():
    # clean monotone survival curve → no arb
    clean = [StrikeQuote(60_000, 0.60, 0.62, 100, 100),
             StrikeQuote(61_000, 0.40, 0.42, 100, 100),
             StrikeQuote(62_000, 0.20, 0.22, 100, 100)]
    assert arb_violation_raw(clean, [], fee_rate=0.07) == 0.0
    # inject a crossing: higher strike bid ABOVE lower strike ask
    bad = [StrikeQuote(60_000, 0.30, 0.35, 100, 100),
           StrikeQuote(61_000, 0.80, 0.85, 100, 100)]          # 0.80 > 0.35
    assert arb_violation_raw(bad, [], fee_rate=0.0) > 0        # fee-free → credit
    assert arb_violation_raw(bad, [], fee_rate=5.0) == 0.0     # huge fee kills it


def test_rolling_z_is_pit():
    s = pd.Series(np.arange(100.0))
    z = rolling_z(s, 30)
    early = z.iloc[50]
    z2 = rolling_z(pd.concat([s, pd.Series([1e6])], ignore_index=True), 30)
    assert z2.iloc[50] == early                              # future point doesn't leak back


def test_fair_value_gap_has_positive_ic_on_reverting_gap():
    # construct a mean-reverting implied-vs-perp gap → positive IC of gap_z vs
    # forward convergence (the property the composite relies on)
    rng = np.random.default_rng(3)
    n = 400
    implied = np.full(n, 60_000.0)
    perp = np.zeros(n)
    perp[0] = 60_000
    for i in range(1, n):
        # perp reverts toward implied (OU) + noise
        perp[i] = perp[i - 1] + 0.25 * (implied[i] - perp[i - 1]) + rng.normal(0, 30)
    gap = (implied - perp) / perp
    gz = rolling_z(pd.Series(gap), 60)
    d = perp - implied
    fwd = pd.Series(d).shift(-3) - pd.Series(d)
    pair = pd.DataFrame({"gz": gz, "fwd": fwd}).dropna()
    ic = pair["gz"].corr(pair["fwd"], method="spearman")
    assert ic > 0.1                                          # gap predicts convergence


def test_composite_weights_and_within_horizon_grouping():
    # two horizons interleaved; composite must z WITHIN each, not across
    rng = np.random.default_rng(1)
    rows = []
    for h in ("A", "B"):
        base = 0.0 if h == "A" else 100.0                   # very different levels
        for i in range(80):
            rows.append({"close_time": h,
                         "fair_value_gap": base + rng.normal(0, 1),
                         "vol_gap": rng.normal(0, 1), "skew_gap": rng.normal(0, 1),
                         "arb_violation": 0.0})
    frame = pd.DataFrame(rows)
    comp = composite(frame, DislocationParams(zwin=40))
    assert len(comp) == len(frame)
    # within-horizon z removes the 100-level offset → composite not dominated by it
    assert abs(comp.mean()) < 1.0 and comp.std() > 0
    assert "z_fair_value" in frame                          # z columns written back


def test_snapshot_factors_none_safe():
    f = snapshot_factors(None, perp_spot=60_000, realized_vol=0.02,
                         funding_rate=1e-4)
    assert f["fair_value_gap"] is None and f["arb_violation"] == 0.0
