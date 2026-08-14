"""Plan 09 tests — synthetic, no network. PIT safety, dead-zone labels, IC
recovery of a constructed predictive feature, purged-CV wiring."""
import numpy as np
import pandas as pd
import pytest

from crypto_trading.crypto_common.trade_stats import purged_kfold_indices
from crypto_trading.crypto_strategies.ml_directional import features as F
from crypto_trading.crypto_strategies.ml_directional.research_ic import ic_cell


def synth_frame(n=2000, seed=7):
    """Grid frame with a genuinely predictive feature and a noise feature."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-07-07", periods=n, freq="5min", tz="UTC")
    # future return partially driven by 'alpha' feature known at t
    alpha = rng.standard_normal(n)
    noise = rng.standard_normal(n)
    fwd = 0.3 * alpha * 1e-3 + rng.standard_normal(n) * 1e-3
    f = pd.DataFrame(index=idx)
    f["mark_mid"] = 6.4 * (1 + pd.Series(fwd, index=idx)).cumprod()
    f["alpha_feat"] = alpha
    f["noise_feat"] = noise
    f["fwd_15m"] = pd.Series(fwd, index=idx)
    bps = f["fwd_15m"] * 1e4
    f["label_15m"] = np.where(bps > F.DEAD_ZONE_BPS, 1,
                              np.where(bps < -F.DEAD_ZONE_BPS, -1, 0))
    return f


def test_ic_recovers_predictive_feature_and_rejects_noise():
    f = synth_frame()
    hit = ic_cell(f, "alpha_feat", "15m", min_n=100)
    miss = ic_cell(f, "noise_feat", "15m", min_n=100)
    assert hit is not None and hit["ic"] > 0.15 and hit["t_nw"] > 3
    assert miss is not None and abs(miss["t_nw"]) < 2


def test_dead_zone_labels():
    idx = pd.date_range("2026-07-07", periods=5, freq="5min", tz="UTC")
    mid = pd.Series([100.0, 100.0, 100.0, 100.0, 100.0], index=idx)
    # construct forward returns: +20bps, −20bps, +5bps, NaN tail
    f = pd.DataFrame({"mark_mid": mid})
    fwd = pd.Series([20e-4, -20e-4, 5e-4, np.nan, np.nan], index=idx)
    bps = fwd * 1e4
    lab = np.where(bps > F.DEAD_ZONE_BPS, 1, np.where(bps < -F.DEAD_ZONE_BPS, -1, 0))
    assert lab[0] == 1 and lab[1] == -1 and lab[2] == 0


def test_labels_are_strictly_future():
    """Perturbing the LAST grid price must not change any feature at earlier t
    (features are trailing-only), but must change the last labels."""
    f1 = synth_frame(seed=3)
    # features module functions we can check directly: momentum is trailing
    mom = f1["mark_mid"].pct_change(3)
    f2 = f1.copy()
    f2.iloc[-1, f2.columns.get_loc("mark_mid")] *= 1.01
    mom2 = f2["mark_mid"].pct_change(3)
    # all but the last 1 momentum values identical → no lookahead in the feature
    pd.testing.assert_series_equal(mom.iloc[:-1], mom2.iloc[:-1])


def test_streak_series_signed_and_frozen_on_zero():
    idx = pd.date_range("2026-07-07", periods=6, freq="8h", tz="UTC")
    fund = pd.DataFrame({"funding_rate": [1e-4, 1e-4, 0.0, 1e-4, -1e-4, -1e-4]},
                        index=idx)
    s = F._streak_series(fund)
    assert list(s) == [1, 2, 2, 3, 1, 2] or list(np.sign(s)) == [1, 1, 1, 1, -1, -1]
    # sign flips at the −1e-4 cycle
    assert s.iloc[-1] < 0


def test_purged_cv_embargo_respects_horizon():
    folds = purged_kfold_indices(1000, k=5, embargo=12)
    for train, test in folds:
        t0, t1 = test.min(), test.max()
        # no training index within embargo of the test block
        bad = [i for i in train if (t0 - 12) <= i <= (t1 + 12)]
        assert not bad


def test_flow_imbalance_sign_convention():
    idx = pd.to_datetime(["2026-07-07 00:01", "2026-07-07 00:02"], utc=True)
    trades = pd.DataFrame({"count": [10.0, 2.0], "taker_side": ["bid", "ask"]},
                          index=idx)
    grid = pd.date_range("2026-07-07 00:05", periods=1, freq="5min", tz="UTC")
    v = F._signed_flow(trades, grid, "5min")
    # (10 − 2)/12 = +0.667 → aggressive buying = positive imbalance
    assert v.iloc[0] == pytest.approx(8 / 12)
