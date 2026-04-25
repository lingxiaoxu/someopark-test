"""
Engine Smoke Tests
==================
End-to-end smoke tests for the full backtest engine using synthetic data.
No network calls — all data is generated in-memory.

Exercises the complete pipeline:
    synthetic prices + macro
    → compute_composite_signals
    → SectorETFExchange (qlib Exchange)
    → Account / Position (qlib)
    → SectorRotationWeightStrategy (WeightStrategyBase)
    → SectorSimulatorExecutor (backtest_loop)
    → decompose_portofolio (qlib profit_attribution)
    → indicator_analysis (qlib contrib.evaluate)
    → Account turnover extraction
    → QlibRecorder / MLflowExpManager (qlib.workflow)
    → BacktestResult assembly
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from sector_rotation.backtest.engine import SectorRotationBacktest, BacktestResult


# ---------------------------------------------------------------------------
# Synthetic data helpers
# ---------------------------------------------------------------------------

_TICKERS = ["XLK", "XLF", "XLE", "XLV", "XLU"]
_BENCH = "SPY"
_START = "2020-01-01"
_END   = "2021-06-30"


def _make_prices(seed: int = 0) -> pd.DataFrame:
    """Synthetic daily ETF + benchmark prices."""
    np.random.seed(seed)
    dates = pd.bdate_range(_START, _END)
    n = len(dates)
    tickers = _TICKERS + [_BENCH]
    log_ret = np.random.normal(0.0003, 0.012, size=(n, len(tickers)))
    prices = 100.0 * np.exp(np.cumsum(log_ret, axis=0))
    return pd.DataFrame(prices, index=dates, columns=tickers)


def _make_macro(seed: int = 1) -> pd.DataFrame:
    """Synthetic daily macro indicators."""
    np.random.seed(seed)
    dates = pd.bdate_range(_START, _END)
    n = len(dates)
    return pd.DataFrame(
        {
            "vix":         np.abs(np.random.normal(18.0, 3.0, n)),
            "yield_curve": np.random.normal(0.5,  0.1,  n),
            "hy_spread":   np.abs(np.random.normal(350.0, 30.0, n)),
        },
        index=dates,
    )


def _minimal_config() -> dict:
    """Minimal config that exercises all qlib integration paths."""
    return {
        "backtest": {
            "start_date": _START,
            "end_date":   _END,
            "initial_capital": 500_000.0,
        },
        "universe": {
            "etfs":      _TICKERS,
            "benchmark": _BENCH,
        },
        "signals": {
            "weights": {
                "cs_momentum": 0.4,
                "ts_momentum": 0.15,
                "pe_value":    0.2,
                "regime":      0.25,
                "acceleration": 0.15,
            },
            "regime": {"method": "rules"},
            # Use proxy for unit tests (no yfinance network needed)
            "value_source": "proxy",
        },
        "portfolio": {
            "optimizer":    "inv_vol",
            "top_n_sectors": 3,
            "min_zscore":   -1.0,
            "cov": {"method": "ledoit_wolf", "lookback_days": 120},
            "constraints": {"max_weight": 0.50, "min_weight": 0.0},
        },
        "rebalance": {
            "zscore_change_threshold": 0.3,
            "max_monthly_turnover":    0.90,
            "emergency_derisk_vix":    45.0,
            "emergency_cash_pct":      0.50,
        },
        "risk": {
            "vol_scaling": {"enabled": True, "target_vol_annual": 0.15},
            "drawdown":    {"cumulative_dd_halve": -0.20},
        },
        "costs": {
            "transaction_cost_bps": 5,
            "impact_cost_bps":      0,
            "etf_fee_bps":          9,
        },
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestEngineSmokeNative(unittest.TestCase):
    """Smoke test: native (non-qlib) fallback path."""

    @classmethod
    def setUpClass(cls):
        from sector_rotation.backtest import engine as _eng
        # Force native path by temporarily disabling qlib flag
        cls._orig_flag = _eng._QLIB_BACKTEST_AVAILABLE
        _eng._QLIB_BACKTEST_AVAILABLE = False

        prices = _make_prices()
        macro  = _make_macro()
        cfg    = _minimal_config()

        bt = SectorRotationBacktest(cfg, mlflow_experiment="sector_rotation_test")
        cls.result = bt.run(prices=prices, macro=macro)

        _eng._QLIB_BACKTEST_AVAILABLE = cls._orig_flag

    def test_returns_backtest_result(self):
        self.assertIsInstance(self.result, BacktestResult)

    def test_equity_curve_nonempty(self):
        self.assertGreater(len(self.result.equity_curve), 0)

    def test_equity_starts_near_capital(self):
        first_val = self.result.equity_curve.iloc[0]
        self.assertAlmostEqual(first_val, 500_000.0, delta=50_000.0)

    def test_weights_history_columns(self):
        if not self.result.weights_history.empty:
            for col in self.result.weights_history.columns:
                self.assertIn(col, _TICKERS)

    def test_weights_sum_to_one(self):
        if not self.result.weights_history.empty:
            row_sums = self.result.weights_history.sum(axis=1)
            self.assertTrue((row_sums <= 1.0 + 1e-4).all())

    def test_metrics_keys_present(self):
        for key in ["sharpe", "annual_return", "max_drawdown", "calmar"]:
            self.assertIn(key, self.result.metrics)

    def test_benchmark_equity_present(self):
        self.assertIsNotNone(self.result.benchmark_equity)

    def test_regime_history_nonempty(self):
        self.assertGreater(len(self.result.regime_history), 0)


class TestEngineQlibPath(unittest.TestCase):
    """Smoke test: full qlib infrastructure path (if available)."""

    @classmethod
    def setUpClass(cls):
        from sector_rotation.backtest import engine as _eng
        cls._qlib_available = _eng._QLIB_BACKTEST_AVAILABLE

        prices = _make_prices()
        macro  = _make_macro()
        cfg    = _minimal_config()

        bt = SectorRotationBacktest(cfg, mlflow_experiment="sector_rotation_test")
        try:
            cls.result = bt.run(prices=prices, macro=macro)
            cls._ran_qlib = cls._qlib_available
        except Exception as e:
            cls.result = None
            cls._run_error = str(e)
            cls._ran_qlib = False

    def test_result_exists(self):
        self.assertIsNotNone(self.result)

    def test_equity_curve_positive(self):
        if self.result is None:
            self.skipTest("Engine failed to run")
        self.assertTrue((self.result.equity_curve > 0).all())

    def test_daily_returns_finite(self):
        if self.result is None:
            self.skipTest("Engine failed to run")
        self.assertTrue(np.isfinite(self.result.daily_returns).all())

    def test_qlib_turnover_if_qlib_ran(self):
        if not self._ran_qlib or self.result is None:
            self.skipTest("qlib path not used")
        # qlib_turnover may be None if Account didn't record it, but should not crash
        if self.result.qlib_turnover is not None:
            self.assertIn("total_turnover", self.result.qlib_turnover.columns)

    def test_attribution_structure(self):
        if self.result is None:
            self.skipTest("Engine failed to run")
        attr = self.result.attribution
        if attr is not None:
            self.assertIsInstance(attr, dict)
            self.assertIn("group_weight", attr)
            self.assertIn("group_return", attr)
            gw = attr["group_weight"]
            self.assertIsInstance(gw, pd.DataFrame)
            self.assertFalse(gw.empty)

    def test_trade_indicators_structure(self):
        if self.result is None:
            self.skipTest("Engine failed to run")
        ti = self.result.trade_indicators
        if ti is not None:
            self.assertIsInstance(ti, pd.DataFrame)
            # indicator_analysis returns rows: ffr, pa, pos
            self.assertIn("value", ti.columns)


class TestBacktestResultFields(unittest.TestCase):
    """Verify all BacktestResult fields exist and have correct types."""

    @classmethod
    def setUpClass(cls):
        from sector_rotation.backtest import engine as _eng
        _orig = _eng._QLIB_BACKTEST_AVAILABLE
        _eng._QLIB_BACKTEST_AVAILABLE = False

        prices = _make_prices()
        macro  = _make_macro()
        bt = SectorRotationBacktest(_minimal_config(), mlflow_experiment="sector_rotation_test")
        cls.result = bt.run(prices=prices, macro=macro)
        _eng._QLIB_BACKTEST_AVAILABLE = _orig

    def test_equity_curve_is_series(self):
        self.assertIsInstance(self.result.equity_curve, pd.Series)

    def test_daily_returns_is_series(self):
        self.assertIsInstance(self.result.daily_returns, pd.Series)

    def test_weights_history_is_df(self):
        self.assertIsInstance(self.result.weights_history, pd.DataFrame)

    def test_signals_history_is_df(self):
        self.assertIsInstance(self.result.signals_history, pd.DataFrame)

    def test_regime_history_is_series(self):
        self.assertIsInstance(self.result.regime_history, pd.Series)

    def test_costs_history_is_df(self):
        self.assertIsInstance(self.result.costs_history, pd.DataFrame)

    def test_risk_flags_is_list(self):
        self.assertIsInstance(self.result.risk_flags, list)

    def test_metrics_is_dict(self):
        self.assertIsInstance(self.result.metrics, dict)

    def test_subperiod_metrics_is_df(self):
        self.assertIsInstance(self.result.subperiod_metrics, pd.DataFrame)

    def test_drawdown_episodes_is_df(self):
        self.assertIsInstance(self.result.drawdown_episodes, pd.DataFrame)

    def test_summary_runs_without_error(self):
        s = self.result.summary()
        self.assertIn("SECTOR ROTATION BACKTEST SUMMARY", s)

    def test_attribution_is_dict_or_none(self):
        a = self.result.attribution
        self.assertTrue(a is None or isinstance(a, dict))

    def test_trade_indicators_is_df_or_none(self):
        ti = self.result.trade_indicators
        self.assertTrue(ti is None or isinstance(ti, pd.DataFrame))

    def test_qlib_turnover_is_df_or_none(self):
        qt = self.result.qlib_turnover
        self.assertTrue(qt is None or isinstance(qt, pd.DataFrame))


if __name__ == "__main__":
    unittest.main(verbosity=2)
