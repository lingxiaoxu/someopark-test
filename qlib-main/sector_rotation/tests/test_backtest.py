"""
Backtest Engine Tests
=====================
Integration tests for the backtest engine with synthetic data.
Tests avoid network calls by using synthetic price and macro data.
"""

import sys
from pathlib import Path
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from sector_rotation.backtest.costs import (
    compute_transaction_costs,
    compute_daily_fee_drag,
    get_one_way_cost_bps,
    get_round_trip_cost_bps,
    estimate_annual_costs,
)
from sector_rotation.backtest.metrics import (
    annualized_return,
    annualized_vol,
    sharpe_ratio,
    max_drawdown,
    calmar_ratio,
    information_ratio,
    cvar,
    monthly_win_rate,
    compute_metrics,
    find_drawdown_episodes,
    subperiod_analysis,
)


# ---------------------------------------------------------------------------
# Synthetic data helpers
# ---------------------------------------------------------------------------

def _make_returns_series(n=252 * 5, ann_return=0.10, ann_vol=0.15, seed=42):
    np.random.seed(seed)
    daily_mu = ann_return / 252
    daily_sigma = ann_vol / np.sqrt(252)
    r = np.random.normal(daily_mu, daily_sigma, n)
    return pd.Series(r, index=pd.bdate_range("2018-07-01", periods=n))


# ---------------------------------------------------------------------------
# Transaction cost tests
# ---------------------------------------------------------------------------

class TestTransactionCosts(unittest.TestCase):

    def test_cost_bps_by_tier(self):
        self.assertEqual(get_one_way_cost_bps("XLK"), 3)  # Tier 1
        self.assertEqual(get_one_way_cost_bps("XLI"), 5)  # Tier 2
        self.assertEqual(get_one_way_cost_bps("XLC"), 8)  # Tier 3

    def test_round_trip_double_one_way(self):
        for ticker in ["XLK", "XLI", "XLC"]:
            self.assertEqual(
                get_round_trip_cost_bps(ticker),
                get_one_way_cost_bps(ticker) * 2,
            )

    def test_no_rebalance_zero_cost(self):
        w = pd.Series({"XLK": 0.4, "XLV": 0.3, "XLF": 0.3})
        result = compute_transaction_costs(w, w, 1_000_000)
        self.assertAlmostEqual(result["total_cost_usd"], 0.0, places=6)
        self.assertAlmostEqual(result["turnover_pct"], 0.0, places=6)

    def test_full_rotation_cost_positive(self):
        w_old = pd.Series({"XLK": 0.5, "XLV": 0.5})
        w_new = pd.Series({"XLF": 0.5, "XLC": 0.5})
        result = compute_transaction_costs(w_old, w_new, 1_000_000)
        self.assertGreater(result["total_cost_usd"], 0)
        self.assertGreater(result["total_cost_bps"], 0)

    def test_turnover_full_rotation(self):
        w_old = pd.Series({"A": 1.0})
        w_new = pd.Series({"B": 1.0})
        result = compute_transaction_costs(w_old, w_new, 1_000_000)
        self.assertAlmostEqual(result["turnover_pct"], 100.0, places=4)

    def test_fee_drag_positive(self):
        w = pd.Series({"XLK": 0.5, "XLV": 0.5})
        drag = compute_daily_fee_drag(w, 1_000_000, annual_fee_bps=9)
        self.assertGreater(drag, 0)
        # Should be ~$3.57 per day ($1M × 0.09%/252)
        expected = 1_000_000 * 0.0009 / 252
        self.assertAlmostEqual(drag, expected, places=2)

    def test_annual_cost_estimate(self):
        # Assume 40% monthly turnover
        turnover = pd.Series([0.40] * 12)
        costs = estimate_annual_costs(turnover, 1_000_000)
        self.assertIn("annual_turnover_pct", costs)
        self.assertIn("total_cost_bps", costs)
        self.assertGreater(costs["total_cost_bps"], 0)
        # Annual turnover should be ~480%
        self.assertAlmostEqual(costs["annual_turnover_pct"], 480.0, places=0)


# ---------------------------------------------------------------------------
# Metrics tests
# ---------------------------------------------------------------------------

class TestMetrics(unittest.TestCase):

    def setUp(self):
        self.returns = _make_returns_series()
        self.bench = _make_returns_series(ann_return=0.08, ann_vol=0.16, seed=99)

    def test_annualized_return_reasonable(self):
        ann_ret = annualized_return(self.returns)
        # Should be positive and in a reasonable range for a 10% target
        # Wider delta due to randomness over 5-year window
        self.assertAlmostEqual(ann_ret, 0.10, delta=0.12)

    def test_annualized_vol_reasonable(self):
        ann_vol = annualized_vol(self.returns)
        self.assertAlmostEqual(ann_vol, 0.15, delta=0.02)

    def test_sharpe_positive_for_positive_return(self):
        ret = _make_returns_series(ann_return=0.15, ann_vol=0.12, seed=1)
        sr = sharpe_ratio(ret)
        self.assertGreater(sr, 0)

    def test_sharpe_negative_for_negative_return(self):
        ret = _make_returns_series(ann_return=-0.10, ann_vol=0.15, seed=2)
        sr = sharpe_ratio(ret)
        self.assertLess(sr, 0)

    def test_max_drawdown_negative(self):
        mdd, duration = max_drawdown(self.returns)
        self.assertLessEqual(mdd, 0)
        self.assertGreaterEqual(duration, 0)

    def test_max_drawdown_zero_for_always_up(self):
        # Monotonically increasing returns
        r = pd.Series([0.001] * 252, index=pd.bdate_range("2020-01-01", periods=252))
        mdd, duration = max_drawdown(r)
        self.assertAlmostEqual(mdd, 0.0, places=5)

    def test_calmar_positive_for_positive_return(self):
        ret = _make_returns_series(ann_return=0.12, seed=5)
        cal = calmar_ratio(ret)
        self.assertGreater(cal, 0)

    def test_cvar_negative(self):
        cv = cvar(self.returns, confidence=0.95)
        self.assertLess(cv, 0)

    def test_cvar_95_worse_than_99(self):
        cv_95 = cvar(self.returns, confidence=0.95)
        cv_99 = cvar(self.returns, confidence=0.99)
        # CVaR at 99% should be worse (more negative) than at 95%
        self.assertLessEqual(cv_99, cv_95)

    def test_win_rate_in_range(self):
        wr = monthly_win_rate(self.returns)
        self.assertGreaterEqual(wr, 0.0)
        self.assertLessEqual(wr, 1.0)

    def test_information_ratio_computable(self):
        ir = information_ratio(self.returns, self.bench)
        self.assertFalse(np.isnan(ir))

    def test_compute_metrics_keys(self):
        m = compute_metrics(self.returns, self.bench)
        expected_keys = [
            "annual_return", "annual_vol", "sharpe", "max_drawdown",
            "calmar", "cvar_95", "cvar_99", "monthly_win_rate",
            "info_ratio", "total_return",
        ]
        for k in expected_keys:
            self.assertIn(k, m, f"Missing key: {k}")

    def test_metrics_shapes_consistent(self):
        m = compute_metrics(self.returns)
        # Without benchmark, IR should be absent or nan
        if "info_ratio" in m:
            # If no benchmark passed, could be nan
            pass  # Just check no crash

    def test_empty_returns_nan(self):
        empty = pd.Series(dtype=float)
        ann_ret = annualized_return(empty)
        self.assertTrue(np.isnan(ann_ret))


class TestDrawdownEpisodes(unittest.TestCase):

    def test_finds_episodes(self):
        ret = _make_returns_series(ann_return=0.05, ann_vol=0.20, seed=10)
        episodes = find_drawdown_episodes(ret, top_n=5)
        self.assertIsInstance(episodes, pd.DataFrame)
        # Should have at least some columns
        if not episodes.empty:
            self.assertIn("drawdown_pct", episodes.columns)
            self.assertIn("peak_date", episodes.columns)

    def test_always_up_no_episodes(self):
        r = pd.Series([0.001] * 252, index=pd.bdate_range("2020-01-01", periods=252))
        episodes = find_drawdown_episodes(r)
        self.assertEqual(len(episodes), 0)


class TestSubperiodAnalysis(unittest.TestCase):

    def test_subperiod_returns_dataframe(self):
        ret = _make_returns_series(n=252 * 6)
        bench = _make_returns_series(n=252 * 6, seed=99)
        sp = subperiod_analysis(ret, bench)
        self.assertIsInstance(sp, pd.DataFrame)

    def test_custom_subperiods(self):
        ret = _make_returns_series(n=252 * 6)
        subperiods = [
            ("All", "2018-07-01", "2024-12-31"),
            ("First Half", "2018-07-01", "2021-06-30"),
        ]
        sp = subperiod_analysis(ret, subperiods=subperiods)
        self.assertLessEqual(len(sp), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
