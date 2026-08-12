"""econ/lambda_calibration(E11-T1)测试(DEV_CONTRACTS: 小合成数据;输出只进 /tmp)。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from VolumePrediction.econ.lambda_calibration import (
    amihud_panel, calibrate_lambda_amihud, rolling_calibration,
    s_curve_comparison, s_opt_generalized, PAPER_C, PAPER_GAMMA,
)

TMP = Path("/tmp/vp_tests/lambda")
TMP.mkdir(parents=True, exist_ok=True)

RNG = np.random.default_rng(7)


def _synthetic_bars(n_names=150, n_days=300, C=2.5e-4, gamma=0.8,
                    end="2026-06-30") -> pd.DataFrame:
    """已知 (C,γ) 的合成市场: ILLIQ_i = C·DV_i^(−γ) → |ret| = ILLIQ·DV。

    价格路径按目标 |ret| 生成(符号随机),v×vw 固定为该名字的 DV 水平
    (×轻噪声),使 amihud_panel 还原出的截面严格围绕真值。
    """
    days = pd.bdate_range(end=end, periods=n_days).strftime("%Y-%m-%d")
    rows = []
    for i in range(n_names):
        dv = 10 ** RNG.uniform(5.5, 10.0)            # $300k … $10B
        target_absret = C * dv ** (-gamma) * dv       # = C·dv^(1−γ)
        px = 100.0
        for d in days:
            r = target_absret * RNG.lognormal(0, 0.25) * RNG.choice([-1, 1])
            r = max(min(r, 0.3), -0.3)
            px *= (1 + r)
            vw = px
            v = dv * RNG.lognormal(0, 0.1) / vw
            rows.append({"date": d, "ticker": f"T{i:04d}",
                         "c": px, "v": v, "vw": vw})
    return pd.DataFrame(rows)


BARS = _synthetic_bars()
PANEL = amihud_panel(BARS)


def test_amihud_panel_columns_and_positivity():
    assert set(PANEL.columns) == {"date", "ticker", "dv", "illiq"}
    assert (PANEL["dv"] > 0).all() and (PANEL["illiq"] > 0).all()
    # 每名字首日无环比 → 行数 = names×(days−1) 上下(极端 ret 截断不减行)
    assert PANEL["ticker"].nunique() == 150


def test_recovers_known_C_gamma():
    r = calibrate_lambda_amihud(PANEL, asof="2026-06-30", window_days=252,
                                min_names=100, min_obs=60)
    assert r["calibration_source"] == "amihud_market_proxy"
    assert abs(r["gamma"] - 0.8) < 0.05, r["gamma"]
    assert abs(np.log(r["C"]) - np.log(2.5e-4)) < 0.25, r["C"]
    assert r["r2"] > 0.9
    assert r["n_names"] == 150
    # 分层: dv 中位递增,论文 λ 递减
    meds = [v["dv_median"] for v in r["tiers"].values()]
    assert meds == sorted(meds)


def test_insufficient_names_falls_back_to_paper_prior():
    small = PANEL[PANEL["ticker"].isin([f"T{i:04d}" for i in range(30)])]
    r = calibrate_lambda_amihud(small, asof="2026-06-30")
    assert r["calibration_source"] == "paper_prior"
    assert r["C"] == PAPER_C and r["gamma"] == PAPER_GAMMA


def test_pit_future_rows_excluded():
    r0 = calibrate_lambda_amihud(PANEL, asof="2026-03-31", window_days=120)
    # 注入 asof 之后的极端未来数据(把 illiq 全体放大 100 倍)
    future = PANEL[PANEL["date"] > "2026-03-31"].copy()
    future["illiq"] *= 100
    poisoned = pd.concat([PANEL, future], ignore_index=True)
    r1 = calibrate_lambda_amihud(poisoned, asof="2026-03-31", window_days=120)
    assert abs(r0["C"] - r1["C"]) < 1e-12
    assert abs(r0["gamma"] - r1["gamma"]) < 1e-12


def test_rolling_series_stable_on_stationary_data():
    roll = rolling_calibration(PANEL, ["2026-03-31", "2026-06-30"],
                               window_days=120)
    assert len(roll) == 2
    assert roll["gamma"].std() < 0.05            # 平稳合成市场 → 逐季稳定


def test_s_curve_shapes_and_bounds():
    calib = calibrate_lambda_amihud(PANEL, asof="2026-06-30", window_days=252)
    assert calib["calibration_source"] == "amihud_market_proxy"
    df = s_curve_comparison(calib)
    assert ((df["s_paper"] >= 0) & (df["s_paper"] <= 1)).all()
    assert ((df["s_calibrated"] >= 0) & (df["s_calibrated"] <= 1)).all()
    # λ 随 V 递减 → 固定 μ 下 s* 随 V 递增
    for _, g in df.groupby("mu"):
        s = g.sort_values("dollar_volume")["s_calibrated"].values
        assert (np.diff(s) >= -1e-12).all()
    # 边界语义与 policy 一致
    assert s_opt_generalized(1e8, float("inf"), 0.05, 0.8) == 1.0
    assert s_opt_generalized(1e8, 0.0, 0.05, 0.8) == 0.0
