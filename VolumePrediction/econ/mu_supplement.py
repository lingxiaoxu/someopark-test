"""
econ/mu_supplement — μ 补两腿(E11-T2;GKMSZ 差距#2)
=====================================================
① pairs: per-pair HL 未落盘(oos_pair_summary/pair_universe 均无此列)→
   对当前 universe 价差序列重算 OU 半衰期(与 `PortfolioClasses.Half_Life`
   同数学: OLS Δs ~ s_lag → HL=−ln2/β,门槛 1<HL<42 同类),
   解析保留曲线 R(t)=exp(−θt), θ=ln2/HL —— 直接喂现有
   calibrate_mu_momentum(口径=初段日均衰减幅度,与 aiss 同族同量纲)。
② ssrs: sr_daily_report 目录只有 1 个历史文件 → 曲线无信号。修法=从自有
   raw grouped bars 的行业 ETF 价格(2017-07+)合成动量 score 面板,
   喂 calibrate_mu.decay_curve_from_panel(与日报通路同一套事件/断裂/加权
   逻辑),再喂同一个 calibrate_mu_momentum。

纪律: 纯函数只吃 DataFrame;文件 IO 全在 runner(calibrate_mu_supplement)。
"""
from __future__ import annotations

import math
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

# 与 PortfolioClasses.Half_Life 同门槛(hl_min/hl_max/look_back)
HL_MIN, HL_MAX, HL_LOOKBACK = 1.0, 42.0, 43
SPREAD_BETA_LOOKBACK = 126
SECTOR_ETFS = ["XLB", "XLC", "XLE", "XLF", "XLI", "XLK",
               "XLP", "XLRE", "XLU", "XLV", "XLY"]


def ou_half_life(series: pd.Series | np.ndarray) -> Optional[float]:
    """OU 半衰期(与 PortfolioClasses.Half_Life.apply_half_life 同数学):
    OLS  Δs_t = a + β·s_{t−1}  →  HL = −ln2/β;β≥0(非均值回复)→ None。"""
    x = np.asarray(series, dtype=float)
    x = x[np.isfinite(x)]
    if len(x) < 20:
        return None
    lag, ret = x[:-1], np.diff(x)
    X = np.column_stack([np.ones(len(lag)), lag])
    beta, *_ = np.linalg.lstsq(X, ret, rcond=None)
    b = float(beta[1])
    if b >= 0:
        return None
    return float(-math.log(2) / b)


def pair_spread(c1: pd.Series, c2: pd.Series,
                beta_lookback: int = SPREAD_BETA_LOOKBACK) -> Optional[pd.Series]:
    """对数价差 s = log(p1) − β·log(p2),β=对齐窗口内 OLS 对冲比。"""
    df = pd.concat([c1, c2], axis=1, keys=["p1", "p2"]).dropna()
    if len(df) < max(beta_lookback // 2, HL_LOOKBACK):
        return None
    df = df.tail(beta_lookback)
    l1, l2 = np.log(df["p1"].values), np.log(df["p2"].values)
    X = np.column_stack([np.ones(len(l2)), l2])
    beta, *_ = np.linalg.lstsq(X, l1, rcond=None)
    return pd.Series(l1 - beta[1] * l2, index=df.index, name="spread")


def pairs_ou_retention(closes: pd.DataFrame,
                       pairs: List[Tuple[str, str]],
                       max_delay: int = 5,
                       hl_lookback: int = HL_LOOKBACK
                       ) -> Tuple[Optional[pd.Series], dict]:
    """当前 pair universe 的 OU 保留曲线。

    每对: 价差 → HL;门槛过滤(1<HL<42,同 PortfolioClasses.use());
    合成 θ = ln2 / median(HL);曲线 R(d)=exp(−θd), d=0..max_delay
    (index=延迟天数, value=保留的累计 alpha 比例 —— calibrate_mu_momentum
    的输入语义)。有效对 <3 → (None, diag) 交由上游降级论文先验,不猜。
    """
    hls, per_pair = [], {}
    for s1, s2 in pairs:
        if s1 not in closes.columns or s2 not in closes.columns:
            per_pair[f"{s1}/{s2}"] = {"hl": None, "reason": "missing prices"}
            continue
        sp = pair_spread(closes[s1], closes[s2])
        if sp is None:
            per_pair[f"{s1}/{s2}"] = {"hl": None, "reason": "insufficient overlap"}
            continue
        hl = ou_half_life(sp.tail(hl_lookback).values)
        ok = hl is not None and HL_MIN < hl < HL_MAX
        per_pair[f"{s1}/{s2}"] = {"hl": None if hl is None else round(hl, 2),
                                  "used": bool(ok)}
        if ok:
            hls.append(hl)
    diag = {"n_pairs": len(pairs), "n_valid_hl": len(hls),
            "hl_median": float(np.median(hls)) if hls else None,
            "hl_range": ([round(min(hls), 2), round(max(hls), 2)] if hls else None),
            "per_pair": per_pair,
            "method": "OU half-life on log-spread (PortfolioClasses.Half_Life "
                      "math), retention exp(-ln2/HL_median * d)"}
    if len(hls) < 3:
        return None, diag
    theta = math.log(2) / float(np.median(hls))
    curve = pd.Series({d: math.exp(-theta * d) for d in range(max_delay + 1)},
                      name="alpha").sort_index()
    diag["alpha_by_delay"] = {int(k): round(float(v), 6) for k, v in curve.items()}
    return curve, diag


def synthetic_momentum_panel(closes: pd.DataFrame,
                             lookback: int = 252,
                             skip: int = 21,
                             tickers: Optional[List[str]] = None) -> dict:
    """行业 ETF 价格 → 合成动量 score 面板(12-1 动量,月度尺度信号、日频落点)。

    score_i(t) = p_i(t−skip)/p_i(t−lookback) − 1(跳过近月,标准动量口径)。
    返回 {date_str: {ticker: (score, price)}} —— decay_curve_from_panel 输入。
    """
    tickers = tickers or [t for t in SECTOR_ETFS if t in closes.columns]
    px = closes[tickers].dropna(how="all").sort_index()
    panel: dict = {}
    for i in range(lookback, len(px)):
        row_now = px.iloc[i]
        row_skip = px.iloc[i - skip]
        row_lb = px.iloc[i - lookback]
        day = {}
        for t in tickers:
            p_now, p_s, p_l = row_now.get(t), row_skip.get(t), row_lb.get(t)
            if pd.notna(p_now) and pd.notna(p_s) and pd.notna(p_l) \
                    and p_now > 0 and p_l > 0:
                day[t] = (float(p_s / p_l - 1.0), float(p_now))
        if len(day) >= 3:
            d = px.index[i]
            panel[str(pd.Timestamp(d).date())] = day
    return panel
