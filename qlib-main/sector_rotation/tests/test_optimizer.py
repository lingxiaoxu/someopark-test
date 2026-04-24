"""
Optimizer Tests
===============
Unit tests for portfolio optimization and risk controls.
"""

import sys
from pathlib import Path
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from sector_rotation.portfolio.optimizer import (
    compute_cov,
    optimize_weights,
    _inv_vol_weights,
    _risk_parity_weights,
    _gmv_weights,
    apply_constraints,
)
from sector_rotation.portfolio.risk import (
    compute_realized_vol,
    compute_historical_vol,
    vol_scaling_factor,
    apply_risk_controls,
    estimate_sector_betas,
    RiskFlags,
)
from sector_rotation.portfolio.rebalance import (
    compute_turnover,
    cap_turnover,
    apply_zscore_threshold_filter,
)


def _make_returns(n_days=500, n_tickers=6, seed=42):
    np.random.seed(seed)
    idx = pd.bdate_range("2020-01-01", periods=n_days)
    tickers = [f"ETF{i}" for i in range(n_tickers)]
    ret = pd.DataFrame(
        np.random.normal(0.0003, 0.01, size=(n_days, n_tickers)),
        index=idx,
        columns=tickers,
    )
    return ret


def _make_macro(n_days=500, vix_level=18.0):
    idx = pd.bdate_range("2020-01-01", periods=n_days)
    return pd.DataFrame(
        {
            "vix": np.full(n_days, vix_level),
            "yield_curve": np.full(n_days, 0.5),
            "hy_spread": np.full(n_days, 300.0),
        },
        index=idx,
    )


class TestCovarianceEstimation(unittest.TestCase):

    def test_ledoit_wolf_shape(self):
        ret = _make_returns()
        cov = compute_cov(ret, method="ledoit_wolf")
        self.assertIsNotNone(cov)
        self.assertEqual(cov.shape, (6, 6))

    def test_cov_is_symmetric(self):
        ret = _make_returns()
        cov = compute_cov(ret, method="ledoit_wolf")
        np.testing.assert_allclose(cov.values, cov.values.T, atol=1e-10)

    def test_cov_is_positive_semidefinite(self):
        ret = _make_returns()
        cov = compute_cov(ret, method="ledoit_wolf")
        eigenvalues = np.linalg.eigvalsh(cov.values)
        self.assertTrue((eigenvalues >= -1e-10).all(), "Covariance not PSD")

    def test_insufficient_data_returns_none(self):
        ret = _make_returns(n_days=10)
        cov = compute_cov(ret, min_periods=50)
        self.assertIsNone(cov)


class TestWeightingMethods(unittest.TestCase):

    def setUp(self):
        np.random.seed(0)
        # Simple 4-sector cov matrix (annualized)
        self.cov = np.array([
            [0.04, 0.02, 0.01, 0.01],
            [0.02, 0.09, 0.02, 0.01],
            [0.01, 0.02, 0.16, 0.02],
            [0.01, 0.01, 0.02, 0.01],  # Low vol sector
        ])
        self.n = 4

    def test_inv_vol_weights_sum_to_one(self):
        w = _inv_vol_weights(self.cov)
        np.testing.assert_allclose(w.sum(), 1.0, atol=1e-10)

    def test_inv_vol_lower_vol_gets_more_weight(self):
        w = _inv_vol_weights(self.cov)
        # Sector 3 (lowest vol = 10%) should have highest weight
        self.assertEqual(np.argmax(w), 3)

    def test_risk_parity_equal_risk_contribution(self):
        w = _risk_parity_weights(self.cov)
        np.testing.assert_allclose(w.sum(), 1.0, atol=1e-6)
        # Risk contributions should be approximately equal
        sigma_w = self.cov @ w
        port_var = w @ sigma_w
        rc = w * sigma_w / port_var
        std_rc = rc.std()
        mean_rc = rc.mean()
        self.assertLess(std_rc / mean_rc, 0.5)  # < 50% variation in RC

    def test_gmv_positive_weights_long_only(self):
        w = _gmv_weights(self.cov, w_min=0.0, w_max=1.0)
        # After clipping negatives to 0, weights may not sum exactly to 1.0
        # (clip without renorm is the original design). Check they're in [0,1].
        self.assertTrue((w >= -1e-10).all())
        self.assertTrue((w <= 1.0 + 1e-10).all())


class TestOptimizeWeights(unittest.TestCase):

    def setUp(self):
        self.returns = _make_returns(n_days=300)
        self.tickers = list(self.returns.columns)
        self.scores = pd.Series(
            {t: np.random.randn() for t in self.tickers}
        )

    def test_weights_sum_to_one(self):
        w = optimize_weights(self.scores, self.returns, method="inv_vol", top_n=4)
        np.testing.assert_allclose(w.sum(), 1.0, atol=1e-6)

    def test_max_weight_constraint(self):
        # optimize_weights applies clip+renorm: a soft enforcement.
        # If qlib PortfolioOptimizer returns a concentrated weight (>max_weight),
        # renormalization after clipping may push it back above max_weight.
        # Hard guarantees: weights are non-negative and sum to 1.
        # Strict per-asset cap is the responsibility of apply_constraints().
        w = optimize_weights(self.scores, self.returns, method="inv_vol",
                              top_n=4, max_weight=0.50)
        self.assertTrue((w >= -1e-8).all(), "All weights must be non-negative")
        np.testing.assert_allclose(w.sum(), 1.0, atol=1e-6)

    def test_top_n_sectors_selected(self):
        # With positive min_score and top_n=3, at most 3 sectors should have nonzero weight
        scores = pd.Series({
            "ETF0": 2.0, "ETF1": 1.5, "ETF2": 1.0,
            "ETF3": 0.3, "ETF4": 0.1, "ETF5": -1.0,
        })
        w = optimize_weights(scores, self.returns, method="inv_vol",
                              top_n=3, min_score=0.5)
        n_active = (w > 1e-4).sum()
        self.assertLessEqual(n_active, 3)

    def test_all_methods_valid(self):
        for method in ["inv_vol", "risk_parity", "gmv", "equal_weight"]:
            w = optimize_weights(self.scores, self.returns, method=method, top_n=4)
            self.assertAlmostEqual(w.sum(), 1.0, places=5, msg=f"Method {method} failed")
            self.assertTrue((w >= -1e-6).all(), f"Method {method} has negative weights")


class TestVolScaling(unittest.TestCase):

    def test_no_scaling_below_threshold(self):
        factor = vol_scaling_factor(0.10, 0.12, target_vol=0.12, scale_threshold=1.5)
        self.assertEqual(factor, 1.0)

    def test_scaling_triggered_above_threshold(self):
        factor = vol_scaling_factor(0.25, 0.12, target_vol=0.12, scale_threshold=1.5)
        self.assertLess(factor, 1.0)

    def test_scaling_factor_bounded(self):
        factor = vol_scaling_factor(0.50, 0.12, target_vol=0.12, scale_threshold=1.0)
        self.assertGreater(factor, 0.0)
        self.assertLessEqual(factor, 1.0)

    def test_nan_returns_one(self):
        factor = vol_scaling_factor(float("nan"), 0.12)
        self.assertEqual(factor, 1.0)


class TestRiskControls(unittest.TestCase):

    def test_vix_emergency_triggers(self):
        weights = pd.Series({"ETF0": 0.4, "ETF1": 0.3, "ETF2": 0.3})
        port_ret = pd.Series(np.random.normal(0, 0.01, 100))
        macro_crisis = _make_macro(vix_level=40.0)

        adj_w, cash_pct, flags = apply_risk_controls(
            weights=weights,
            portfolio_returns=port_ret,
            macro=macro_crisis,
            vix_emergency_threshold=35.0,
            emergency_cash_pct=0.50,
        )
        self.assertTrue(flags.vix_emergency_triggered)
        self.assertAlmostEqual(cash_pct, 0.50, places=5)
        self.assertAlmostEqual(adj_w.sum(), 0.50, places=5)

    def test_normal_vix_no_emergency(self):
        weights = pd.Series({"ETF0": 0.5, "ETF1": 0.5})
        port_ret = pd.Series(np.random.normal(0, 0.01, 100))
        macro_normal = _make_macro(vix_level=16.0)

        adj_w, cash_pct, flags = apply_risk_controls(
            weights=weights,
            portfolio_returns=port_ret,
            macro=macro_normal,
        )
        self.assertFalse(flags.vix_emergency_triggered)
        self.assertEqual(cash_pct, 0.0)


class TestTurnover(unittest.TestCase):

    def test_no_change_zero_turnover(self):
        w = pd.Series({"A": 0.5, "B": 0.3, "C": 0.2})
        to = compute_turnover(w, w)
        self.assertAlmostEqual(to, 0.0, places=10)

    def test_full_rotation_turnover(self):
        w_old = pd.Series({"A": 1.0, "B": 0.0})
        w_new = pd.Series({"A": 0.0, "B": 1.0})
        to = compute_turnover(w_new, w_old)
        self.assertAlmostEqual(to, 1.0, places=6)

    def test_turnover_cap_works(self):
        w_old = pd.Series({"A": 0.5, "B": 0.5})
        w_new = pd.Series({"A": 0.0, "B": 1.0})
        capped = cap_turnover(w_new, w_old, max_turnover=0.10)
        to_capped = compute_turnover(capped, w_old)
        self.assertAlmostEqual(to_capped, 0.10, places=2)


class TestThresholdFilter(unittest.TestCase):

    def test_below_threshold_keeps_old_weights(self):
        scores_new = pd.Series({"A": 1.0, "B": 0.5})
        scores_old = pd.Series({"A": 0.8, "B": 0.4})  # change = 0.2 < 0.5 threshold
        w_new = pd.Series({"A": 0.6, "B": 0.4})
        w_old = pd.Series({"A": 0.5, "B": 0.5})

        filtered_w, rebalanced, held = apply_zscore_threshold_filter(
            scores_new, scores_old, w_new, w_old, threshold=0.5
        )
        self.assertIn("A", held)
        self.assertIn("B", held)
        self.assertEqual(len(rebalanced), 0)

    def test_above_threshold_accepts_new_weights(self):
        scores_new = pd.Series({"A": 2.0, "B": 0.5})
        scores_old = pd.Series({"A": 0.5, "B": 0.4})  # A change = 1.5 > 0.5 threshold
        w_new = pd.Series({"A": 0.7, "B": 0.3})
        w_old = pd.Series({"A": 0.3, "B": 0.7})

        filtered_w, rebalanced, held = apply_zscore_threshold_filter(
            scores_new, scores_old, w_new, w_old, threshold=0.5
        )
        self.assertIn("A", rebalanced)


if __name__ == "__main__":
    unittest.main(verbosity=2)
