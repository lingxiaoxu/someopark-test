"""单测: evaluation 层(metrics 双口径手算对拍/eda/acf/bakeoff;输出仅 /tmp)。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from VolumePrediction.evaluation.metrics import (
    oos_r2_eta, r2_v_total, mse, decile_analysis, feature_importance_knockout)
from VolumePrediction.evaluation.eda import (
    baseline_predictability_table, fig1_distributions, correlation_matrix)
from VolumePrediction.evaluation.acf import acf_pacf, significant_lags, panel_significant_lags
from VolumePrediction.evaluation.target_bakeoff import run_bakeoff, build_targets
from VolumePrediction.tests.test_models_classic import make_panel, split

TMP = Path("/tmp/vp_tests/evaluation")
TMP.mkdir(parents=True, exist_ok=True)

X, y = make_panel(n_days=150, n_tickers=5)
Xtr, ytr, Xte, yte = split(X, y)


def test_oos_r2_eta_hand_calc():
    t = pd.Series([1.0, -1.0, 2.0])
    h = pd.Series([0.5, -0.5, 1.0])
    # 1 − Σ(e−h)²/Σe² = 1 − (0.25+0.25+1)/(1+1+4) = 1 − 1.5/6
    assert abs(oos_r2_eta(t, h) - (1 - 1.5 / 6)) < 1e-12
    # ma5 归一化零点: η̂=0 → R²=0
    assert abs(oos_r2_eta(t, pd.Series(0.0, index=t.index))) < 1e-12


def test_r2_v_total_hand_calc():
    v = pd.Series([10.0, 12.0, 11.0])
    ma5 = pd.Series([10.5, 11.5, 11.0])
    eta_hat = pd.Series([0.0, 0.0, 0.0])
    sse = ((v - ma5) ** 2).sum()
    sst = ((v - v.mean()) ** 2).sum()
    assert abs(r2_v_total(v, ma5, eta_hat) - (1 - sse / sst)) < 1e-12
    assert abs(mse(v, ma5) - float(((v - ma5) ** 2).mean())) < 1e-12


def test_decile_monotonic_on_perfect_signal():
    d = decile_analysis(yte, yte, n=5)   # 完美预测 → 单调、正差
    assert d["monotone_spread"].iloc[0] > 0
    assert (d["mean_real"].diff().dropna() >= -1e-9).all()


def test_knockout_direction():
    from VolumePrediction.models.linear import OLSModel
    imp = feature_importance_knockout(
        OLSModel, Xtr, ytr, Xte, yte,
        groups={"signal": ["tech_f0", "tech_f1", "tech_f2"],
                "noise": ["tech_f5", "tech_f6", "tech_f7"]})
    assert imp.loc["signal", "r2_drop"] > imp.loc["noise", "r2_drop"]
    assert imp.loc["signal", "r2_drop"] > 0.3


def test_eda_outputs():
    tbl = baseline_predictability_table(X, out_dir=TMP)
    assert set(tbl.index) == {"ma5", "lag1", "ma22", "ma252"}
    assert (TMP / "baseline_predictability.csv").exists()
    p = fig1_distributions(X, TMP)
    assert p.exists() and p.stat().st_size > 1000
    corr = correlation_matrix(X, out_dir=TMP)
    assert abs(corr.iloc[0, 0] - 1.0) < 1e-9


def test_acf_significant_lags():
    rng = np.random.default_rng(0)
    ar = [0.0]
    for _ in range(500):
        ar.append(0.6 * ar[-1] + rng.normal())
    s = pd.Series(ar)
    tbl = acf_pacf(s, nlags=10)
    assert abs(tbl.loc[1, "acf"] - 0.6) < 0.15
    lags = significant_lags(s, nlags=10, kind="pacf")
    assert 1 in lags
    res = panel_significant_lags(X, col="eta", nlags=6, sample_tickers=3)
    assert "lags" in res and res["n_tickers_used"] > 0


def test_target_bakeoff_frame():
    from VolumePrediction.models.linear import OLSModel
    df = run_bakeoff(Xtr, Xte, {"ols": OLSModel}, out_dir=TMP)
    # 3 常规目标 × 1 模型 + quantile × 1 = 4 行
    assert len(df) == 4
    assert (TMP / "target_bakeoff.csv").exists()
    tg = build_targets(X)
    assert set(tg) == {"eta", "log_volume", "sqrt_shock", "quantile_eta"}
    # eta 目标下 OLS 的 v-空间 R̄² 应显著为正(信号可学)
    assert df.loc[("eta", "ols"), "v_space_r2_total"] > 0
