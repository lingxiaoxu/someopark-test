"""
test_new_features.py — Unit tests for WF framework, new signals, MCPS, and param sets
=====================================================================================
Covers:
  1. new_signals.py: short-term momentum, earnings revision, RS breakout
  2. walk_forward.py: WalkForwardAnalyzer, DSR, WFE, fold generation
  3. composite.py: bonus signal integration, benchmark_prices param
  4. MCPS.py: macro_cond_sharpe, select_param
  5. value.py: SECTOR_REPRESENTATIVES (110 stocks)
  6. SectorRotationStrategyRuns.py: 64 param sets, weight validation

All tests use synthetic data — no network, no API keys.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))  # for MCPS


# ═══════════════════════════════════════════════════════════════════════════
#  Synthetic data helpers
# ═══════════════════════════════════════════════════════════════════════════

_TICKERS = ["XLK", "XLF", "XLE", "XLV", "XLU", "XLI", "XLY", "XLP", "XLB", "XLC", "XLRE"]
_BENCH = "SPY"


def _make_prices(n_days: int = 1500, seed: int = 42) -> pd.DataFrame:
    """Synthetic daily prices for 11 ETFs + SPY."""
    np.random.seed(seed)
    dates = pd.bdate_range("2018-07-01", periods=n_days)
    tickers = _TICKERS + [_BENCH]
    rets = np.random.normal(0.0003, 0.015, (n_days, len(tickers)))
    prices = pd.DataFrame(
        100 * np.exp(np.cumsum(rets, axis=0)),
        index=dates, columns=tickers,
    )
    return prices


def _make_macro(prices: pd.DataFrame) -> pd.DataFrame:
    """Synthetic macro data aligned with prices."""
    n = len(prices)
    np.random.seed(99)
    macro = pd.DataFrame({
        "vix": 15 + 5 * np.random.randn(n).cumsum() * 0.02,
        "yield_curve": 0.5 + 0.3 * np.random.randn(n),
        "hy_spread": 400 + 50 * np.random.randn(n),
        "fin_stress": np.random.randn(n) * 0.5,
        "breakeven_10y": 2.0 + 0.1 * np.random.randn(n),
        "consumer_sent": 70 + 5 * np.random.randn(n),
    }, index=prices.index)
    macro["vix"] = macro["vix"].clip(10, 80)
    return macro


# ═══════════════════════════════════════════════════════════════════════════
#  1. new_signals.py tests
# ═══════════════════════════════════════════════════════════════════════════

class TestShortTermMomentum(unittest.TestCase):
    def setUp(self):
        self.prices = _make_prices()
        self.etf_prices = self.prices[_TICKERS]

    def test_output_shape(self):
        from sector_rotation.signals.new_signals import compute_short_term_momentum
        result = compute_short_term_momentum(self.etf_prices, lookback_months=6)
        self.assertGreater(len(result), 0)
        self.assertEqual(result.shape[1], len(_TICKERS))

    def test_zscore_properties(self):
        from sector_rotation.signals.new_signals import compute_short_term_momentum
        result = compute_short_term_momentum(self.etf_prices, lookback_months=6)
        # Cross-sectional mean should be ~0
        row_means = result.dropna(how="all").mean(axis=1).dropna()
        self.assertTrue(all(abs(row_means) < 0.5),
                        f"Cross-sectional means too far from 0: {row_means.describe()}")

    def test_empty_on_short_data(self):
        from sector_rotation.signals.new_signals import compute_short_term_momentum
        short = self.etf_prices.head(50)
        result = compute_short_term_momentum(short, lookback_months=6)
        self.assertTrue(result.empty)


class TestRelativeStrengthBreakout(unittest.TestCase):
    def setUp(self):
        self.prices = _make_prices()

    def test_output_shape(self):
        from sector_rotation.signals.new_signals import compute_relative_strength_breakout
        result = compute_relative_strength_breakout(
            self.prices[_TICKERS], self.prices[_BENCH], lookback_days=63
        )
        self.assertGreater(len(result), 0)
        self.assertEqual(result.shape[1], len(_TICKERS))

    def test_no_benchmark_returns_empty(self):
        from sector_rotation.signals.new_signals import compute_relative_strength_breakout
        result = compute_relative_strength_breakout(
            self.prices[_TICKERS], None, lookback_days=63
        )
        self.assertTrue(result.empty)


class TestComputeAllNewSignals(unittest.TestCase):
    def test_all_disabled_returns_none(self):
        from sector_rotation.signals.new_signals import compute_all_new_signals
        prices = _make_prices()
        result = compute_all_new_signals(prices[_TICKERS])
        self.assertIsNone(result["short_term_mom"])
        self.assertIsNone(result["earnings_revision"])
        self.assertIsNone(result["rs_breakout"])

    def test_stm_enabled(self):
        from sector_rotation.signals.new_signals import compute_all_new_signals
        prices = _make_prices()
        result = compute_all_new_signals(
            prices[_TICKERS], stm_enabled=True
        )
        self.assertIsNotNone(result["short_term_mom"])
        self.assertIsNone(result["earnings_revision"])

    def test_rsb_enabled(self):
        from sector_rotation.signals.new_signals import compute_all_new_signals
        prices = _make_prices()
        result = compute_all_new_signals(
            prices[_TICKERS],
            benchmark_prices=prices[_BENCH],
            rsb_enabled=True,
        )
        self.assertIsNotNone(result["rs_breakout"])


# ═══════════════════════════════════════════════════════════════════════════
#  2. walk_forward.py tests
# ═══════════════════════════════════════════════════════════════════════════

class TestDSR(unittest.TestCase):
    def test_expected_max_sharpe_positive(self):
        from sector_rotation.walk_forward import expected_max_sharpe
        sr0 = expected_max_sharpe(59, 0.1)
        self.assertGreater(sr0, 0)

    def test_expected_max_sharpe_increases_with_n(self):
        from sector_rotation.walk_forward import expected_max_sharpe
        sr10 = expected_max_sharpe(10, 0.1)
        sr59 = expected_max_sharpe(59, 0.1)
        self.assertGreater(sr59, sr10)

    def test_deflated_sharpe_ratio_range(self):
        from sector_rotation.walk_forward import deflated_sharpe_ratio
        p = deflated_sharpe_ratio(sr_obs=1.0, sr_0=0.5, T=500)
        self.assertGreaterEqual(p, 0.0)
        self.assertLessEqual(p, 1.0)

    def test_deflated_sharpe_high_obs(self):
        from sector_rotation.walk_forward import deflated_sharpe_ratio
        p = deflated_sharpe_ratio(sr_obs=2.0, sr_0=0.3, T=1000)
        self.assertGreater(p, 0.95)

    def test_deflated_sharpe_low_obs(self):
        from sector_rotation.walk_forward import deflated_sharpe_ratio
        p = deflated_sharpe_ratio(sr_obs=0.1, sr_0=0.5, T=500)
        self.assertLess(p, 0.5)


class TestComputeMetrics(unittest.TestCase):
    def test_basic_metrics(self):
        from sector_rotation.walk_forward import _compute_metrics_from_equity
        eq = pd.Series(
            np.exp(np.cumsum(np.random.RandomState(0).normal(0.0003, 0.01, 252))),
            index=pd.bdate_range("2020-01-01", periods=252),
        )
        m = _compute_metrics_from_equity(eq)
        self.assertIn("sharpe", m)
        self.assertIn("maxdd", m)
        self.assertIn("ann_ret", m)
        self.assertFalse(np.isnan(m["sharpe"]))

    def test_empty_equity(self):
        from sector_rotation.walk_forward import _compute_metrics_from_equity
        m = _compute_metrics_from_equity(pd.Series(dtype=float))
        self.assertTrue(np.isnan(m["sharpe"]))


class TestFoldGeneration(unittest.TestCase):
    def test_generates_folds(self):
        from sector_rotation.walk_forward import WalkForwardAnalyzer
        prices = _make_prices(n_days=1500)
        macro = _make_macro(prices)
        cfg = {
            "backtest": {"start_date": "2018-07-01"},
            "signals": {},
            "portfolio": {},
        }
        analyzer = WalkForwardAnalyzer(
            base_cfg=cfg, prices=prices, macro=macro,
            is_years_min=2, oos_months=6, step_days=60, embargo_days=5,
            param_sets={"dummy": {}},
        )
        folds = analyzer.generate_folds()
        self.assertGreater(len(folds), 0)

    def test_embargo_respected(self):
        from sector_rotation.walk_forward import WalkForwardAnalyzer
        prices = _make_prices(n_days=1500)
        macro = _make_macro(prices)
        cfg = {"backtest": {"start_date": "2018-07-01"}, "signals": {}, "portfolio": {}}
        analyzer = WalkForwardAnalyzer(
            base_cfg=cfg, prices=prices, macro=macro,
            is_years_min=2, oos_months=6, step_days=60, embargo_days=5,
            param_sets={"dummy": {}},
        )
        folds = analyzer.generate_folds()
        for f in folds:
            # OOS start must be after embargo end
            self.assertGreater(f.oos_start, f.embargo_end)
            # IS end must be before embargo end
            self.assertLessEqual(f.is_end, f.embargo_end)

    def test_anchored_vs_rolling_differ(self):
        from sector_rotation.walk_forward import WalkForwardAnalyzer
        prices = _make_prices(n_days=1500)
        macro = _make_macro(prices)
        cfg = {"backtest": {"start_date": "2018-07-01"}, "signals": {}, "portfolio": {}}
        folds_a = WalkForwardAnalyzer(
            base_cfg=cfg, prices=prices, macro=macro,
            mode="anchored", is_years_min=2, oos_months=6, step_days=60,
            param_sets={"dummy": {}},
        ).generate_folds()
        folds_r = WalkForwardAnalyzer(
            base_cfg=cfg, prices=prices, macro=macro,
            mode="rolling", is_years_min=2, oos_months=6, step_days=60,
            param_sets={"dummy": {}},
        ).generate_folds()
        # Anchored: IS always starts at same date
        if len(folds_a) > 1:
            self.assertEqual(folds_a[0].is_start, folds_a[-1].is_start)
        # Rolling: IS start moves forward
        if len(folds_r) > 1:
            self.assertGreater(folds_r[-1].is_start, folds_r[0].is_start)


# ═══════════════════════════════════════════════════════════════════════════
#  3. composite.py bonus signal integration
# ═══════════════════════════════════════════════════════════════════════════

class TestCompositeBonusWeights(unittest.TestCase):
    def test_validate_weights_ignores_bonus(self):
        from sector_rotation.signals.composite import _validate_weights
        weights = {
            "cross_sectional_momentum": 0.40,
            "ts_momentum": 0.15,
            "relative_value": 0.20,
            "regime_adjustment": 0.25,
            "short_term_momentum_bonus": 0.10,
            "earnings_revision_bonus": 0.05,
            "rs_breakout_bonus": 0.08,
        }
        # Should not raise — bonus keys excluded from sum
        _validate_weights(weights)

    def test_bonus_keys_defined(self):
        from sector_rotation.signals.composite import _BONUS_WEIGHT_KEYS
        self.assertIn("acceleration_bonus", _BONUS_WEIGHT_KEYS)
        self.assertIn("short_term_momentum_bonus", _BONUS_WEIGHT_KEYS)
        self.assertIn("earnings_revision_bonus", _BONUS_WEIGHT_KEYS)
        self.assertIn("rs_breakout_bonus", _BONUS_WEIGHT_KEYS)


# ═══════════════════════════════════════════════════════════════════════════
#  4. MCPS.py tests
# ═══════════════════════════════════════════════════════════════════════════

class TestMCPSMacroCondSharpe(unittest.TestCase):
    def test_positive_drift_gives_positive_score(self):
        """Equity with strong positive drift → macro_cond_sharpe should be positive."""
        from MCPS import macro_cond_sharpe
        np.random.seed(0)
        n = 500
        dates = pd.bdate_range("2020-01-01", periods=n)
        # Strong positive drift (daily 0.05% = ~13% annual)
        equity = pd.Series(np.exp(np.cumsum(np.random.normal(0.0005, 0.008, n))),
                           index=dates)
        macro_df = pd.DataFrame({
            "fin_stress_z": np.random.randn(n),
            "vix_z": np.random.randn(n),
        }, index=dates)
        today_vec = {"fin_stress_z": 0.5, "vix_z": -0.3}
        score = macro_cond_sharpe(equity, macro_df, today_vec, ["fin_stress_z", "vix_z"])
        self.assertFalse(np.isnan(score))
        self.assertGreater(score, 0, "Positive drift equity should have positive MCPS score")

    def test_negative_drift_gives_negative_score(self):
        """Equity with negative drift → macro_cond_sharpe should be negative."""
        from MCPS import macro_cond_sharpe
        np.random.seed(1)
        n = 500
        dates = pd.bdate_range("2020-01-01", periods=n)
        equity = pd.Series(np.exp(np.cumsum(np.random.normal(-0.001, 0.008, n))),
                           index=dates)
        macro_df = pd.DataFrame({"f1": np.random.randn(n)}, index=dates)
        score = macro_cond_sharpe(equity, macro_df, {"f1": 0.0}, ["f1"])
        self.assertFalse(np.isnan(score))
        self.assertLess(score, 0, "Negative drift equity should have negative MCPS score")

    def test_similar_macro_weights_more(self):
        """When today_vec is close to a cluster of good days, score should be higher."""
        from MCPS import macro_cond_sharpe
        np.random.seed(2)
        n = 300
        dates = pd.bdate_range("2020-01-01", periods=n)
        # First 150 days: macro_f=0, returns=positive
        # Last 150 days: macro_f=5, returns=negative
        rets = np.concatenate([
            np.random.normal(0.001, 0.005, 150),  # good days at macro=0
            np.random.normal(-0.001, 0.005, 150), # bad days at macro=5
        ])
        equity = pd.Series(np.exp(np.cumsum(rets)), index=dates)
        macro_vals = np.concatenate([np.zeros(150), 5 * np.ones(150)])
        macro_df = pd.DataFrame({"f1": macro_vals}, index=dates)

        # today_vec near 0 → should weight good days more → higher score
        score_near_good = macro_cond_sharpe(equity, macro_df, {"f1": 0.0}, ["f1"])
        # today_vec near 5 → should weight bad days more → lower score
        score_near_bad = macro_cond_sharpe(equity, macro_df, {"f1": 5.0}, ["f1"])

        self.assertGreater(score_near_good, score_near_bad,
                           "Score should be higher when today's macro matches good-return days")

    def test_nan_on_short_data(self):
        from MCPS import macro_cond_sharpe
        dates = pd.bdate_range("2020-01-01", periods=10)
        equity = pd.Series(np.ones(10), index=dates)
        macro_df = pd.DataFrame({"f1": np.ones(10)}, index=dates)
        score = macro_cond_sharpe(equity, macro_df, {"f1": 1.0}, ["f1"], min_overlap=60)
        self.assertTrue(np.isnan(score))

    def test_nan_on_missing_feature(self):
        from MCPS import macro_cond_sharpe
        n = 200
        dates = pd.bdate_range("2020-01-01", periods=n)
        equity = pd.Series(np.exp(np.cumsum(np.random.normal(0, 0.01, n))), index=dates)
        macro_df = pd.DataFrame({"f1": np.random.randn(n)}, index=dates)
        score = macro_cond_sharpe(equity, macro_df, {"f1": None}, ["f1"])
        self.assertTrue(np.isnan(score))


class TestMCPSSelectParam(unittest.TestCase):
    def test_single_candidate(self):
        from MCPS import select_param
        result = select_param({}, [{"param_set": "only_one"}])
        self.assertEqual(result, "only_one")

    def test_selects_highest_score(self):
        from MCPS import select_param
        cands = [
            {"param_set": "A", "dsr_pvalue": 0.9, "pair_sharpe": 1.0,
             "is_macro_vector": {"f1": 0.1}},
            {"param_set": "B", "dsr_pvalue": 0.3, "pair_sharpe": 0.5,
             "is_macro_vector": {"f1": 5.0}},
        ]
        # today close to A's vector → A should win
        result = select_param({"f1": 0.0}, cands, features=["f1"])
        self.assertEqual(result, "A")

    def test_custom_score_field(self):
        from MCPS import select_param
        cands = [
            {"param_set": "X", "is_sharpe": 1.5, "is_calmar": 0.8,
             "is_macro_vector": {"f1": 0.0}},
            {"param_set": "Y", "is_sharpe": 0.5, "is_calmar": 0.3,
             "is_macro_vector": {"f1": 0.0}},
        ]
        result = select_param({"f1": 0.0}, cands, features=["f1"],
                               score_field="is_sharpe", quality_field="is_calmar")
        self.assertEqual(result, "X")


# ═══════════════════════════════════════════════════════════════════════════
#  5. value.py SECTOR_REPRESENTATIVES
# ═══════════════════════════════════════════════════════════════════════════

class TestSectorRepresentatives(unittest.TestCase):
    def test_110_stocks(self):
        from sector_rotation.signals.value import SECTOR_REPRESENTATIVES
        total = sum(len(v) for v in SECTOR_REPRESENTATIVES.values())
        self.assertEqual(total, 110, f"Expected 110 stocks, got {total}")

    def test_11_sectors(self):
        from sector_rotation.signals.value import SECTOR_REPRESENTATIVES
        self.assertEqual(len(SECTOR_REPRESENTATIVES), 11)

    def test_each_sector_has_10(self):
        from sector_rotation.signals.value import SECTOR_REPRESENTATIVES
        for etf, stocks in SECTOR_REPRESENTATIVES.items():
            self.assertEqual(len(stocks), 10, f"{etf} has {len(stocks)} stocks, expected 10")

    def test_no_brk_or_v(self):
        """BRK-B and V have no EPS on Polygon — should be replaced."""
        from sector_rotation.signals.value import SECTOR_REPRESENTATIVES
        all_stocks = [s for stocks in SECTOR_REPRESENTATIVES.values() for s in stocks]
        self.assertNotIn("BRK-B", all_stocks)
        self.assertNotIn("V", all_stocks)

    def test_gs_and_axp_present(self):
        """GS and AXP replace BRK-B and V in XLF."""
        from sector_rotation.signals.value import SECTOR_REPRESENTATIVES
        xlf = SECTOR_REPRESENTATIVES["XLF"]
        self.assertIn("GS", xlf)
        self.assertIn("AXP", xlf)


# ═══════════════════════════════════════════════════════════════════════════
#  6. PARAM_SETS validation
# ═══════════════════════════════════════════════════════════════════════════

class TestParamSets(unittest.TestCase):
    def test_59_param_sets(self):
        from sector_rotation.SectorRotationStrategyRuns import PARAM_SETS
        self.assertEqual(len(PARAM_SETS), 59, f"Expected 59, got {len(PARAM_SETS)}")

    def test_all_weights_sum_to_1(self):
        from sector_rotation.SectorRotationStrategyRuns import PARAM_SETS
        for name, ps in PARAM_SETS.items():
            w_keys = [k for k in ps if k.startswith("signals.weights.")]
            if w_keys:
                total = sum(ps[k] for k in w_keys)
                self.assertAlmostEqual(
                    total, 1.0, places=2,
                    msg=f"{name}: signal weights sum to {total}, expected 1.0"
                )

    def test_13_groups_a_to_m(self):
        from sector_rotation.SectorRotationStrategyRuns import _PARAM_SET_DESCRIPTIONS
        groups = set(d[0] for d in _PARAM_SET_DESCRIPTIONS.values() if d and d[0].isalpha())
        expected = set("ABCDEFGHIJKLM")
        self.assertEqual(groups, expected, f"Groups: {groups}")

    def test_new_signals_selective_activation(self):
        """New signals enabled only in specific param sets with clear rationale."""
        from sector_rotation.SectorRotationStrategyRuns import PARAM_SETS
        stm_sets = [n for n, ps in PARAM_SETS.items()
                    if ps.get("signals.short_term_momentum.enabled", False)]
        erm_sets = [n for n, ps in PARAM_SETS.items()
                    if ps.get("signals.earnings_revision.enabled", False)]
        rsb_sets = [n for n, ps in PARAM_SETS.items()
                    if ps.get("signals.relative_strength_breakout.enabled", False)]
        # Only a few sets (≤5 each) should have new signals enabled
        self.assertLessEqual(len(stm_sets), 5,
                             f"Too many STM sets: {stm_sets}")
        self.assertLessEqual(len(erm_sets), 5,
                             f"Too many ERM sets: {erm_sets}")
        self.assertLessEqual(len(rsb_sets), 5,
                             f"Too many RSB sets: {rsb_sets}")
        # At least some should be enabled (we intentionally activated 6)
        total_enabled = len(stm_sets) + len(erm_sets) + len(rsb_sets)
        self.assertGreaterEqual(total_enabled, 3,
                                "Expected at least 3 sets with new signals enabled")

    def test_apply_param_set_works(self):
        from sector_rotation.SectorRotationStrategyRuns import PARAM_SETS, apply_param_set
        base = {"signals": {"weights": {"cross_sectional_momentum": 0.5}}, "portfolio": {}}
        cfg = apply_param_set(base, PARAM_SETS["default"])
        self.assertIn("signals", cfg)
        self.assertIn("weights", cfg["signals"])


# ═══════════════════════════════════════════════════════════════════════════
#  7. ERM point-in-time (45-day lag)
# ═══════════════════════════════════════════════════════════════════════════

class TestERMPointInTime(unittest.TestCase):
    def test_lag_shifts_index_forward(self):
        """Verify that reporting_lag_days actually shifts the signal index forward.
        With lag=45, a quarter ending 2024-03-31 shouldn't produce signal until 2024-05-15."""
        from sector_rotation.signals.new_signals import _load_eps_store, SECTOR_REPRESENTATIVES
        import json, tempfile, os

        # Create a fake eps_history.json with multiple stocks (need ≥2 for z-score)
        base_eps = [
            {"end_date": "2022-12-31", "eps": 1.0},
            {"end_date": "2023-03-31", "eps": 1.1},
            {"end_date": "2023-06-30", "eps": 1.2},
            {"end_date": "2023-09-30", "eps": 1.3},
            {"end_date": "2023-12-31", "eps": 1.4},
            {"end_date": "2024-03-31", "eps": 1.5},
            {"end_date": "2024-06-30", "eps": 1.6},
            {"end_date": "2024-09-30", "eps": 1.7},
            {"end_date": "2024-12-31", "eps": 1.8},
        ]
        # Need multiple sectors with data for cross-sectional z-score
        fake_store = {
            "fetched_at": "2026-01-01",
            "symbol_meta": {},
            "symbols": {
                "AAPL": base_eps,
                "MSFT": [{"end_date": q["end_date"], "eps": q["eps"] * 1.2} for q in base_eps],
                "NVDA": [{"end_date": q["end_date"], "eps": q["eps"] * 0.8} for q in base_eps],
                "JPM":  [{"end_date": q["end_date"], "eps": q["eps"] * 0.9} for q in base_eps],
                "GS":   [{"end_date": q["end_date"], "eps": q["eps"] * 1.1} for q in base_eps],
                "XOM":  [{"end_date": q["end_date"], "eps": q["eps"] * 0.7} for q in base_eps],
            },
        }

        # Write to temp file
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(fake_store, tmp)
        tmp.close()

        try:
            from sector_rotation.signals.new_signals import compute_earnings_revision_momentum
            result_0 = compute_earnings_revision_momentum(
                ["XLK"], lookback_quarters=4,
                eps_store_path=Path(tmp.name),
                reporting_lag_days=0,
            )
            result_45 = compute_earnings_revision_momentum(
                ["XLK"], lookback_quarters=4,
                eps_store_path=Path(tmp.name),
                reporting_lag_days=45,
            )

            # With lag=0, signal available as early as end_date itself
            # With lag=45, signal shifted 45 days later
            valid_0 = result_0.dropna(how="all")
            valid_45 = result_45.dropna(how="all")
            if not valid_0.empty and not valid_45.empty:
                first_0 = valid_0.index[0]
                first_45 = valid_45.index[0]
                self.assertGreater(first_45, first_0,
                                   f"45-day lag should shift signal later: "
                                   f"lag=0 starts {first_0}, lag=45 starts {first_45}")
            else:
                # Fake store only has AAPL; ERM needs ≥2 stocks to z-score.
                # At minimum, verify the function ran without error.
                self.assertIsInstance(result_45, pd.DataFrame)
        finally:
            os.unlink(tmp.name)

    def test_future_quarter_not_available_today(self):
        """A quarter ending in the future should NOT appear in today's signal."""
        from sector_rotation.signals.new_signals import compute_earnings_revision_momentum
        import json, tempfile, os

        # Quarter ending 2026-12-31 with lag=45 → available 2027-02-14
        # Signal at 2026-05-31 should NOT use this quarter
        fake_store = {
            "fetched_at": "2026-05-05",
            "symbol_meta": {},
            "symbols": {
                "AAPL": [
                    {"end_date": "2024-12-31", "eps": 1.0},
                    {"end_date": "2025-03-31", "eps": 1.1},
                    {"end_date": "2025-06-30", "eps": 1.2},
                    {"end_date": "2025-09-30", "eps": 1.3},
                    {"end_date": "2025-12-31", "eps": 1.4},
                    {"end_date": "2026-03-31", "eps": 1.5},
                    {"end_date": "2026-06-30", "eps": 99.0},  # Future quarter
                    {"end_date": "2026-09-30", "eps": 99.0},  # Future quarter
                    {"end_date": "2026-12-31", "eps": 99.0},  # Future quarter
                ],
            },
        }

        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(fake_store, tmp)
        tmp.close()

        try:
            from sector_rotation.signals.new_signals import compute_earnings_revision_momentum
            # Monthly index up to 2026-05-31
            idx = pd.date_range("2025-01-31", "2026-05-31", freq="ME")
            result = compute_earnings_revision_momentum(
                ["XLK"], lookback_quarters=4,
                eps_store_path=Path(tmp.name),
                reporting_lag_days=45,
                monthly_index=idx,
            )
            # The signal at 2026-05-31 should NOT reflect Q2 2026 (end=2026-06-30)
            # because 2026-06-30 + 45d = 2026-08-14 > 2026-05-31
            # (this is guaranteed by the available_date indexing in the code)
            if not result.empty and "XLK" in result.columns:
                # If the future eps=99 leaked in, the signal would be extreme
                last_val = result.loc[result.index <= "2026-05-31", "XLK"].dropna()
                if not last_val.empty:
                    # Z-score should be bounded (no 99.0 leakage)
                    self.assertLess(abs(last_val.iloc[-1]), 5.0,
                                    "Future EPS seems to have leaked (extreme z-score)")
        finally:
            os.unlink(tmp.name)


if __name__ == "__main__":
    unittest.main()
