"""models — 五层模型库(Plan B §四/附录 A E34-41/C24-29 等)。

BaseModel: DEV_CONTRACTS 公共 API 的唯一定义处(全组模型共用)。

面板约定(DEV_CONTRACTS):
- X: pd.DataFrame, MultiIndex(date, ticker),含辅助列 V/v/ma5_v 与特征列
  (tech_/fund1_/fund2_/cal_/earn_ 前缀)
- y: pd.Series(目标,默认 eta),index 与 X 对齐
- 学习类模型只吃特征前缀列;基线类模型用辅助列(v/ma5_v)——同一 API,语义在各自 docstring
"""
from __future__ import annotations

from typing import List

import pandas as pd

# 新增特征组必须在此注册,否则 feature_cols() 静默丢弃 → 模型根本看不到
# (2026-08-02 教训: fund3_ 前缀未注册,两组 ablation 结果逐位相同才暴露)
FEATURE_PREFIXES = ("tech_", "fund1_", "fund2_", "cal_", "earn_", "intraday_")


def feature_cols(X: pd.DataFrame) -> List[str]:
    return [c for c in X.columns if c.startswith(FEATURE_PREFIXES)]


class BaseModel:
    """统一模型 API(见 DEV_CONTRACTS)。子类必须实现三方法。"""

    name: str = "base"

    def fit(self, X: pd.DataFrame, y: pd.Series) -> "BaseModel":
        raise NotImplementedError

    def predict(self, X: pd.DataFrame) -> pd.Series:
        raise NotImplementedError

    def param_count(self) -> int:
        raise NotImplementedError
