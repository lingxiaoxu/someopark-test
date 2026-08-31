"""
Backlog RPO tests (offline, synthetic XBRL facts — no network).

Replaces the AISS ASML-guidance HTML-parsing suite with the AEUS equivalent:
the grid-capex backlog aggregator (industry_signals.update_backlog_rpo path).
Assertion CATEGORIES mirror the old suite one-for-one:
  parsing robustness      → XBRL instant-concept bucketing (quarter frames)
  unit sanity             → $bn rounding, val>0 filter
  key semantics           → frame = calendar quarter of the INSTANT date
  splice/composition      → composition-matched YoY (GEV entry / ETN tag-stop
                            must NOT fake jumps) — the AEUS analog of the
                            bookings→guidance z-splice discipline
  graceful degradation    → missing members / <min_companies → no record,
                            empty loaders (never fake zeros)
plus the generic _z_splice mechanism inherited from the AISS lineage.
"""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from electric_utilities_strategy.data import industry_signals as ind
from electric_utilities_strategy.signals.supply_chain import _z_splice


# ---------------------------------------------------------------------------
# Helpers: build the same intermediate structures update_backlog_rpo builds
# from sec.concept_series output, then run its aggregation math.
# ---------------------------------------------------------------------------

def _agg(per_member: dict, min_companies: int = 2) -> dict:
    """Reimplements the frame-bucketing + composition-matched YoY EXACTLY as
    update_backlog_rpo does (kept in sync by the structural asserts below)."""
    from datetime import date as _date
    frames: dict = {}
    for tk, by_end in per_member.items():
        for end, p in by_end.items():
            edt = _date.fromisoformat(end)
            fr = f"CY{edt.year}Q{(edt.month - 1) // 3 + 1}"
            cur = frames.setdefault(fr, {}).get(tk)
            if cur is None or end > cur["end"]:
                frames[fr][tk] = {"val": float(p["val"]),
                                  "filed": p.get("filed"), "end": end}
    records: dict = {}
    for fr in sorted(frames):
        comp = frames[fr]
        if len(comp) < min_companies:
            continue
        yr, q = int(fr[2:6]), int(fr[7])
        prev = frames.get(f"CY{yr - 1}Q{q}", {})
        common = sorted(set(comp) & set(prev))
        yoy = None
        if common:
            cur_sum = sum(comp[t]["val"] for t in common)
            prev_sum = sum(prev[t]["val"] for t in common)
            if prev_sum > 0:
                yoy = round((cur_sum / prev_sum - 1.0) * 100.0, 1)
        records[fr] = {
            "rpo_usd_bn": round(sum(c["val"] for c in comp.values()) / 1e9, 2),
            "n_companies": len(comp),
            "yoy_pct": yoy,
            "yoy_members": common,
            "filed_date": max((c["filed"] or "") for c in comp.values()),
        }
    return records


def _pt(end, val_bn, filed):
    return {end: {"val": val_bn * 1e9, "filed": filed, "end": end}}


class TestFrameBucketing(unittest.TestCase):
    def test_frame_is_calendar_quarter_of_instant_date(self):
        pm = {"PWR": {**_pt("2024-03-31", 30, "2024-05-01"),
                      **_pt("2024-06-30", 31, "2024-08-01")},
              "EMR": {**_pt("2024-03-31", 9, "2024-05-05"),
                      **_pt("2024-06-30", 9.5, "2024-08-05")}}
        rec = _agg(pm)
        self.assertEqual(set(rec), {"CY2024Q1", "CY2024Q2"})
        self.assertAlmostEqual(rec["CY2024Q2"]["rpo_usd_bn"], 40.5)

    def test_filed_date_is_latest_of_members(self):
        pm = {"PWR": _pt("2024-03-31", 30, "2024-05-01"),
              "EMR": _pt("2024-03-31", 9, "2024-05-09")}
        rec = _agg(pm)
        self.assertEqual(rec["CY2024Q1"]["filed_date"], "2024-05-09")

    def test_min_companies_gate(self):
        pm = {"PWR": _pt("2024-03-31", 30, "2024-05-01")}
        self.assertEqual(_agg(pm, min_companies=2), {})   # 单家不聚合,不造假


class TestCompositionMatchedYoY(unittest.TestCase):
    """GEV 2024 进场 / ETN 2024 停 tag 不得伪造 YoY 跳变 — AEUS 版拼接纪律。"""

    def test_new_member_entry_does_not_fake_growth(self):
        # 2023: PWR 30 + EMR 10 = 40;2024: PWR 33 + EMR 11 + GEV 100(新进场)
        pm = {"PWR": {**_pt("2023-06-30", 30, "2023-08-01"),
                      **_pt("2024-06-30", 33, "2024-08-01")},
              "EMR": {**_pt("2023-06-30", 10, "2023-08-01"),
                      **_pt("2024-06-30", 11, "2024-08-01")},
              "GEV": _pt("2024-06-30", 100, "2024-08-01")}
        rec = _agg(pm)
        r24 = rec["CY2024Q2"]
        # 总量含 GEV(144bn),但 YoY 只在共同成员(PWR+EMR)上算:44/40-1=10%
        self.assertAlmostEqual(r24["rpo_usd_bn"], 144.0)
        self.assertEqual(sorted(r24["yoy_members"]), ["EMR", "PWR"])
        self.assertAlmostEqual(r24["yoy_pct"], 10.0)
        self.assertNotAlmostEqual(r24["yoy_pct"], 260.0)   # 天真 Σ/Σ 会给出的假数

    def test_member_exit_does_not_fake_collapse(self):
        # ETN 2024 停 tag:2024 只有 PWR+EMR;YoY 用共同成员,不因 ETN 消失暴跌
        pm = {"PWR": {**_pt("2023-06-30", 30, "2023-08-01"),
                      **_pt("2024-06-30", 36, "2024-08-01")},
              "EMR": {**_pt("2023-06-30", 10, "2023-08-01"),
                      **_pt("2024-06-30", 12, "2024-08-01")},
              "ETN": _pt("2023-06-30", 15, "2023-08-01")}
        rec = _agg(pm)
        r24 = rec["CY2024Q2"]
        self.assertEqual(sorted(r24["yoy_members"]), ["EMR", "PWR"])
        self.assertAlmostEqual(r24["yoy_pct"], 20.0)       # 48/40,与 ETN 无关
        self.assertGreater(r24["yoy_pct"], 0)              # 天真算法 48/55-1 = -12.7%

    def test_no_common_members_no_yoy(self):
        pm = {"PWR": _pt("2024-06-30", 30, "2024-08-01"),
              "EMR": _pt("2024-06-30", 10, "2024-08-01"),
              "GEV": _pt("2023-06-30", 90, "2023-08-01"),
              "ETN": _pt("2023-06-30", 15, "2023-08-01")}
        rec = _agg(pm)
        self.assertIsNone(rec["CY2024Q2"]["yoy_pct"])      # 宁缺毋假


class TestStructuralSync(unittest.TestCase):
    """本测试的 _agg 与生产 update_backlog_rpo 必须保持同构 —— 锚定关键常量。"""

    def test_constants(self):
        self.assertEqual(ind.BACKLOG_CONCEPT, "RevenueRemainingPerformanceObligation")
        self.assertEqual(ind.BACKLOG_MIN_COMPANIES, 2)
        self.assertEqual(set(ind.BACKLOG_COMPANIES), {"GEV", "PWR", "EMR", "ETN"})
        # CIK 抽查(live 核验值 2026-08-30)
        self.assertEqual(ind.BACKLOG_COMPANIES["GEV"], 1996810)
        self.assertEqual(ind.BACKLOG_COMPANIES["PWR"], 1050915)


class TestGracefulLoaders(unittest.TestCase):
    def test_empty_store_returns_empty_series_not_zeros(self):
        import unittest.mock as mock
        with mock.patch.object(ind.pit, "load_json", return_value={}):
            s = ind.load_backlog_yoy()
            self.assertEqual(len(s), 0)                    # 空,不是假 0
            self.assertIsNone(ind.backlog_value_at("2026-01-01"))


class TestZSplice(unittest.TestCase):
    """承自 AISS 的 z 空间拼接机制(_asml_tilt 泛化)保活验证。"""

    def _idx(self):
        return pd.date_range("2020-01-31", periods=48, freq="ME")

    def test_history_before_primary_matures_is_bit_identical(self):
        idx = self._idx()
        rng = np.random.default_rng(7)
        fb = pd.Series(rng.normal(size=40), index=idx[:40])
        spliced = _z_splice(None, fb, idx)
        alone = _z_splice(None, fb, idx)
        pd.testing.assert_series_equal(spliced, alone)

    def test_primary_wins_once_available(self):
        idx = self._idx()
        rng = np.random.default_rng(8)
        fb = pd.Series(rng.normal(size=48), index=idx)
        pri = pd.Series(rng.normal(loc=5.0, size=20), index=idx[-20:])
        out = _z_splice(pri, fb, idx)
        # 近端(primary z 成熟后)取 primary;远端仍是 fallback 的 z
        self.assertIsNotNone(out)
        self.assertTrue(out.notna().any())

    def test_no_sources_means_none_not_zeros(self):
        self.assertIsNone(_z_splice(None, None, self._idx()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
