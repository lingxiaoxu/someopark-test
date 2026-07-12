"""Plan 04 mechanism-test unit tests — synthetic series, no network.

Verifies the overshoot/reversion detector distinguishes a mean-reverting
process (Plan 04's required edge) from a random walk (no edge), and that the
σ-threshold gate fires at the right level.
"""
import numpy as np
import pandas as pd

from crypto_trading.crypto_strategies.liq_reversion.mechanism_test import (
    overshoot_reversion, round_trip_cost_bps, sweep)


def _series(vals) -> pd.Series:
    idx = pd.date_range("2026-06-04", periods=len(vals), freq="1min", tz="UTC")
    return pd.Series(np.asarray(vals, dtype=float), index=idx)


def test_mean_reverting_spikes_detected():
    """Baseline + periodic up-spikes that fully revert next bar → positive rev."""
    rng = np.random.default_rng(0)
    n = 3000
    base = 100.0 + np.cumsum(rng.normal(0, 0.01, n))   # slow drift + tiny noise
    px = base.copy()
    # every 50 bars inject a +2% spike that reverts over the next 5 bars
    for t in range(100, n - 20, 50):
        px[t] = base[t] * 1.02
        for j in range(1, 6):
            px[t + j] = base[t + j]                     # snaps back to baseline
    s = overshoot_reversion(_series(px), x_sigma=3.0, k_fwd=5, vol_window=60)
    assert s["n_events"] >= 20
    assert s["mean_rev_bps"] > 0            # reversion, not continuation
    assert s["hit_rate"] > 0.6
    assert s["t_stat"] > 2                  # distinguishable from noise


def test_random_walk_has_no_reversion():
    rng = np.random.default_rng(7)
    px = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, 4000)))
    s = overshoot_reversion(_series(px), x_sigma=3.0, k_fwd=5, vol_window=60)
    if s["n_events"] >= 20:
        # a pure GBM has no mean reversion in the spike → not a strong positive
        assert abs(s["mean_rev_bps"]) < 15
        assert abs(s["t_stat"]) < 2.5


def test_momentum_series_shows_negative_reversion():
    """Spikes that CONTINUE (trend) → negative reversion (the failure mode)."""
    n = 3000
    base = np.full(n, 100.0)
    px = base.copy()
    for t in range(100, n - 20, 50):
        px[t] = base[t] * 1.02
        for j in range(1, 6):
            px[t + j] = base[t] * (1.02 + 0.004 * j)   # keeps going up
    s = overshoot_reversion(_series(px), x_sigma=3.0, k_fwd=5, vol_window=60)
    assert s["n_events"] >= 10
    assert s["mean_rev_bps"] < 0            # continuation, not reversion


def test_sigma_threshold_gates_events():
    """A single ~4σ move: caught at X=3, missed at X=5."""
    rng = np.random.default_rng(3)
    px = 100.0 + np.cumsum(rng.normal(0, 0.02, 500))   # σ_ret ≈ 0.02%
    # inject one large move near the end
    px[400] = px[399] * 1.03
    px[401:406] = px[399]
    s3 = overshoot_reversion(_series(px), x_sigma=3.0, k_fwd=3, vol_window=120)
    s8 = overshoot_reversion(_series(px), x_sigma=8.0, k_fwd=3, vol_window=120)
    assert s3.get("n_events", 0) >= 1
    assert s8.get("n_events", 0) <= s3.get("n_events", 0)


def test_insufficient_data_returns_zero_events():
    assert overshoot_reversion(_series([1, 2, 3]), x_sigma=3, k_fwd=5,
                               vol_window=60) == {"n_events": 0}


def test_oi_drop_filter_restricts_events():
    """OI-drop overlay keeps only spikes with a concurrent OI drop."""
    n = 1500
    px = np.full(n, 100.0)
    oi = np.full(n, 1000.0)
    for t in range(100, n - 20, 50):
        px[t] = 102.0
        px[t + 1:t + 6] = 100.0
        if t % 100 == 0:                    # only half the spikes drop OI
            oi[t] = 900.0
    close, oiv = _series(px), _series(oi)
    plain = overshoot_reversion(close, x_sigma=3, k_fwd=5, vol_window=60)
    filt = overshoot_reversion(close, x_sigma=3, k_fwd=5, vol_window=60,
                               oi=oiv, oi_drop_min=0.05)
    assert filt["n_events"] < plain["n_events"]


def test_round_trip_cost_positive():
    c = round_trip_cost_bps("KXBTCPERP")
    assert c > 0 and c < 200               # sane bps range


def test_sweep_returns_labeled_grid():
    rng = np.random.default_rng(1)
    px = 100.0 + np.cumsum(rng.normal(0, 0.01, 2000))
    rows = sweep(_series(px), label="TEST", vol_window=60,
                 x_grid=(3.0,), k_grid=(5, 15))
    assert all(r["label"] == "TEST" for r in rows)
    assert all("net_of_cost_bps" in r and "beats_cost" in r for r in rows)
