"""
econ/lambda_calibration — λ 市场代理单轨校准(E11-T1;GKMSZ 差距#1)
====================================================================
背景(EXTENSION_PLAN E11 发现②): 自家 fills 轨物理不可测(participation 中位
1.5e-5 → 理论冲击 0.01bp,"impact" 字段 ±数百 bp 全是 decision→fill 漂移,
信噪 ~1:10⁴)→ λ 校准只走市场代理轨(Amihud),文献标准做法
(Kyle/Amihud/Hasbrouck)。

结构映射(与 econ/policy.lambda_of_v 同构):
    论文  λ(V) = C · V^(−γ),先验 C=0.2, γ=1(FORM_MAIN "0.2/V")
    Amihud ILLIQ_it = |ret_it| / $vol_it  ——单位美元成交的价格冲击
    截面回归  log(ILLIQ̄_i) = log(C) − γ·log($V̄_i) + ε
    → 斜率检验 γ 对 1 的偏离(λ∝1/V 形状),截距 exp(a)=C 定刻度。

纪律(与 econ/calibration.py 同族):
  - 纯函数只吃 DataFrame;文件 IO 全在 runner(__main__)。
  - PIT: 只用 asof 及之前、window 内的数据;滚动序列逐点 PIT。
  - 样本不足 → 论文先验 + calibration_source="paper_prior",绝不静默。
  - **8/15 前不接线**: 产物写 outputs/registry/lambda_calibration.json,
    生产 econ/policy 不读它;对照仅出报告。
"""
from __future__ import annotations

import math
from typing import List, Optional

import numpy as np
import pandas as pd

PAPER_C = 0.2
PAPER_GAMMA = 1.0
MIN_NAMES = 100          # 截面最少名字数(不足 → paper_prior)
MIN_OBS_PER_NAME = 60    # 每名字窗口内最少有效天数
DEFAULT_WINDOW = 252


def _stamp(source: str, asof: Optional[str] = None) -> dict:
    return {"asof": asof or pd.Timestamp.now().strftime("%Y-%m-%d"),
            "calibration_source": source}


def amihud_panel(bars: pd.DataFrame) -> pd.DataFrame:
    """日线长表 → Amihud 面板。

    输入列: date, ticker, c(收盘), v(股数量), vw(vwap)。
    输出列: date, ticker, dv(美元量=v×vw), illiq(=|c 环比|/dv)。
    环比在 ticker 内部按日期排序计算;首日/停牌断档由 pct_change 自然 NaN。
    dv<=0 或 illiq 非有限值的行剔除(不猜)。
    """
    need = {"date", "ticker", "c", "v", "vw"}
    if bars is None or not need.issubset(bars.columns):
        raise ValueError(f"amihud_panel needs columns {sorted(need)}")
    df = bars[["date", "ticker", "c", "v", "vw"]].copy()
    df = df.sort_values(["ticker", "date"])
    df["ret"] = df.groupby("ticker")["c"].pct_change()
    df["dv"] = df["v"].astype(float) * df["vw"].astype(float)
    df = df[(df["dv"] > 0) & df["ret"].notna()]
    df["illiq"] = df["ret"].abs() / df["dv"]
    df = df[np.isfinite(df["illiq"]) & (df["illiq"] > 0)]
    return df[["date", "ticker", "dv", "illiq"]].reset_index(drop=True)


def calibrate_lambda_amihud(panel: pd.DataFrame,
                            asof: str,
                            window_days: int = DEFAULT_WINDOW,
                            min_names: int = MIN_NAMES,
                            min_obs: int = MIN_OBS_PER_NAME,
                            n_tiers: int = 4) -> dict:
    """asof 时点的 PIT 截面校准(trailing window 名字级均值 → log-log OLS)。

    返回: {C, gamma, r2, n_names, window: [start, asof], tiers: {...},
           paper: {...}, deviation: {...}, **stamp}
    """
    cutoff = pd.Timestamp(asof)
    start = cutoff - pd.Timedelta(days=int(window_days * 1.6))  # 日历日换算余量
    d = panel[(pd.to_datetime(panel["date"]) <= cutoff)
              & (pd.to_datetime(panel["date"]) > start)]
    if len(d):                                   # 精确截取最后 window_days 个交易日
        days = sorted(d["date"].unique())[-window_days:]
        d = d[d["date"].isin(days)]
    g = d.groupby("ticker").agg(dv_bar=("dv", "mean"),
                                illiq_bar=("illiq", "mean"),
                                n_obs=("illiq", "size"))
    g = g[(g["n_obs"] >= min_obs) & (g["dv_bar"] > 0) & (g["illiq_bar"] > 0)]
    n = len(g)
    if n < min_names:
        return {"C": PAPER_C, "gamma": PAPER_GAMMA, "r2": None, "n_names": n,
                "window": None, "tiers": {}, "paper": {"C": PAPER_C, "gamma": PAPER_GAMMA},
                "deviation": None, **_stamp("paper_prior", asof)}
    lx = np.log(g["dv_bar"].values)
    ly = np.log(g["illiq_bar"].values)
    X = np.column_stack([np.ones(n), lx])
    beta, *_ = np.linalg.lstsq(X, ly, rcond=None)
    yhat = X @ beta
    ss_res = float(((ly - yhat) ** 2).sum())
    ss_tot = float(((ly - ly.mean()) ** 2).sum())
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    C, gamma = float(np.exp(beta[0])), float(-beta[1])

    # 流动性分层报告: 按 dv_bar 分位分层,层内实测 λ 中位 vs 拟合 λ
    tiers = {}
    q = pd.qcut(g["dv_bar"], n_tiers, labels=False, duplicates="drop")
    for t in sorted(pd.unique(q)):
        sub = g[q == t]
        dv_med = float(sub["dv_bar"].median())
        tiers[f"tier{int(t) + 1}"] = {
            "dv_median": dv_med,
            "n_names": int(len(sub)),
            "lambda_observed_median": float(sub["illiq_bar"].median()),
            "lambda_fitted": C * dv_med ** (-gamma),
            "lambda_paper": PAPER_C / dv_med,
        }
    days_used = sorted(d["date"].unique())
    return {
        "C": C, "gamma": gamma, "r2": r2, "n_names": n,
        "window": [str(days_used[0]), str(days_used[-1])],
        "tiers": tiers,
        "paper": {"C": PAPER_C, "gamma": PAPER_GAMMA},
        "deviation": {"gamma_minus_1": gamma - 1.0,
                      "logC_minus_log_paper": math.log(C) - math.log(PAPER_C)},
        **_stamp("amihud_market_proxy", asof),
    }


def rolling_calibration(panel: pd.DataFrame,
                        asofs: List[str],
                        window_days: int = DEFAULT_WINDOW) -> pd.DataFrame:
    """PIT 滚动序列: 每个 asof 独立校准(检验 C/γ 的时变稳定性)。"""
    rows = []
    for a in asofs:
        r = calibrate_lambda_amihud(panel, a, window_days=window_days)
        rows.append({"asof": a, "C": r["C"], "gamma": r["gamma"],
                     "r2": r["r2"], "n_names": r["n_names"],
                     "calibration_source": r["calibration_source"]})
    return pd.DataFrame(rows)


def s_opt_generalized(v_bar_dollars: float, mu: float,
                      C: float, gamma: float) -> float:
    """z* = μ/(μ+λ) 在广义 λ=C·V^(−γ) 下(对照报告用;生产 policy 不动)。"""
    if math.isinf(mu):
        return 1.0
    if mu <= 0:
        return 0.0
    lam = C * v_bar_dollars ** (-gamma)
    return mu / (mu + lam)


def s_curve_comparison(calib: dict,
                       mus: Optional[dict] = None,
                       v_grid: Optional[List[float]] = None) -> pd.DataFrame:
    """新旧 λ 的 s*(v̄) 对照表(验收件②)。"""
    mus = mus or {"mu_paper_prior": 1e-6, "mu_aiss_calibrated": 2.8244634881703216e-3}
    v_grid = v_grid or [10 ** e for e in range(5, 11)]
    rows = []
    for mu_name, mu in mus.items():
        for V in v_grid:
            rows.append({
                "mu": mu_name, "dollar_volume": V,
                "s_paper": s_opt_generalized(V, mu, PAPER_C, PAPER_GAMMA),
                "s_calibrated": s_opt_generalized(V, mu, calib["C"], calib["gamma"]),
            })
    df = pd.DataFrame(rows)
    df["delta"] = df["s_calibrated"] - df["s_paper"]
    return df
