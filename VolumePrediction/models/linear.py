"""
linear — Tier2 线性族(附录 A: E34-41 线性部分;G4 的 ols)
=========================================================
旧作规格(§一 1.3): LassoCV .627 / PCR(5) .137 / PLS(5) .742 / FwdStepwise .700。
- OLSModel: G4 论文版——纯线性回归,无正则(论文注 14: lasso/ridge 收效不大)。
- LassoCVModel / PCRModel(5) / PLSModel(5): 旧规格起步,成分数写死默认=5。
- ForwardStepwiseModel: 贪心前向选择,按 SSE 降幅(等价 F 值排序)入选;
  复杂度控制: 每步只对未入选特征做一次最小二乘增量评估(QR 增量式),
  默认 max_features=15 —— 旧 notebook 的确切步数在 P1 逐步移植时以原文回填,
  此处为工程默认并在 registry 元数据记录(不视为规格偏离,附录 A 勾验时对齐)。

统一约定: 只吃特征前缀列(FEATURE_PREFIXES);缺列报错不静默。
"""
from __future__ import annotations

from typing import List, Optional

import numpy as np
import pandas as pd

from . import BaseModel, feature_cols


class _SkLinearBase(BaseModel):
    """sklearn 系线性模型公共骨架。"""

    def __init__(self) -> None:
        self._est = None
        self._cols: List[str] = []

    def _make(self):
        raise NotImplementedError

    def fit(self, X: pd.DataFrame, y: pd.Series) -> "_SkLinearBase":
        self._cols = feature_cols(X)
        if not self._cols:
            raise ValueError("no feature columns (tech_/fund1_/... prefixes) in panel")
        self._est = self._make()
        self._est.fit(X[self._cols].values, y.values)
        return self

    def predict(self, X: pd.DataFrame) -> pd.Series:
        yhat = self._est.predict(X[self._cols].values)
        return pd.Series(np.asarray(yhat).ravel(), index=X.index, name="eta_hat")

    def param_count(self) -> int:
        est = self._est
        if hasattr(est, "coef_"):
            coef = np.atleast_1d(np.asarray(est.coef_)).ravel()
            n = coef.size + (1 if getattr(est, "intercept_", None) is not None else 0)
            return int(n)
        return 0


class OLSModel(_SkLinearBase):
    """G4 ols: 纯线性回归;参数量 = #features + 1(截距)。"""

    name = "ols"

    def _make(self):
        from sklearn.linear_model import LinearRegression
        return LinearRegression()


class LassoCVModel(_SkLinearBase):
    name = "lassocv"

    def __init__(self, cv: int = 5) -> None:
        super().__init__()
        self.cv = cv

    def _make(self):
        from sklearn.linear_model import LassoCV
        return LassoCV(cv=self.cv, n_alphas=50, max_iter=5000)


class PCRModel(BaseModel):
    """主成分回归: StandardScaler→PCA(n)→OLS。旧规格 n=5。"""

    name = "pcr5"

    def __init__(self, n_components: int = 5) -> None:
        self.n_components = n_components
        self._pipe = None
        self._cols: List[str] = []

    def fit(self, X: pd.DataFrame, y: pd.Series) -> "PCRModel":
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler
        from sklearn.decomposition import PCA
        from sklearn.linear_model import LinearRegression
        self._cols = feature_cols(X)
        self._pipe = Pipeline([
            ("sc", StandardScaler()),
            ("pca", PCA(n_components=self.n_components)),
            ("ols", LinearRegression()),
        ])
        self._pipe.fit(X[self._cols].values, y.values)
        return self

    def predict(self, X: pd.DataFrame) -> pd.Series:
        yhat = self._pipe.predict(X[self._cols].values)
        return pd.Series(yhat.ravel(), index=X.index, name="eta_hat")

    def param_count(self) -> int:
        # PCA 载荷(n_feat×k)+回归系数(k+1)——按旧作口径只报回归端 k+1
        return self.n_components + 1


class PLSModel(BaseModel):
    """偏最小二乘。旧规格 n=5;旧成绩 .742(最佳线性)。"""

    name = "pls5"

    def __init__(self, n_components: int = 5) -> None:
        self.n_components = n_components
        self._est = None
        self._cols: List[str] = []

    def fit(self, X: pd.DataFrame, y: pd.Series) -> "PLSModel":
        from sklearn.cross_decomposition import PLSRegression
        self._cols = feature_cols(X)
        self._est = PLSRegression(n_components=self.n_components)
        self._est.fit(X[self._cols].values, y.values.reshape(-1, 1))
        return self

    def predict(self, X: pd.DataFrame) -> pd.Series:
        yhat = self._est.predict(X[self._cols].values)
        return pd.Series(np.asarray(yhat).ravel(), index=X.index, name="eta_hat")

    def param_count(self) -> int:
        return self.n_components + 1


class ForwardStepwiseModel(BaseModel):
    """前向逐步回归(贪心,按 SSE 降幅)。

    实现: 每一步对每个候选特征,在已选设计矩阵基础上解增量最小二乘,
    选 SSE 最小者入选;等价于按偏 F 值最大入选。max_features 默认 15
    (工程默认;P1 逐步移植时按旧 notebook 原值对齐并写入 registry)。
    """

    name = "fwdstep"

    def __init__(self, max_features: int = 15) -> None:
        self.max_features = max_features
        self.selected_: List[str] = []
        self._coef: Optional[np.ndarray] = None
        self._cols: List[str] = []

    def fit(self, X: pd.DataFrame, y: pd.Series) -> "ForwardStepwiseModel":
        self._cols = feature_cols(X)
        Xv = X[self._cols].values.astype(float)
        yv = y.values.astype(float)
        n = len(yv)
        chosen: List[int] = []
        remaining = list(range(Xv.shape[1]))
        ones = np.ones((n, 1))
        best_sse = float(((yv - yv.mean()) ** 2).sum())
        for _ in range(min(self.max_features, len(remaining))):
            best_j, best_j_sse = None, best_sse
            base = np.hstack([ones] + ([Xv[:, chosen]] if chosen else []))
            for j in remaining:
                design = np.hstack([base, Xv[:, [j]]])
                coef, *_ = np.linalg.lstsq(design, yv, rcond=None)
                sse = float(((yv - design @ coef) ** 2).sum())
                if sse < best_j_sse - 1e-12:
                    best_j, best_j_sse = j, sse
            if best_j is None:
                break
            chosen.append(best_j)
            remaining.remove(best_j)
            best_sse = best_j_sse
        self.selected_ = [self._cols[j] for j in chosen]
        design = np.hstack([ones, Xv[:, chosen]]) if chosen else ones
        self._coef, *_ = np.linalg.lstsq(design, yv, rcond=None)
        self._chosen_idx = chosen
        return self

    def predict(self, X: pd.DataFrame) -> pd.Series:
        Xv = X[self._cols].values.astype(float)
        design = np.hstack([np.ones((len(Xv), 1)), Xv[:, self._chosen_idx]]) \
            if self._chosen_idx else np.ones((len(Xv), 1))
        return pd.Series(design @ self._coef, index=X.index, name="eta_hat")

    def param_count(self) -> int:
        return int(len(self.selected_) + 1)
