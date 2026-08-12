"""econ/mu_supplement(E11-T2)测试(小合成数据;输出只进 /tmp)。"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from VolumePrediction.econ.mu_supplement import (
    ou_half_life, pair_spread, pairs_ou_retention, synthetic_momentum_panel,
)
from VolumePrediction.econ.calibration import calibrate_mu_momentum
from VolumePrediction.calibrate_mu import momentum_decay_curve, decay_curve_from_panel

TMP = Path("/tmp/vp_tests/mu_supplement")
TMP.mkdir(parents=True, exist_ok=True)

RNG = np.random.default_rng(11)


def _ou_series(theta: float, n: int = 500, sigma: float = 0.02) -> np.ndarray:
    x = np.zeros(n)
    for i in range(1, n):
        x[i] = x[i - 1] * (1 - theta) + RNG.normal(0, sigma)
    return x


def test_ou_half_life_recovers_theta():
    theta = 0.10                                 # HL = ln2/0.1 ≈ 6.93
    hls = [ou_half_life(_ou_series(theta, 800)) for _ in range(20)]
    hls = [h for h in hls if h]
    assert len(hls) >= 15
    assert abs(np.median(hls) - math.log(2) / theta) < 2.0


def test_ou_half_life_rejects_random_walk_and_short():
    rw = np.cumsum(RNG.normal(0, 1, 800))        # β≈0 → None 或巨大 HL
    hl = ou_half_life(rw)
    assert hl is None or hl > 100
    assert ou_half_life(np.ones(5)) is None


def test_pairs_ou_retention_and_mu():
    idx = pd.bdate_range("2025-01-01", periods=300)
    theta = 0.15
    pairs, cols = [], {}
    for k in range(6):                           # 6 对协整对(共因子+OU 价差)
        common = np.cumsum(RNG.normal(0, 0.01, len(idx)))
        sp = _ou_series(theta, len(idx), 0.01)
        cols[f"A{k}"] = 100 * np.exp(common + sp)
        cols[f"B{k}"] = 100 * np.exp(common)
        pairs.append((f"A{k}", f"B{k}"))
    closes = pd.DataFrame(cols, index=idx)
    curve, diag = pairs_ou_retention(closes, pairs)
    assert curve is not None and diag["n_valid_hl"] >= 4
    assert abs(curve.loc[0] - 1.0) < 1e-9        # R(0)=1
    assert (curve.diff().dropna() < 0).all()     # 单调衰减
    hl_med = diag["hl_median"]
    assert abs(hl_med - math.log(2) / theta) < 3.0
    r = calibrate_mu_momentum(curve, strategy="pairs")
    # μ ≈ 初段日均衰减 ≈ 1−exp(−θ)(θ 尺度),必为正实测
    assert r["calibration_source"] == "alpha_decay_curve"
    assert 0.2 * theta < r["mu"] < 2.0 * theta


def test_pairs_ou_retention_insufficient_pairs_returns_none():
    idx = pd.bdate_range("2025-01-01", periods=300)
    closes = pd.DataFrame({"A0": 100 + RNG.normal(0, 1, len(idx)).cumsum()},
                          index=idx)
    curve, diag = pairs_ou_retention(closes, [("A0", "MISSING")])
    assert curve is None
    assert diag["per_pair"]["A0/MISSING"]["reason"] == "missing prices"


def test_synthetic_momentum_panel_shape_and_score():
    idx = pd.bdate_range("2023-01-01", periods=400)
    closes = pd.DataFrame(
        {t: 100 * np.exp(np.cumsum(RNG.normal(0.0002, 0.01, len(idx))))
         for t in ["XLB", "XLE", "XLK", "XLV"]}, index=idx)
    panel = synthetic_momentum_panel(closes, lookback=252, skip=21)
    assert len(panel) == len(idx) - 252
    d0 = sorted(panel)[0]
    tk, (score, price) = next(iter(panel[d0].items()))
    i = list(idx.strftime("%Y-%m-%d")).index(d0)
    expect = closes[tk].iloc[i - 21] / closes[tk].iloc[i - 252] - 1
    assert abs(score - expect) < 1e-12
    assert abs(price - closes[tk].iloc[i]) < 1e-12


def test_decay_curve_refactor_equivalence(tmp_path):
    """momentum_decay_curve(文件通路)≡ decay_curve_from_panel(面板通路)。"""
    days = pd.bdate_range("2026-01-01", periods=40).strftime("%Y-%m-%d")
    panel = {}
    for j, d in enumerate(days):
        sig = []
        day = {}
        for k, tk in enumerate(["AA", "BB", "CC", "DD"]):
            score = math.sin(j / 5 + k)
            price = 50 + k * 10 + j * (0.1 + 0.05 * k)
            sig.append({"ticker": tk, "composite_score": score, "price": price})
            day[tk] = (score, price)
        panel[d] = day
        (tmp_path / f"r_daily_report_{d}.json").write_text(
            json.dumps({"signal_date": d, "signals": sig}))
    c1, d1 = momentum_decay_curve(tmp_path, "r_daily_report_*.json")
    c2, d2 = decay_curve_from_panel(panel)
    assert c1 is not None and c2 is not None
    pd.testing.assert_series_equal(c1, c2)
    assert d1["n_events"] == d2["n_events"]
    assert d1["alpha_by_delay"] == d2["alpha_by_delay"]
