"""trade_stats tests — synthetic, no network."""
import numpy as np
import pandas as pd
import pytest

from crypto_trading.crypto_common.trade_stats import (deflated_sharpe,
                                                      effective_sample_size,
                                                      newey_west_tstat,
                                                      purged_cv_evaluate,
                                                      purged_kfold_indices,
                                                      trade_significance_report)


def _ar1(n, rho, mean=0.0, seed=0):
    rng = np.random.default_rng(seed)
    e = rng.standard_normal(n)
    x = np.zeros(n)
    for i in range(1, n):
        x[i] = rho * x[i - 1] + e[i]
    return pd.Series(x + mean)


def test_nw_deflates_positive_autocorrelation():
    ar = _ar1(2000, 0.6, mean=0.1, seed=1)
    r = newey_west_tstat(ar)
    assert r["t_nw"] < r["t_naive"]           # HAC deflates when persistent
    assert r["lags"] > 0


def test_nw_matches_naive_on_iid():
    iid = pd.Series(np.random.default_rng(2).standard_normal(2000) + 0.1)
    r = newey_west_tstat(iid)
    assert abs(r["t_nw"] - r["t_naive"]) / abs(r["t_naive"]) < 0.10   # within 10%


def test_effective_sample_size():
    n = 2000
    assert effective_sample_size(_ar1(n, 0.6, seed=3)) < 0.6 * n      # persistent → fewer
    iid = pd.Series(np.random.default_rng(4).standard_normal(n))
    assert effective_sample_size(iid) > 0.85 * n                     # iid ≈ n


def test_purged_kfold_partition_and_purge():
    n, k, emb = 100, 5, 3
    folds = purged_kfold_indices(n, k, embargo=emb)
    assert len(folds) == k
    # test blocks disjoint and cover all
    all_test = np.concatenate([t for _, t in folds])
    assert sorted(all_test.tolist()) == list(range(n))
    # no training index sits within `embargo` of its test block
    for train, test in folds:
        lo, hi = test.min(), test.max()
        assert not any(lo - emb <= j < hi + 1 + emb for j in train)


def test_purged_cv_positive_edge_vs_noise():
    pos = pd.Series(np.random.default_rng(5).standard_normal(1500) + 0.15)
    cv = purged_cv_evaluate(pos, k=5)
    assert cv["frac_positive"] >= 0.6 and cv["pooled_oos_t"] > 0
    noise = pd.Series(np.random.default_rng(6).standard_normal(1500))
    assert purged_cv_evaluate(noise, k=5)["pooled_oos_t"] < 2.0


def test_deflated_sharpe_drops_with_more_trials():
    # marginal Sharpe so the deflation is visible
    r = pd.Series(np.random.default_rng(7).standard_normal(500) + 0.05)
    d1 = deflated_sharpe(r, n_trials=1)["dsr"]
    d50 = deflated_sharpe(r, n_trials=50)["dsr"]
    assert d50 <= d1                                                 # more trials → deflated


def test_report_significant_on_edge_not_on_noise():
    pos = pd.Series(np.random.default_rng(8).standard_normal(1500) + 0.15)
    assert trade_significance_report(pos)["significant"] is True
    noise = pd.Series(np.random.default_rng(9).standard_normal(1500))
    assert trade_significance_report(noise)["significant"] is False


def test_report_shape_and_neff():
    rep = trade_significance_report(_ar1(1500, 0.5, mean=0.1, seed=10))
    assert set(rep) >= {"mean", "n", "n_eff", "t_nw", "sharpe", "dsr", "purged_cv",
                        "significant"}
    assert rep["n_eff"] < rep["n"]                                   # persistent
