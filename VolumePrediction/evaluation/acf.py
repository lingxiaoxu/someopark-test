"""
acf — C 组时序诊断(附录 A C24-29 的诊断半边;弱点⑧回灌)
========================================================
- acf_pacf: 逐票或池化的 ACF/PACF(statsmodels)。
- significant_lags: 95% CI 外的显著 lag 列表 → **直接回灌建模**(弱点⑧):
  返回值可交给 features.pipeline 追加 tech_ 滞后列,或给 ARIMA 定阶参考。
"""
from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np
import pandas as pd


def acf_pacf(series: pd.Series, nlags: int = 20) -> pd.DataFrame:
    from statsmodels.tsa.stattools import acf as _acf, pacf as _pacf
    s = series.dropna().astype(float)
    a = _acf(s, nlags=nlags, fft=True)
    p = _pacf(s, nlags=nlags)
    return pd.DataFrame({"lag": range(nlags + 1), "acf": a, "pacf": p}).set_index("lag")


def significant_lags(series: pd.Series, nlags: int = 20,
                     kind: str = "pacf") -> List[int]:
    """95% CI(±1.96/√n)外的显著 lag(不含 0)。kind=acf|pacf。"""
    tbl = acf_pacf(series, nlags=nlags)
    n = series.dropna().shape[0]
    ci = 1.96 / np.sqrt(max(n, 1))
    col = tbl[kind]
    return [int(l) for l in tbl.index if l > 0 and abs(col.loc[l]) > ci]


def panel_significant_lags(panel: pd.DataFrame, col: str = "eta",
                           nlags: int = 20, sample_tickers: Optional[int] = 50,
                           min_frac: float = 0.5, seed: int = 0) -> Dict:
    """面板级: 抽样票逐票求显著 lag,取出现频率 ≥min_frac 的 lag 交集式共识。

    返回 {"lags": [...], "per_ticker_frac": {lag: 出现比例}}——
    "lags" 可直接作为回灌特征清单(弱点⑧的正式回灌路径)。
    """
    tickers = panel.index.get_level_values(1).unique()
    if sample_tickers and len(tickers) > sample_tickers:
        rng = np.random.default_rng(seed)
        tickers = rng.choice(tickers, sample_tickers, replace=False)
    counts: Dict[int, int] = {}
    used = 0
    for tkr in tickers:
        s = panel.xs(tkr, level=1)[col]
        if s.dropna().shape[0] < 5 * nlags:
            continue
        used += 1
        for l in significant_lags(s, nlags=nlags):
            counts[l] = counts.get(l, 0) + 1
    frac = {l: c / max(used, 1) for l, c in counts.items()}
    lags = sorted(l for l, f in frac.items() if f >= min_frac)
    return {"lags": lags, "per_ticker_frac": frac, "n_tickers_used": used}
