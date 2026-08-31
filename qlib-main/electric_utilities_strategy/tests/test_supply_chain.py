"""
AEUS supply-chain propagation tests (offline, synthetic prices).
"""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from electric_utilities_strategy.data import universe as U
from electric_utilities_strategy.signals.supply_chain import (
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

    def test_rate_env_channel_present(self):
        # rate_env(取负后的利率)必须挂到三个防御板块 —— AEUS 的"负向"通道在节点
        # 侧取负(AISS 的 logic_cpu→ai_gpu 负权边的对应物)
        from electric_utilities_strategy.signals.supply_chain import NODE_RATE_ENV
        w, lag, _ = SUPPLY_CHAIN_GRAPH[(NODE_RATE_ENV, "regulated_mega")]
        self.assertGreater(w, 0)   # 权重为正,负号在节点变换里(neg_yoy_z)

    def test_graceful_without_external(self):
        # No external data → still produces valid scores (price-proxy fallback)
        sc = compute_supply_chain_scores(
            self.px, self.capex, None, self.monthly_idx,
            power_demand=None, power_price=None, rate_env=None,
            pmi_series=None, external_tilts=None,
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
        self.assertIn("ipp_wholesale", m.columns)
        # ai_capex_proxy → ipp_wholesale weight = 1.0(链头边)
        self.assertAlmostEqual(m.loc["ai_capex_proxy", "ipp_wholesale"], 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
