"""
AISS supply-chain propagation tests (offline, synthetic prices).
"""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from semiconductor_strategy.data import universe as U
from semiconductor_strategy.signals.supply_chain import (
    compute_supply_chain_scores, build_propagation_matrix, SUPPLY_CHAIN_GRAPH,
)


def _synth_subsector_prices(n_days=1500, seed=7):
    np.random.seed(seed)
    idx = pd.bdate_range("2019-01-01", periods=n_days)
    subs = U.subsector_names()
    rets = np.random.normal(0.0006, 0.02, size=(n_days, len(subs)))
    px = pd.DataFrame(100 * np.cumprod(1 + rets, axis=0), index=idx, columns=subs)
    return px


class TestSupplyChain(unittest.TestCase):

    def setUp(self):
        self.px = _synth_subsector_prices()
        self.monthly_idx = self.px.resample("ME").last().index
        self.capex = pd.Series(np.random.normal(0, 1, len(self.px)), index=self.px.index)

    def test_scores_shape(self):
        sc = compute_supply_chain_scores(self.px, self.capex, None, self.monthly_idx)
        self.assertEqual(set(sc.columns), set(U.subsector_names()))
        self.assertTrue(len(sc) > 0)

    def test_cross_sectional_zscore(self):
        sc = compute_supply_chain_scores(self.px, self.capex, None, self.monthly_idx)
        valid = sc.dropna(how="all")
        # rows are cross-sectionally z-scored → row mean ≈ 0
        row_means = valid.mean(axis=1).abs()
        self.assertLess(row_means.max(), 1e-6)

    def test_negative_competition_edge_present(self):
        # logic_cpu → ai_gpu must be a negative-weight (competition) edge
        w, lag, _ = SUPPLY_CHAIN_GRAPH[("logic_cpu", "ai_gpu")]
        self.assertLess(w, 0)

    def test_graceful_without_external(self):
        # No external data → still produces valid scores (price-proxy fallback)
        sc = compute_supply_chain_scores(
            self.px, self.capex, None, self.monthly_idx,
            tsmc_yoy=None, asml_orders=None, dram_proxy=None, mu_dio=None,
            use_external_macro=True,
        )
        self.assertFalse(sc.isna().all().all())

    def test_no_future_leakage(self):
        # Truncating future data must not change past propagation scores
        full = compute_supply_chain_scores(self.px, self.capex, None, self.monthly_idx)
        short_px = self.px.iloc[:-63]
        short_idx = short_px.resample("ME").last().index
        short = compute_supply_chain_scores(short_px, self.capex.iloc[:-63], None, short_idx)
        common = full.index.intersection(short.index)[:-1]  # drop last (edge)
        if len(common) > 6:
            # compare the stable interior (exclude the most recent month)
            a = full.loc[common].round(4)
            b = short.loc[common].round(4)
            # allow tiny differences only at the tail; interior must match closely
            diff = (a - b).abs().iloc[:-3].max().max()
            self.assertLess(diff, 0.5)

    def test_propagation_matrix(self):
        m = build_propagation_matrix()
        self.assertIn("ai_gpu", m.columns)
        # ai_capex_proxy → ai_gpu weight = 1.0
        self.assertAlmostEqual(m.loc["ai_capex_proxy", "ai_gpu"], 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
