"""
Signal Tests
============
Unit tests for momentum, value, regime, and composite signal computation.
"""

import sys
from pathlib import Path
import unittest

import numpy as np
import pandas as pd

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from sector_rotation.signals.momentum import (
    compute_cs_momentum,
    compute_ts_momentum,
    compute_acceleration,
    _cumulative_return,
)
from sector_rotation.signals.value import (
    compute_value_signal,
    pe_to_percentile,
    build_pe_proxy_series,
)
from sector_rotation.signals.regime import (
    compute_regime_rules,
    regime_to_monthly,
    normalize_macro,
    RISK_ON, RISK_OFF, TRANSITION_UP, TRANSITION_DOWN,
)


def _make_price_data(n_months=60, n_tickers=5, seed=42):
    """Generate synthetic monthly price data."""
    np.random.seed(seed)
    rng = pd.date_range("2018-07-01", periods=n_months * 21, freq="B")
    tickers = [f"ETF{i}" for i in range(n_tickers)]
    returns = np.random.normal(0.0004, 0.01, size=(len(rng), n_tickers))
    prices = pd.DataFrame(
        100 * np.cumprod(1 + returns, axis=0),
        index=rng,
        columns=tickers,
    )
    return prices


def _make_macro_data(n_days=1000, seed=99):
    """Generate synthetic daily macro data."""
    np.random.seed(seed)
    idx = pd.bdate_range("2018-07-01", periods=n_days)
    macro = pd.DataFrame(
        {
            "vix": np.abs(np.random.normal(18, 5, n_days)) + 10,
            "yield_curve": np.random.normal(0.5, 0.5, n_days),
            "hy_spread": np.abs(np.random.normal(350, 100, n_days)),
            "ism_mfg": np.random.normal(52, 3, n_days),
            "breakeven_10y": np.random.normal(2.2, 0.4, n_days),
            "fed_rate": np.abs(np.random.normal(2.5, 1.5, n_days)),
        },
        index=idx,
    )
    return macro


class TestCumulativeReturn(unittest.TestCase):
    """Tests for _cumulative_return helper."""

    def test_basic_return(self):
        prices = _make_price_data(n_months=24)
        monthly = prices.resample("ME").last()
        ret = monthly.pct_change()
        cr = _cumulative_return(ret, start_lag=1, end_lag=12)
        self.assertEqual(cr.shape, ret.shape)

    def test_nan_for_insufficient_history(self):
        prices = _make_price_data(n_months=24)
        monthly = prices.resample("ME").last()
        ret = monthly.pct_change()
        cr = _cumulative_return(ret, start_lag=1, end_lag=12)
        # First 12 rows should be NaN (insufficient history)
        self.assertTrue(cr.iloc[:11].isna().all().all())

    def test_start_lag_zero_raises(self):
        with self.assertRaises(AssertionError):
            _cumulative_return(pd.DataFrame([[1, 2]]), start_lag=5, end_lag=3)


class TestCsMomentum(unittest.TestCase):

    def setUp(self):
        self.prices = _make_price_data(n_months=48)

    def test_output_shape(self):
        sig = compute_cs_momentum(self.prices)
        # Should have monthly index (end-of-month)
        self.assertIsInstance(sig.index, pd.DatetimeIndex)
        self.assertEqual(set(sig.columns), set(self.prices.columns))

    def test_zscore_roughly_standard(self):
        sig = compute_cs_momentum(self.prices, zscore_window=0)
        valid = sig.dropna(how="all")
        if len(valid) > 5:
            # Cross-sectional mean should be ~0 for each row
            row_means = valid.mean(axis=1)
            np.testing.assert_allclose(row_means.mean(), 0.0, atol=0.5)

    def test_no_lookahead(self):
        # If we add one more month of data, existing signals should not change
        prices_short = self.prices.iloc[:-21]
        sig_short = compute_cs_momentum(prices_short)
        sig_full = compute_cs_momentum(self.prices)
        common_idx = sig_short.index.intersection(sig_full.index)
        if len(common_idx) > 2:
            pd.testing.assert_frame_equal(
                sig_short.loc[common_idx].round(6),
                sig_full.loc[common_idx].round(6),
                check_names=False,
            )

    def test_lookback_parameter(self):
        sig_12 = compute_cs_momentum(self.prices, lookback_months=12, zscore_window=0)
        sig_6 = compute_cs_momentum(self.prices, lookback_months=6, zscore_window=0)
        # Different lookbacks should produce different signals
        valid_both = sig_12.dropna(how="all").index.intersection(sig_6.dropna(how="all").index)
        if len(valid_both) > 3:
            diff = (sig_12.loc[valid_both] - sig_6.loc[valid_both]).abs().mean().mean()
            self.assertGreater(diff, 0.001)


class TestTsMomentum(unittest.TestCase):

    def setUp(self):
        self.prices = _make_price_data(n_months=36)

    def test_multiplier_values(self):
        mult = compute_ts_momentum(self.prices, crash_filter_multiplier=0.0)
        valid = mult.dropna(how="all")
        # All values should be 0.0 or 1.0
        all_vals = valid.values.flatten()
        all_vals = all_vals[~np.isnan(all_vals)]
        for v in all_vals:
            self.assertIn(v, [0.0, 1.0])

    def test_crash_filter_multiplier(self):
        mult_05 = compute_ts_momentum(self.prices, crash_filter_multiplier=0.5)
        valid = mult_05.dropna(how="all")
        all_vals = valid.values.flatten()
        all_vals = all_vals[~np.isnan(all_vals)]
        for v in all_vals:
            self.assertIn(round(v, 2), [0.5, 1.0])


class TestAcceleration(unittest.TestCase):

    def test_output(self):
        prices = _make_price_data(n_months=36)
        accel = compute_acceleration(prices, short_months=3, long_months=12)
        self.assertIsNotNone(accel)
        valid = accel.dropna(how="all")
        self.assertGreater(len(valid), 0)


class TestPePercentile(unittest.TestCase):

    def test_percentile_range(self):
        pe = pd.Series(
            np.linspace(10, 30, 150),
            index=pd.date_range("2010-01-01", periods=150, freq="ME"),
        )
        pct = pe_to_percentile(pe, lookback_years=5, window_min_periods=12)
        valid = pct.dropna()
        self.assertTrue((valid >= 0).all() and (valid <= 1).all())

    def test_monotone(self):
        # Higher P/E should → higher percentile (more expensive)
        pe = pd.Series(
            np.concatenate([np.linspace(15, 25, 120), [30, 10]]),
            index=pd.date_range("2010-01-01", periods=122, freq="ME"),
        )
        pct = pe_to_percentile(pe, lookback_years=5)
        # Last value (10 = cheapest) should have lower percentile than second-to-last (30 = expensive)
        valid = pct.dropna()
        if len(valid) >= 2:
            self.assertGreater(valid.iloc[-2], valid.iloc[-1])


class TestRegimeRules(unittest.TestCase):

    def setUp(self):
        self.macro = _make_macro_data(n_days=500)

    def test_output_is_series(self):
        regime = compute_regime_rules(self.macro)
        self.assertIsInstance(regime, pd.Series)
        self.assertEqual(len(regime), len(self.macro))

    def test_valid_states(self):
        regime = compute_regime_rules(self.macro)
        valid_states = {RISK_ON, RISK_OFF, TRANSITION_UP, TRANSITION_DOWN}
        unique = set(regime.unique())
        self.assertTrue(unique.issubset(valid_states))

    def test_high_vix_triggers_risk_off(self):
        macro_crisis = self.macro.copy()
        macro_crisis["vix"] = 50.0  # Extreme VIX
        macro_crisis["hy_spread"] = 800.0
        regime = compute_regime_rules(macro_crisis, smoothing_days=1)
        # Most days should be RISK_OFF
        self.assertGreater((regime == RISK_OFF).mean(), 0.5)

    def test_low_vix_triggers_risk_on(self):
        macro_calm = self.macro.copy()
        macro_calm["vix"] = 12.0   # Very calm
        macro_calm["yield_curve"] = 1.0
        macro_calm["hy_spread"] = 200.0
        macro_calm["ism_mfg"] = 56.0
        regime = compute_regime_rules(macro_calm, smoothing_days=1)
        self.assertGreater((regime == RISK_ON).mean(), 0.5)

    def test_monthly_regime(self):
        regime_daily = compute_regime_rules(self.macro)
        regime_monthly = regime_to_monthly(regime_daily)
        self.assertIsInstance(regime_monthly, pd.Series)
        self.assertLessEqual(len(regime_monthly), len(self.macro))


class TestNormalizeMacro(unittest.TestCase):

    def test_z_scores_shape(self):
        macro = _make_macro_data(n_days=500)
        z = normalize_macro(macro)
        self.assertEqual(z.shape, macro.shape)

    def test_near_zero_mean(self):
        macro = _make_macro_data(n_days=600)
        z = normalize_macro(macro, rolling_window=252)
        valid = z.dropna(how="all")
        if len(valid) > 50:
            col_means = valid.mean()
            for col, mean in col_means.items():
                self.assertLess(abs(mean), 2.0, f"Column {col} has |mean|={abs(mean):.2f}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
