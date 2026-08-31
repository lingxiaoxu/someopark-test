"""
AEUS universe tests (offline, no network).

Rewritten for the 10-subsector / 41-member AEUS universe (AEUS_PLAN §2.1);
assertion CATEGORIES mirror the AISS suite one-for-one (count, uniqueness,
reserves + accident cascade, weight sums, benchmarks, IPO gating, cost blend,
engine shim) plus AEUS additions (purity scores, N-member generalisation,
capex beta coverage).
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from electric_utilities_strategy.data import universe as U


class TestUniverse(unittest.TestCase):

    def test_ten_subsectors(self):
        self.assertEqual(len(U.subsector_names()), 10)
        self.assertEqual(U.subsector_names()[0], "nuclear_fuel")

    def test_unique_tickers(self):
        # Weighted universe: 41 members, no cross-subsector duplicates in AEUS
        weighted = U.all_tickers(include_reserve=False)
        self.assertEqual(len(weighted), 41)
        self.assertEqual(len(set(weighted)), 41)
        for anchor in ("BWXT", "KMI", "ETN", "PWR", "VST", "NEE", "AEE",
                       "VRT", "NXT", "AWK"):
            self.assertIn(anchor, weighted)
        # With reserves: +10 unique
        full = U.all_tickers()  # include_reserve=True by default
        self.assertEqual(len(full), 51)
        self.assertEqual(len(set(full)), 51)
        for r in ("NXE", "LNG", "VMI", "DY", "ORA", "D", "BKH", "AOS",
                  "SHLS", "YORW"):
            self.assertIn(r, full)

    def test_reserves(self):
        # Each subsector has exactly one 0%-weight reserve, not in members.
        for s in U.subsector_names():
            r = U.subsector_reserve(s)
            self.assertIsNotNone(r)
            self.assertNotIn(r, U.subsector_tickers(s))  # reserve is separate
        # Accident cascade: a stale anchor hands its weight to the reserve.
        ew = U.effective_weights("grid_equipment", "2026-08-01",
                                 unavailable={"ETN"})
        self.assertIn("VMI", ew)
        self.assertNotIn("ETN", ew)
        self.assertAlmostEqual(sum(ew.values()), 1.0, places=9)
        # Normal case: no reserve present.
        ew0 = U.effective_weights("grid_equipment", "2026-08-01")
        self.assertNotIn("VMI", ew0)
        self.assertEqual(set(ew0), {"ETN", "EMR", "GEV", "POWL"})

    def test_no_duplicate_members_within_subsector(self):
        for s in U.subsector_names():
            tk = U.subsector_tickers(s)
            self.assertEqual(len(tk), len(set(tk)))

    def test_subsector_weights_sum_to_one(self):
        # N-member generalisation: 4 or 5 weighted members per subsector.
        for s in U.subsector_names():
            w = U.subsector_weights(s)
            self.assertAlmostEqual(sum(w.values()), 1.0, places=9)
            self.assertIn(len(w), (4, 5))

    def test_benchmarks(self):
        self.assertEqual(U.benchmark_tickers(), ["XLU", "GRID", "SPY"])
        self.assertEqual(U.PRIMARY_BENCHMARK, "XLU")

    def test_effective_weights_ipo_fallback(self):
        # GEV spun off 2024-04 → absent (24m gate) until 2026-04; the other
        # three members renormalise proportionally.
        ew_2024 = U.effective_weights("grid_equipment", "2024-06-30")
        self.assertNotIn("GEV", ew_2024)
        self.assertAlmostEqual(sum(ew_2024.values()), 1.0, places=9)
        self.assertAlmostEqual(ew_2024["ETN"], 0.40 / 0.80, places=9)
        # After the gate clears the full base_w vector returns.
        ew_2026 = U.effective_weights("grid_equipment", "2026-06-30")
        self.assertEqual(set(ew_2026), {"ETN", "EMR", "GEV", "POWL"})
        self.assertAlmostEqual(ew_2026["GEV"], 0.20, places=9)
        # nuclear_fuel early: OKLO (2024-05) and SMR (2022-05) both gated in 2019.
        ew_n = U.effective_weights("nuclear_fuel", "2019-06-30")
        self.assertNotIn("OKLO", ew_n)
        self.assertNotIn("SMR", ew_n)
        self.assertAlmostEqual(sum(ew_n.values()), 1.0, places=9)
        self.assertGreater(ew_n["BWXT"], 0.45)  # renormalised up from 0.45
        # ipp_wholesale early: CEG (2022-02) and TLN (2023-07) gated in 2019.
        ew_i = U.effective_weights("ipp_wholesale", "2019-06-30")
        self.assertEqual(set(ew_i), {"VST", "NRG"})
        self.assertAlmostEqual(sum(ew_i.values()), 1.0, places=9)

    def test_first_available_overrides_ipo_dates(self):
        # Real price data (first_available) takes precedence over IPO_DATES.
        import datetime as _dt
        ew = U.effective_weights(
            "grid_equipment", "2026-06-30",
            first_available={"GEV": _dt.date(2026, 1, 2)})  # pretend recent
        self.assertNotIn("GEV", ew)  # 24m gate not met under the override

    def test_cost_blend(self):
        # subsector cost = base_w blend of member tiers, within tier range
        for s in U.subsector_names():
            c = U.subsector_cost_bps(s)
            self.assertGreaterEqual(c, 3.0)
            self.assertLessEqual(c, 8.0)
        # Every universe ticker (incl. reserves) has an explicit tier.
        for t in U.all_tickers():
            self.assertIn(t, U.STOCK_TIER, f"{t} missing from STOCK_TIER")

    def test_purity_scores(self):
        # Every weighted member and reserve carries a purity score in [0, 1].
        for s in U.subsector_names():
            for t in U.subsector_tickers(s) + [U.subsector_reserve(s)]:
                p = U.get_purity(t, s)
                self.assertGreaterEqual(p, 0.0, f"{s}/{t}")
                self.assertLessEqual(p, 1.0, f"{s}/{t}")
        # Spot checks from AEUS_PLAN §2.5.
        self.assertEqual(U.get_purity("GEV"), 1.00)   # pure grid alpha amplifier
        self.assertEqual(U.get_purity("EMR"), 0.35)   # diversified industrial
        self.assertEqual(U.get_purity("VRT"), 1.00)
        # Unknown ticker → 0.0, never a crash.
        self.assertEqual(U.get_purity("ZZZZ"), 0.0)

    def test_capex_beta_and_groups(self):
        # CAPEX_BETA covers all 10 subsectors; defensive/AI-cycle groups valid.
        self.assertEqual(set(U.CAPEX_BETA), set(U.subsector_names()))
        for s in U.DEFENSIVE_SUBSECTORS + U.AI_CYCLE_SUBSECTORS:
            self.assertIn(s, U.SUBSECTORS)
        self.assertIn("water_cooling", U.DEFENSIVE_SUBSECTORS)

    def test_get_tickers_returns_subsectors(self):
        # Engine-compat shim: tradeable assets = subsectors
        self.assertEqual(U.get_tickers(), U.subsector_names())


if __name__ == "__main__":
    unittest.main(verbosity=2)
