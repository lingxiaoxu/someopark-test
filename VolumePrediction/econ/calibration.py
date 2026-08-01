"""
econ/calibration — μ/λ 实测校准(Plan §7.12-2 / P2 弱点⑤修复)
=============================================================
纯函数层: 只接受 DataFrame,不读文件(数据由 service/tca 侧准备)。
输出统一 schema: {value 字段..., r2, n, asof, strategy, calibration_source}。
样本不足(n < min_samples)一律返回论文先验并显式标注——绝不静默。

μ 的量纲: losscon = λz² + μ(1−z)² 中 μ 是"单位延迟的机会成本"(每日、
以目标名义的比例计),与 λz² 的冲击成本同量纲(交易额归一后)。
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

MIN_SAMPLES = 50
PAPER_PRIOR_MU = 1.0e-6
PAPER_PRIOR_LAMBDA = {"k": 0.1, "exponent": 1.0}   # impact = 0.1×participation(Kyle)


def _stamp(strategy: str, source: str, asof: Optional[str] = None) -> dict:
    return {"strategy": strategy,
            "asof": asof or pd.Timestamp.now().strftime("%Y-%m-%d"),
            "calibration_source": source}


def calibrate_mu_pairs(signal_history: pd.DataFrame,
                       strategy: str = "pairs",
                       min_samples: int = MIN_SAMPLES,
                       asof: Optional[str] = None) -> dict:
    """pairs 剖面 μ: (信号强度, 延迟天数) → 实现收敛差 的回归斜率。

    输入列(由调用侧从 mrpt/mtfs_signals 历史构造,附录 D schema):
      z_score        : 信号强度(入场时 |z|)
      delay_days     : 假想执行延迟(0=当日成交,1=次日,…)
      realized_pnl_frac : 该延迟下实现的收敛收益(名义比例)
    μ := max(0, −slope(realized_pnl_frac ~ delay_days | 控制 z_score))
    —— 每延迟一天平均损失的名义比例 = 未成交部分的机会成本。
    """
    need = {"z_score", "delay_days", "realized_pnl_frac"}
    if signal_history is None or not need.issubset(signal_history.columns):
        return {"mu": PAPER_PRIOR_MU, "slope": None, "r2": None, "n": 0,
                **_stamp(strategy, "paper_prior")}
    df = signal_history.dropna(subset=list(need))
    n = len(df)
    if n < min_samples:
        return {"mu": PAPER_PRIOR_MU, "slope": None, "r2": None, "n": n,
                **_stamp(strategy, "paper_prior")}
    # 控制信号强度: 双变量 OLS y = a + b·delay + c·z
    X = np.column_stack([np.ones(n), df["delay_days"].values, df["z_score"].values])
    y = df["realized_pnl_frac"].values.astype(float)
    beta, res, *_ = np.linalg.lstsq(X, y, rcond=None)
    slope = float(beta[1])
    yhat = X @ beta
    ss_res = float(((y - yhat) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    mu = max(0.0, -slope)
    src = "fills_regression" if mu > 0 else "fills_regression_nonneg_floor"
    return {"mu": mu if mu > 0 else PAPER_PRIOR_MU, "slope": slope, "r2": r2, "n": n,
            **_stamp(strategy, src if mu > 0 else "paper_prior")}


def calibrate_mu_momentum(alpha_decay_curve: pd.Series,
                          strategy: str = "aiss",
                          asof: Optional[str] = None) -> dict:
    """动量类剖面 μ: alpha 保留曲线(index=延迟天数, value=保留的累计 alpha 比例)
    的初段日均衰减幅度。曲线点数 <3 → 论文先验。"""
    if alpha_decay_curve is None or len(alpha_decay_curve.dropna()) < 3:
        return {"mu": PAPER_PRIOR_MU, "decay_per_day": None, "r2": None, "n": 0,
                **_stamp(strategy, "paper_prior")}
    c = alpha_decay_curve.dropna().sort_index()
    d = -c.diff().dropna()                       # 每日衰减量(正=损失)
    head = d.iloc[: max(3, len(d) // 3)]
    mu = float(max(head.mean(), 0.0))
    return {"mu": mu if mu > 0 else PAPER_PRIOR_MU,
            "decay_per_day": float(head.mean()), "r2": None, "n": int(len(c)),
            **_stamp(strategy, "alpha_decay_curve" if mu > 0 else "paper_prior")}


def calibrate_lambda_from_fills(fills: pd.DataFrame,
                                strategy: str = "all",
                                min_samples: int = MIN_SAMPLES,
                                asof: Optional[str] = None) -> dict:
    """λ 冲击回归(Almgren-Chriss 形式): impact = k × participation^exponent。

    输入列: participation(成交额/当日美元量), impact(实现冲击,比例;
    可由 (成交价−到达价)/到达价 × side 构造,由调用侧准备)。
    log-log OLS;n<min_samples → 论文先验 k=0.1, exp=1(Kyle 线性)。
    """
    need = {"participation", "impact"}
    if fills is None or not need.issubset(fills.columns):
        return {**PAPER_PRIOR_LAMBDA, "r2": None, "n": 0,
                **_stamp(strategy, "paper_prior")}
    df = fills.dropna(subset=list(need))
    df = df[(df["participation"] > 0) & (df["impact"] > 0)]
    n = len(df)
    if n < min_samples:
        return {**PAPER_PRIOR_LAMBDA, "r2": None, "n": n,
                **_stamp(strategy, "paper_prior")}
    lx = np.log(df["participation"].values.astype(float))
    ly = np.log(df["impact"].values.astype(float))
    X = np.column_stack([np.ones(n), lx])
    beta, *_ = np.linalg.lstsq(X, ly, rcond=None)
    yhat = X @ beta
    ss_res = float(((ly - yhat) ** 2).sum())
    ss_tot = float(((ly - ly.mean()) ** 2).sum())
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return {"k": float(np.exp(beta[0])), "exponent": float(beta[1]), "r2": r2, "n": n,
            **_stamp(strategy, "fills_regression")}
