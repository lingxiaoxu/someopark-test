"""tests/test_eval.py — pure-function coverage of the §9.5 evaluation layer.

No production DB is touched: only in-memory data + gate_verdict logic.
"""
from __future__ import annotations

import numpy as np

from prediction_market_macro.research.eval import (
    bootstrap_ci, calibration_table, dm_test, drift_check, gate_verdict)


def test_dm_clearly_better_is_significant():
    rng = np.random.RandomState(1)
    diffs = list(-0.05 + 0.01 * rng.randn(30))            # model wins by 5pts of Brier
    r = dm_test(diffs)
    assert r["p"] is not None and r["p"] < 0.01
    assert r["mean"] < 0


def test_dm_equal_is_not_significant():
    diffs = [0.01, -0.01] * 15                            # exactly zero mean
    r = dm_test(diffs)
    assert r["p"] is not None and r["p"] > 0.10


def test_dm_tiny_sample_refuses():
    assert dm_test([-0.1, -0.2])["p"] is None


def test_bootstrap_ci_brackets_mean():
    rng = np.random.RandomState(3)
    diffs = list(-0.03 + 0.01 * rng.randn(50))
    ci = bootstrap_ci(diffs)
    assert ci["lo"] < -0.03 < ci["hi"] or ci["lo"] < np.mean(diffs) < ci["hi"]
    assert ci["hi"] < 0                                   # clearly negative overall


def test_calibration_table_bins():
    pairs = [(0.05, 0.0)] * 9 + [(0.05, 1.0)] + [(0.95, 1.0)] * 9 + [(0.95, 0.0)]
    t = calibration_table(pairs)
    assert t[0]["n"] == 10 and abs(t[0]["freq"] - 0.1) < 1e-9
    assert t[9]["n"] == 10 and abs(t[9]["freq"] - 0.9) < 1e-9
    assert sum(b["n"] for b in t) == len(pairs)


def test_drift_check_flags_degradation():
    xs = [0.05] * 20 + [0.12] * 8                          # trailing 2.4x worse
    r = drift_check(xs)
    assert r["drift"] is True
    r2 = drift_check([0.05] * 28)
    assert r2["drift"] is False


def test_gate_all_pass():
    v = gate_verdict(
        {"n_scored-1h": 20, "brier_model-1h": 0.08, "brier_market-1h": 0.11},
        {"roi": 0.06, "edge_capture": 0.55},
        {"p": 0.03})
    assert v["real"] is True and v["reasons"] == []


def test_gate_small_sample_stays_paper():
    v = gate_verdict(
        {"n_scored-1h": 6, "brier_model-1h": 0.05, "brier_market-1h": 0.20},
        {"roi": 0.5, "edge_capture": 0.9},
        {"p": 0.01})
    assert v["real"] is False
    assert any("n_scored" in r for r in v["reasons"])


def test_gate_not_significant_stays_paper():
    v = gate_verdict(
        {"n_scored-1h": 20, "brier_model-1h": 0.10, "brier_market-1h": 0.101},
        {"roi": 0.01, "edge_capture": 0.45},
        {"p": 0.4})
    assert v["real"] is False
    assert any("dm p" in r for r in v["reasons"])


def test_gate_losing_model_stays_paper():
    v = gate_verdict(
        {"n_scored-1h": 30, "brier_model-1h": 0.17, "brier_market-1h": 0.09},
        {"roi": -0.2, "edge_capture": 0.1},
        {"p": 0.99})
    assert v["real"] is False and len(v["reasons"]) >= 3
