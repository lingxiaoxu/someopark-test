"""
metrics — 评估指标(附录 A: E34-41 评估部分;G5 表 1 双口径;弱点⑪归因)
=====================================================================
论文双口径(G5,复现表 1 必须严格区分):
- 面板 A `oos_r2_eta`: 对 η 的 OOS R² = 1 − Σ(η−η̂)² / Σ η²。
  分母绕 **0** 而非样本均值——因为 η̂≡0(=ma5)是论文的 0% 归一化基准;
  这样 ma5 严格得 0,负值=比 ma5 还差(论文 Tier1 基线为负的口径)。
- 面板 B `r2_v_total`: 对 v 总方差的 R̄² = 1 − Σ(v−v̂)² / Σ(v−v̄)²,
  其中 v̂ = ma5 + η̂。ma5 单独≈93.68%,最优模型增量 ≤~1.2pp(G5 验收模式)。

其余: MSE、decile_analysis、feature_importance_knockout(全保留)、
SHAP(树系,延迟 import)与 NN 梯度归因入口(转发 NN2Model.gradient_attribution)。
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional

import numpy as np
import pandas as pd


# ── 论文双口径 R² ────────────────────────────────────────────────────────────

def oos_r2_eta(eta_true: pd.Series, eta_hat: pd.Series) -> float:
    """面板 A: 1 − SSE/Σε²(分母绕 0;ma5 恒为 0%)。"""
    e = eta_true.astype(float)
    h = eta_hat.reindex(e.index).astype(float)
    denom = float((e ** 2).sum())
    if denom == 0:
        return float("nan")
    return float(1.0 - ((e - h) ** 2).sum() / denom)


def r2_v_total(v_true: pd.Series, ma5_v: pd.Series, eta_hat: pd.Series) -> float:
    """面板 B: v̂ = ma5 + η̂ 对 v 总方差(绕样本均值)的 R̄²。"""
    v = v_true.astype(float)
    vhat = ma5_v.reindex(v.index).astype(float) + eta_hat.reindex(v.index).astype(float)
    denom = float(((v - v.mean()) ** 2).sum())
    if denom == 0:
        return float("nan")
    return float(1.0 - ((v - vhat) ** 2).sum() / denom)


def mse(y_true: pd.Series, y_hat: pd.Series) -> float:
    e = y_true.astype(float) - y_hat.reindex(y_true.index).astype(float)
    return float((e ** 2).mean())


# ── 分位分析(全保留) ────────────────────────────────────────────────────────

def decile_analysis(eta_true: pd.Series, eta_hat: pd.Series,
                    n: int = 10) -> pd.DataFrame:
    """按预测值分位分桶,报告各桶实现均值/命中率——预测单调性诊断。"""
    df = pd.DataFrame({"true": eta_true, "hat": eta_hat.reindex(eta_true.index)}).dropna()
    df["decile"] = pd.qcut(df["hat"].rank(method="first"), n, labels=False) + 1
    g = df.groupby("decile").agg(
        n=("true", "size"),
        mean_pred=("hat", "mean"),
        mean_real=("true", "mean"),
        hit_rate=("true", lambda s: float((np.sign(s) == np.sign(
            df.loc[s.index, "hat"])).mean())),
    )
    g["monotone_spread"] = g["mean_real"].iloc[-1] - g["mean_real"].iloc[0]
    return g


# ── knockout 重要度(全保留) ─────────────────────────────────────────────────

def feature_importance_knockout(
    model_factory: Callable[[], "object"],
    X_train: pd.DataFrame, y_train: pd.Series,
    X_test: pd.DataFrame, y_test: pd.Series,
    groups: Optional[Dict[str, List[str]]] = None,
) -> pd.DataFrame:
    """逐组置零重训,报告 OOS R²(面板 A 口径)下降。

    groups 缺省 = 按特征前缀分组(tech/fund1/fund2/cal/earn)。
    旧作 knockout 语义: 破坏该组信息后模型损失多少——置零+重训(非仅置零预测)。
    """
    from VolumePrediction.models import feature_cols, FEATURE_PREFIXES
    cols = feature_cols(X_train)
    if groups is None:
        groups = {p.rstrip("_"): [c for c in cols if c.startswith(p)]
                  for p in FEATURE_PREFIXES}
        groups = {k: v for k, v in groups.items() if v}
    base_model = model_factory().fit(X_train, y_train)
    base_r2 = oos_r2_eta(y_test, base_model.predict(X_test))
    rows = []
    for gname, gcols in groups.items():
        Xtr = X_train.copy()
        Xte = X_test.copy()
        Xtr[gcols] = 0.0
        Xte[gcols] = 0.0
        m = model_factory().fit(Xtr, y_train)
        r2 = oos_r2_eta(y_test, m.predict(Xte))
        rows.append({"group": gname, "n_features": len(gcols),
                     "r2_knockout": r2, "r2_drop": base_r2 - r2})
    out = pd.DataFrame(rows).set_index("group").sort_values("r2_drop", ascending=False)
    out.attrs["base_r2"] = base_r2
    return out


# ── 归因通道(弱点⑪) ─────────────────────────────────────────────────────────

def shap_attribution(model, X: pd.DataFrame, n_sample: int = 2000) -> pd.Series:
    """树系 SHAP(lightgbm/adaboost 的树)。shap 未装时显式抛错。"""
    import os as _os
    _os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")   # 同 ml.py 的 libomp 防护
    import shap   # 延迟 import;缺失即抛
    from VolumePrediction.models import feature_cols
    cols = feature_cols(X)
    Xv = X[cols]
    if len(Xv) > n_sample:
        Xv = Xv.sample(n_sample, random_state=0)
    est = getattr(model, "_est", model)
    explainer = shap.TreeExplainer(est)
    sv = explainer.shap_values(Xv.values)
    imp = np.abs(np.asarray(sv)).mean(axis=0)
    return pd.Series(imp, index=cols, name="shap").sort_values(ascending=False)


def nn_gradient_attribution(model, X: pd.DataFrame) -> pd.Series:
    """NN 通道: 转发到模型自带 gradient_attribution(NN2/deep 系提供)。"""
    if not hasattr(model, "gradient_attribution"):
        raise TypeError(f"{type(model).__name__} has no gradient_attribution")
    return model.gradient_attribution(X)
