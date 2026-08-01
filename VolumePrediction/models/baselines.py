"""
baselines — Tier1 基线 + ARIMA/SARIMA 族(附录 A: B17-23 基线部分 + C24-29)
==========================================================================
目标空间约定: y = eta = v − ma5(v)。

- MA5Model:    η̂ ≡ 0(即 v̂ = ma5)。论文的归一化零点(表 2 中 ma5 = 0%)。
- PrevDayModel: v̂ = v_{t-1} → η̂ = v_{t-1} − ma5_t。需要辅助列 v_lag1 与 ma5_v;
  若面板未提供 v_lag1,由 (date,ticker) 面板自行 groupby shift 生成(PIT 安全:仅用过去)。
- ARIMAPerTicker / SARIMAPerTicker: 旧作 C24-29 的工程化——**逐票**在训练段拟合,
  测试段用 statsmodels `res.apply(测试序列)` 做真正的滚动一步前瞻(不重估参数,
  与旧 notebook 的"训练一次、逐日一步预测"语义一致)。
  收敛/拟合失败: 记录到 self.failures 并对该票回退 MA5(η̂=0)——这是旧作行为的
  工程化(旧 notebook 手动跳过失败票),失败计数在 report() 上报,不静默。
"""
from __future__ import annotations

import warnings
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

from . import BaseModel


def _ensure_multiindex(X: pd.DataFrame) -> None:
    if not isinstance(X.index, pd.MultiIndex) or X.index.nlevels != 2:
        raise ValueError("panel must have MultiIndex (date, ticker)")


class MA5Model(BaseModel):
    """η̂ ≡ 0(v̂ = ma5)。零参数;所有 OOS R²(面板 A)的 0% 基准。"""

    name = "ma5"

    def fit(self, X: pd.DataFrame, y: pd.Series) -> "MA5Model":
        _ensure_multiindex(X)
        return self

    def predict(self, X: pd.DataFrame) -> pd.Series:
        return pd.Series(0.0, index=X.index, name="eta_hat")

    def param_count(self) -> int:
        return 0


class PrevDayModel(BaseModel):
    """v̂ = lag1(v) → η̂ = v_lag1 − ma5_v。

    论文基准表中 lag1 对 v 的 R²=92.53%(方向对照);对 η 而言其 R² 为负
    (论文表 1: PrevDay −0.637 量级——旧作数值),单测只验方向与对齐。
    """

    name = "prevday"

    def fit(self, X: pd.DataFrame, y: pd.Series) -> "PrevDayModel":
        _ensure_multiindex(X)
        return self

    @staticmethod
    def _v_lag1(X: pd.DataFrame) -> pd.Series:
        if "v_lag1" in X.columns:
            return X["v_lag1"]
        if "v" not in X.columns:
            raise ValueError("PrevDayModel needs column 'v' (or precomputed 'v_lag1')")
        # (date,ticker) 面板 → 按 ticker 组内 shift(仅用过去,PIT 安全)
        return X["v"].groupby(level=1).shift(1)

    def predict(self, X: pd.DataFrame) -> pd.Series:
        if "ma5_v" not in X.columns:
            raise ValueError("PrevDayModel needs column 'ma5_v'")
        eta_hat = self._v_lag1(X) - X["ma5_v"]
        return eta_hat.fillna(0.0).rename("eta_hat")   # 首日无 lag → 回退 ma5(η̂=0)

    def param_count(self) -> int:
        return 0


class _ArimaFamilyBase(BaseModel):
    """逐票 ARIMA 族公共骨架。子类给 order/seasonal_order。"""

    order: Tuple[int, int, int] = (1, 0, 1)
    seasonal_order: Optional[Tuple[int, int, int, int]] = None
    min_train_obs: int = 60          # 旧作小样本下限;不足即记失败回退

    def __init__(self) -> None:
        self._results: Dict[str, object] = {}       # ticker -> fitted results
        self.failures: Dict[str, str] = {}          # ticker -> reason

    # -- BaseModel ---------------------------------------------------------
    def fit(self, X: pd.DataFrame, y: pd.Series) -> "_ArimaFamilyBase":
        from statsmodels.tsa.statespace.sarimax import SARIMAX
        _ensure_multiindex(X)
        self._results.clear()
        self.failures.clear()
        for tkr, y_t in y.groupby(level=1):
            series = y_t.droplevel(1).sort_index().astype(float)
            if series.dropna().shape[0] < self.min_train_obs:
                self.failures[tkr] = f"insufficient_obs<{self.min_train_obs}"
                continue
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    mod = SARIMAX(
                        series, order=self.order,
                        seasonal_order=self.seasonal_order or (0, 0, 0, 0),
                        enforce_stationarity=False, enforce_invertibility=False,
                    )
                    res = mod.fit(disp=False, maxiter=100)
                if not np.all(np.isfinite(res.params)):
                    raise ValueError("non-finite params")
                self._results[tkr] = res
            except Exception as e:  # noqa: BLE001 — 记录并回退,旧作语义
                self.failures[tkr] = type(e).__name__
        return self

    def predict(self, X: pd.DataFrame) -> pd.Series:
        """测试段一步前瞻: res.apply(该票测试序列) 后取 predicted_mean。

        apply 用训练所得参数在新数据上滚动过滤 → 每个 t 的预测只用 ≤t-1 信息
        (statsmodels 状态空间一步预测),无前视。
        """
        if "eta" in X.columns:
            obs_col = "eta"
        elif "v" in X.columns and "ma5_v" in X.columns:
            obs_col = None
        else:
            raise ValueError("ARIMA predict needs 'eta' (or v+ma5_v) in panel for filtering")
        out = pd.Series(0.0, index=X.index, name="eta_hat")
        for tkr, X_t in X.groupby(level=1):
            res = self._results.get(tkr)
            if res is None:
                continue                      # 失败票回退 η̂=0(=MA5)
            series = (X_t[obs_col] if obs_col else (X_t["v"] - X_t["ma5_v"]))
            series = series.droplevel(1).sort_index().astype(float)
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    applied = res.apply(series, refit=False)
                    pred = applied.get_prediction().predicted_mean
                pred.index = X_t.index
                out.loc[X_t.index] = pred.values
            except Exception as e:  # noqa: BLE001
                self.failures[tkr] = f"predict:{type(e).__name__}"
        return out

    def param_count(self) -> int:
        return int(sum(len(r.params) for r in self._results.values()))

    def report(self) -> dict:
        return {"fitted": len(self._results), "failed": len(self.failures),
                "failures": dict(self.failures)}


class ARIMAPerTicker(_ArimaFamilyBase):
    """C24-27: ARIMA(1,0,1) 逐票(旧作阶数起点)。"""

    name = "arima"
    order = (1, 0, 1)


class SARIMAPerTicker(_ArimaFamilyBase):
    """C28-29: SARIMA(1,0,1)×(1,0,1,5) 周内季节(交易周=5)。"""

    name = "sarima"
    order = (1, 0, 1)
    seasonal_order = (1, 0, 1, 5)
