"""
AISS universe tests (offline, no network).
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from semiconductor_strategy.data import universe as U


class TestUniverse(unittest.TestCase):

    def test_eight_subsectors(self):
        self.assertEqual(len(U.subsector_names()), 8)

    def test_unique_tickers(self):
        # 8x3 = 24 slots but ARM is shared → 23 unique
        tickers = U.all_tickers()
        self.assertEqual(len(tickers), 23)
        self.assertEqual(len(set(tickers)), 23)  # truly unique
        self.assertIn("NVDA", tickers)
        self.assertIn("ARM", tickers)

    def test_arm_dedup_across_subsectors(self):
        subs = U.subsector_of("ARM")
        self.assertEqual(set(subs), {"ai_gpu", "logic_cpu"})
        # ARM appears once in the deduped ticker list
        self.assertEqual(U.all_tickers().count("ARM"), 1)

    def test_subsector_weights_sum_to_one(self):
        for s in U.subsector_names():
            w = U.subsector_weights(s)
            self.assertAlmostEqual(sum(w.values()), 1.0, places=9)
            self.assertEqual(len(w), 3)

    def test_benchmarks(self):
        self.assertEqual(U.benchmark_tickers(), ["SOXX", "SMH", "SPY"])

    def test_effective_weights_ipo_fallback(self):
        # Early date: ARM (2023 IPO) and ALAB (2024 IPO) absent → ai_gpu = NVDA only
        ew_early = U.effective_weights("ai_gpu", "2019-06-30")
        self.assertEqual(list(ew_early.keys()), ["NVDA"])
        self.assertAlmostEqual(ew_early["NVDA"], 1.0)
        # foundry early: GFS (2021 IPO) absent → TSM + UMC renormalized
        ew_f = U.effective_weights("foundry", "2020-01-31")
        self.assertNotIn("GFS", ew_f)
        self.assertAlmostEqual(sum(ew_f.values()), 1.0, places=9)
        self.assertGreater(ew_f["TSM"], 0.80)  # renormalized up from 0.80

    def test_cost_blend(self):
        # subsector cost = 80/15/5 blend of constituent tiers, within tier range
        for s in U.subsector_names():
            c = U.subsector_cost_bps(s)
            self.assertGreaterEqual(c, 3.0)
            self.assertLessEqual(c, 8.0)

    def test_get_tickers_returns_subsectors(self):
        # SSRS-compat shim: tradeable assets = subsectors
        self.assertEqual(U.get_tickers(), U.subsector_names())


if __name__ == "__main__":
    unittest.main(verbosity=2)
