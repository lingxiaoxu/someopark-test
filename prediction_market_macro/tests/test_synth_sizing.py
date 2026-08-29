"""sizing.py — the (a) harness measures pairing structure and nothing else."""
import numpy as np
import pytest

from prediction_market_macro.research.synth import sizing


def _fake_inc(rho, n_series=3, n_paths=64, n_weeks=12, seed=0):
    rng = np.random.default_rng(seed)
    common = rng.standard_normal((n_paths, n_weeks))
    return np.stack([np.sqrt(rho) * common
                     + np.sqrt(1 - rho) * rng.standard_normal((n_paths, n_weeks))
                     for _ in range(n_series)])


def test_ratio_reads_one_under_independence_and_above_under_common_factor(monkeypatch):
    """A shuffled pairing destroys alignment and nothing else, so independent series
    give ratio ~1 and positively coupled ones give ratio > 1 — the harness's whole job."""
    for rho, lo, hi in ((0.0, 0.8, 1.2), (0.5, 1.3, 3.0)):
        inc = _fake_inc(rho)
        monkeypatch.setattr(sizing, "weekly_increments",
                            lambda root, after, _inc=inc: (_inc, list("abc"),
                                                           _inc.shape[2]))
        r = sizing.independence_mispricing(None, "", reps=500, seed=1)
        assert lo < r["ratio"] < hi, (rho, r["ratio"])
        assert r["ratio_p05"] < r["ratio"] < r["ratio_p95"]


def test_missing_worlds_refuse_loudly(tmp_path):
    with pytest.raises(FileNotFoundError, match="no worlds"):
        sizing.weekly_increments(tmp_path, "2026-01-01")
