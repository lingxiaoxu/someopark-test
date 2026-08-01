"""单测: Tier1/2/3 经典模型(合成数据,轻量;输出仅 /tmp/vp_tests/)。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from VolumePrediction.models import feature_cols
from VolumePrediction.models.baselines import (
    MA5Model, PrevDayModel, ARIMAPerTicker, SARIMAPerTicker)
from VolumePrediction.models.linear import (
    OLSModel, LassoCVModel, PCRModel, PLSModel, ForwardStepwiseModel)
from VolumePrediction.models.ml import AdaBoostModel, NN2Model

TMP = Path("/tmp/vp_tests/models_classic")
TMP.mkdir(parents=True, exist_ok=True)


def make_panel(n_days=120, n_tickers=6, n_feat=8, seed=0):
    """合成面板: eta 由前 3 个特征线性生成+噪声 → 线性模型应能高 R² 恢复。"""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2023-01-02", periods=n_days)
    tickers = [f"T{i}" for i in range(n_tickers)]
    idx = pd.MultiIndex.from_product([dates, tickers], names=["date", "ticker"])
    X = pd.DataFrame(rng.normal(size=(len(idx), n_feat)),
                     index=idx, columns=[f"tech_f{i}" for i in range(n_feat)])
    beta = np.zeros(n_feat)
    beta[:3] = [0.8, -0.5, 0.3]
    eta = pd.Series(X.values @ beta + rng.normal(0, 0.1, len(idx)),
                    index=idx, name="eta")
    v = pd.Series(18.0, index=idx) + eta.groupby(level=1).cumsum() * 0.01 + eta
    ma5 = v.groupby(level=1).transform(lambda s: s.rolling(5).mean()).fillna(v)
    X["v"], X["ma5_v"], X["V"], X["eta"] = v, ma5, np.exp(v), eta
    return X, eta


def split(X, y, frac=0.7):
    dates = X.index.get_level_values(0).unique().sort_values()
    cut = dates[int(len(dates) * frac)]
    tr = X.index.get_level_values(0) <= cut
    return X[tr], y[tr], X[~tr], y[~tr]


X, y = make_panel()
Xtr, ytr, Xte, yte = split(X, y)


def _check_shape(m):
    yhat = m.predict(Xte)
    assert isinstance(yhat, pd.Series)
    assert yhat.index.equals(Xte.index)
    assert yhat.notna().all()
    return yhat


def test_ma5_zero_and_paramcount():
    m = MA5Model().fit(Xtr, ytr)
    yhat = _check_shape(m)
    assert (yhat == 0).all()
    assert m.param_count() == 0


def test_prevday_semantics():
    m = PrevDayModel().fit(Xtr, ytr)
    yhat = _check_shape(m)
    # 手算一票一日核对: η̂_t = v_{t-1} − ma5_t
    tkr = "T0"
    sub = Xte.xs(tkr, level=1)
    d1, d2 = sub.index[3], sub.index[4]
    expect = sub.loc[d1, "v"] - sub.loc[d2, "ma5_v"]
    got = yhat.loc[(d2, tkr)]
    assert abs(got - expect) < 1e-10


def test_ols_matches_lstsq():
    m = OLSModel().fit(Xtr, ytr)
    yhat = _check_shape(m)
    cols = feature_cols(Xtr)
    A = np.hstack([np.ones((len(Xtr), 1)), Xtr[cols].values])
    coef, *_ = np.linalg.lstsq(A, ytr.values, rcond=None)
    Ate = np.hstack([np.ones((len(Xte), 1)), Xte[cols].values])
    ref = Ate @ coef
    assert np.allclose(yhat.values, ref, atol=1e-8)
    assert m.param_count() == len(cols) + 1


def test_linear_family_recovers_signal():
    for cls in (LassoCVModel, PLSModel, PCRModel, ForwardStepwiseModel):
        m = cls().fit(Xtr, ytr)
        yhat = _check_shape(m)
        r2 = 1 - ((yte - yhat) ** 2).sum() / (yte ** 2).sum()
        assert r2 > 0.5, f"{cls.__name__} r2={r2:.3f}"


def test_pls_pcr_component_counts():
    assert PLSModel().n_components == 5
    assert PCRModel().n_components == 5
    fs = ForwardStepwiseModel(max_features=3).fit(Xtr, ytr)
    assert len(fs.selected_) <= 3
    assert fs.param_count() == len(fs.selected_) + 1
    # 贪心应优先选中真实信号特征
    assert set(fs.selected_) <= set(feature_cols(Xtr))
    assert "tech_f0" in fs.selected_


def test_adaboost_and_nn2():
    m = AdaBoostModel(n_estimators=20).fit(Xtr, ytr)
    _check_shape(m)
    nn = NN2Model(epochs=30, batch=256, seed=1, device="cpu").fit(Xtr, ytr)
    yhat = _check_shape(nn)
    r2 = 1 - ((yte - yhat) ** 2).sum() / (yte ** 2).sum()
    assert r2 > 0.3, f"NN2 r2={r2:.3f}"
    n_feat = len(feature_cols(Xtr))
    expect_params = (n_feat + 1) * 32 + (32 + 1) * 16 + (16 + 1) * 1
    assert nn.param_count() == expect_params


def test_nn2_gradient_attribution():
    nn = NN2Model(epochs=5, batch=256, seed=0, device="cpu").fit(Xtr, ytr)
    attr = nn.gradient_attribution(Xte, n_sample=256)
    assert attr.index[0] in ("tech_f0", "tech_f1")   # 信号特征应排前


def test_lightgbm_optional():
    lgb = pytest.importorskip("lightgbm")   # noqa: F841 — 未装则 skip
    from VolumePrediction.models.ml import LightGBMModel
    m = LightGBMModel(n_estimators=30).fit(Xtr, ytr)
    _check_shape(m)
    assert m.param_count() > 0


def test_arima_family_small():
    Xs, ys = make_panel(n_days=140, n_tickers=2, seed=2)
    Xtr2, ytr2, Xte2, yte2 = split(Xs, ys)
    m = ARIMAPerTicker().fit(Xtr2, ytr2)
    yhat = m.predict(Xte2)
    assert yhat.index.equals(Xte2.index)
    rep = m.report()
    assert rep["fitted"] + rep["failed"] == 2
    s = SARIMAPerTicker()
    s.min_train_obs = 90
    s.fit(Xtr2, ytr2)
    assert s.report()["fitted"] >= 1


def test_arima_failure_fallback():
    """不足样本票 → 记失败,预测回退 0(=MA5),不抛不静默。"""
    Xs, ys = make_panel(n_days=30, n_tickers=2, seed=3)   # < min_train_obs
    m = ARIMAPerTicker().fit(Xs, ys)
    assert m.report()["failed"] == 2
    yhat = m.predict(Xs)
    assert (yhat == 0).all()
