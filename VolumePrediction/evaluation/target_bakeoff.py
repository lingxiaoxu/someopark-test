"""
target_bakeoff — 弱点⑫: 目标变量对比选型
==========================================
四个候选目标(计划 §九 P2⑫):
  1. eta         = v − ma5(v)          (shock,论文主目标)
  2. log_volume  = v                    (水平值,旧作 NN2 最优目标)
  3. sqrt_volume = sqrt(V) 的 shock 形式 = sqrt(V) − ma5(sqrt(V))
  4. quantile    = eta 的分位回归(中位数,q=0.5)——模型侧用 pinball 损失的
     GradientBoosting(quantile loss),其余模型跳过该目标(表中记 NaN 并注明)

公平比较原则: 无论在哪个目标空间训练,统一折回 **v 空间** 评估
(v̂ = 变换逆 + ma5 基),报告 v-空间 MSE 与面板 B R̄²;同时报各自空间的原生 R²。
输出: 对比表 DataFrame(target × model),结论写 config 默认由用户批(§7.4)。
"""
from __future__ import annotations

from typing import Callable, Dict, Optional

import numpy as np
import pandas as pd

from .metrics import oos_r2_eta, r2_v_total, mse


def _ma_by_ticker(s: pd.Series, window: int = 5) -> pd.Series:
    return s.groupby(level=1).transform(lambda x: x.rolling(window).mean())


def build_targets(panel: pd.DataFrame) -> Dict[str, pd.Series]:
    """从面板派生四目标(全部 PIT 安全:仅用当行及组内历史)。"""
    v = panel["v"].astype(float)
    V = panel["V"].astype(float)
    sqrtV = np.sqrt(V)
    ma5_sqrt = _ma_by_ticker(sqrtV.groupby(level=1).shift(0), 5)
    return {
        "eta": panel["eta"].astype(float),
        "log_volume": v,
        "sqrt_shock": (sqrtV - ma5_sqrt),
        "quantile_eta": panel["eta"].astype(float),   # 同 eta 数据,损失不同
    }


def _to_v_space(target: str, yhat: pd.Series, panel: pd.DataFrame) -> pd.Series:
    """把各目标空间的预测折回 v̂(log 美元量)空间。"""
    if target in ("eta", "quantile_eta"):
        return panel["ma5_v"] + yhat
    if target == "log_volume":
        return yhat
    if target == "sqrt_shock":
        sqrtV = np.sqrt(panel["V"].astype(float))
        ma5_sqrt = _ma_by_ticker(sqrtV, 5)
        sqrt_hat = (ma5_sqrt + yhat).clip(lower=1e-6)
        return np.log(sqrt_hat ** 2)
    raise ValueError(target)


def _quantile_model_factory():
    from sklearn.ensemble import GradientBoostingRegressor
    class _Q:
        name = "gbr_quantile_q50"
        def __init__(self):
            self._est = GradientBoostingRegressor(loss="quantile", alpha=0.5,
                                                  n_estimators=100, max_depth=3,
                                                  random_state=0)
            self._cols = None
        def fit(self, X, y):
            from VolumePrediction.models import feature_cols
            self._cols = feature_cols(X)
            self._est.fit(X[self._cols].values, y.values)
            return self
        def predict(self, X):
            return pd.Series(self._est.predict(X[self._cols].values),
                             index=X.index, name="yhat")
    return _Q()


def run_bakeoff(
    panel_train: pd.DataFrame, panel_test: pd.DataFrame,
    model_factories: Dict[str, Callable[[], object]],
    out_dir: Optional[str] = None,
) -> pd.DataFrame:
    """target × model 全交叉。quantile_eta 目标只跑 quantile 专用模型。"""
    ytr_all = build_targets(panel_train)
    yte_all = build_targets(panel_test)
    rows = []
    for tname in ("eta", "log_volume", "sqrt_shock", "quantile_eta"):
        ytr = ytr_all[tname]
        yte = yte_all[tname]
        factories = ({"gbr_quantile_q50": _quantile_model_factory}
                     if tname == "quantile_eta" else model_factories)
        # 目标派生的滚动统计会带 NaN(如 sqrt_shock 的前 4 行)——按目标 mask,
        # 训练只用有效行,评估只在有效行上(不静默填充,保持目标语义)
        tr_mask = ytr.notna()
        te_mask = yte.notna()
        for mname, fac in factories.items():
            m = fac() if not isinstance(fac, type) else fac()
            m = m.fit(panel_train[tr_mask], ytr[tr_mask])
            yhat = m.predict(panel_test[te_mask])
            yte_m = yte[te_mask]
            native_r2 = oos_r2_eta(yte_m, yhat) if "eta" in tname or "shock" in tname \
                else float(1 - ((yte_m - yhat) ** 2).sum()
                           / ((yte_m - yte_m.mean()) ** 2).sum())
            pt = panel_test[te_mask]
            vhat = _to_v_space(tname, yhat, pt)
            rows.append({
                "target": tname, "model": mname, "n_eval": int(te_mask.sum()),
                "native_r2": native_r2,
                "v_space_mse": mse(pt["v"], vhat),
                "v_space_r2_total": r2_v_total(
                    pt["v"], pt["ma5_v"], vhat - pt["ma5_v"]),
            })
    df = pd.DataFrame(rows).set_index(["target", "model"]).sort_values(
        "v_space_r2_total", ascending=False)
    if out_dir:
        from pathlib import Path
        p = Path(out_dir)
        p.mkdir(parents=True, exist_ok=True)
        df.to_csv(p / "target_bakeoff.csv")
    return df
