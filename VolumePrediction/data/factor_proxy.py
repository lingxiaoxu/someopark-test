"""
factor_proxy — BARRA 代理因子(附录 B 规格逐项实现)
==================================================
行业哑变量(SIC 大类);β(252d 对 SPY);SIZE(ln 市值,§6.2 三段拼接);
MOM(12-1);RESVOL(60d 特质波动);LIQ(Amihud 20d);EARNYLD(E/P);
全部横截面 z 化 + 对行业正交(Gram-Schmidt/回归取残差)。
与 BARRA 的差异在 P0_data_audit.md 留档。

SIZE 拼接(§6.2, PIT):
  2021-05 前  Polygon financials 加权股本(filing_date PIT)× 原始收盘价
  2021-05 后  fmp_share_float.outstandingShares × 原始收盘价
  2024-10 后  与 fmp_market_cap 三方对拍(P0 审计 6)
输入价格一律**原始价**(raw close)——SEC/FMP 股本为原始股数(§7.10-3)。
"""
from __future__ import annotations

from datetime import date
from typing import Dict, Iterable, List, Optional

import numpy as np
import pandas as pd

from VolumePrediction.common import get_logger
from VolumePrediction.data import inhouse_loader as ih

log = get_logger("factor_proxy")

SHARE_FLOAT_START = pd.Timestamp("2021-05-18")


# ── 市值拼接 ──────────────────────────────────────────────────────────────────
def market_cap_series(ticker: str, raw_close: pd.Series) -> pd.Series:
    """日频市值(原始价×同期 PIT 股本;三段拼接)。raw_close: DatetimeIndex→原始收盘。"""
    out = pd.Series(index=raw_close.index, dtype=float)
    # 后段: share_float 日频
    sf = ih.share_float([ticker], raw_close.index.min().date(),
                        raw_close.index.max().date())
    if not sf.empty:
        # 同票同日可有多条记录(fmp 增量写入)——不去重则 reindex 抛
        # duplicate labels,被调用侧吞掉 → 市值整票静默置空(P0 项6,2026-07-26)
        sfd = sf.sort_values("date").drop_duplicates("date", keep="last")
        s = (pd.Series(sfd["shares_out"].values,
                       index=pd.to_datetime(sfd["date"]))
               .sort_index().dropna())
        aligned = s.reindex(raw_close.index, method="ffill")
        out.loc[aligned.index] = aligned * raw_close
    # 前段: financials 加权股本(filing_date 起 ffill)
    pre_mask = out.isna()
    if pre_mask.any():
        fin = ih.shares_outstanding_series(ticker)
        if not fin.empty:
            s = (fin.set_index(pd.to_datetime(fin["filing_date"]))["shares"]
                    .sort_index())
            aligned = s.reindex(raw_close.index, method="ffill")
            fill = aligned * raw_close
            out[pre_mask] = fill[pre_mask]
    return out


# ── 风格因子(价量派生) ────────────────────────────────────────────────────────
def beta_252(ret: pd.Series, spy_ret: pd.Series) -> pd.Series:
    """滚动 252d 对 SPY 的 β。"""
    joined = pd.concat({"r": ret, "m": spy_ret}, axis=1).dropna()
    cov = joined["r"].rolling(252).cov(joined["m"])
    var = joined["m"].rolling(252).var()
    return (cov / var).reindex(ret.index)


def momentum_12_1(adj_close: pd.Series) -> pd.Series:
    """MOM 12-1: t-252→t-21 的累计收益(复权价)。"""
    return adj_close.shift(21) / adj_close.shift(252) - 1.0


def resvol_60(ret: pd.Series, spy_ret: pd.Series) -> pd.Series:
    """60d 特质波动: 对 SPY 滚动回归残差的 std(年化)。"""
    joined = pd.concat({"r": ret, "m": spy_ret}, axis=1).dropna()
    beta = joined["r"].rolling(60).cov(joined["m"]) / joined["m"].rolling(60).var()
    alpha = joined["r"].rolling(60).mean() - beta * joined["m"].rolling(60).mean()
    resid = joined["r"] - (alpha + beta * joined["m"])
    return (resid.rolling(60).std() * np.sqrt(252)).reindex(ret.index)


def amihud_20(ret: pd.Series, dollar_volume: pd.Series) -> pd.Series:
    """LIQ: Amihud 非流动性 = mean_{20d}(|r|/DollarVol) ×1e9(量纲便于 z 化)。"""
    illiq = (ret.abs() / dollar_volume.replace(0, np.nan)) * 1e9
    return illiq.rolling(20).mean()


def earnyld(ticker: str, raw_close: pd.Series) -> pd.Series:
    """E/P: TTM EPS(financials,filing PIT)÷ 原始价;年报间 ffill。"""
    recs = ih.fetch_financials(ticker)
    rows = []
    for r in recs:
        inc = (r.get("financials") or {}).get("income_statement") or {}
        eps = (inc.get("diluted_earnings_per_share") or {}).get("value") \
            or (inc.get("basic_earnings_per_share") or {}).get("value")
        fdate = r.get("filing_date")
        if eps is not None and fdate and r.get("fiscal_period") in ("FY", "TTM"):
            rows.append((pd.Timestamp(fdate), float(eps)))
    if not rows:
        return pd.Series(index=raw_close.index, dtype=float)
    eps_s = (pd.Series(dict(rows)).sort_index()
               .reindex(raw_close.index, method="ffill"))
    return eps_s / raw_close


# ── 行业哑变量与正交化 ────────────────────────────────────────────────────────
def sic_major(sic: Optional[str]) -> str:
    """SIC → 10 大类桶(division 近似,首位数字)。"""
    if not sic or not str(sic).strip():
        return "UNK"
    return f"SIC{str(sic).strip()[0]}"


def industry_dummies(symbols: Iterable[str]) -> pd.DataFrame:
    # industry_map 可含重复 symbol(多 CIK/历史条目)→ 不去重时 .get 返回 Series
    # 而非标量,sic_major 真值判断即抛错;每 symbol 保留末条(最新)
    m = (ih.industry_map().drop_duplicates("symbol", keep="last")
           .set_index("symbol")["sic"])
    rows = {s: sic_major(m.get(s)) for s in symbols}
    ser = pd.Series(rows, name="ind")
    return pd.get_dummies(ser, prefix="fund2_ind")


def cross_sectional_zscore(df: pd.DataFrame) -> pd.DataFrame:
    """逐日横截面 z 化(附录 B)。df: MultiIndex(date,ticker)×因子列。"""
    def _z(g: pd.DataFrame) -> pd.DataFrame:
        mu, sd = g.mean(), g.std(ddof=0).replace(0, np.nan)
        return (g - mu) / sd
    return df.groupby(level="date", group_keys=False).apply(_z)


def orthogonalize_to_industry(factors: pd.DataFrame,
                              dummies: pd.DataFrame) -> pd.DataFrame:
    """逐日对行业哑变量回归取残差(弱点④正交化;等价 Gram-Schmidt 对行业空间)。"""
    out = factors.copy()
    for d, g in factors.groupby(level="date"):
        tick = g.index.get_level_values("ticker")
        D = dummies.reindex(tick).fillna(0.0).values
        if D.shape[0] < D.shape[1] + 2:
            continue
        proj = D @ np.linalg.pinv(D.T @ D) @ D.T
        for col in factors.columns:
            y = g[col].values.astype(float)
            mask = ~np.isnan(y)
            if mask.sum() < D.shape[1] + 2:
                continue
            y0 = np.where(mask, y, 0.0)
            resid = y0 - proj @ y0
            vals = out.loc[g.index, col].values
            vals[mask] = resid[mask]
            out.loc[g.index, col] = vals
    return out


# ── VIF 共线诊断/筛除(P2④,与正交化/PCA 并列的第二通道) ──────────────────────
def vif_table(df: pd.DataFrame) -> pd.DataFrame:
    """逐列方差膨胀因子(VIF_j = 1/(1−R²_j),R²_j = x_j 对其余列+截距的回归 R²)。

    手写 R² 法(lstsq)——数值等价 statsmodels variance_inflation_factor 加
    截距列的标准用法(等价性有单测对拍),零额外依赖且奇异情形显式可控:
    - 完全共线(R²→1)与常量列(被截距完全解释)→ VIF=inf
    - 含 NaN 行整行剔除(listwise,与 statsmodels 使用惯例一致)
    返回 DataFrame[feature, vif],按 vif 降序。
    """
    X = df.astype(float).dropna()
    if X.shape[1] < 2:
        raise ValueError("vif_table needs >=2 columns")
    if len(X) < X.shape[1] + 2:
        raise ValueError(f"too few complete rows ({len(X)}) for {X.shape[1]} columns")
    ones = np.ones((len(X), 1))
    rows = []
    for j, col in enumerate(X.columns):
        y = X.iloc[:, j].values
        sst = float(((y - y.mean()) ** 2).sum())
        if sst <= 1e-30:                       # 常量列: 截距完全解释 → inf
            rows.append({"feature": col, "vif": np.inf})
            continue
        others = np.hstack([ones, X.drop(columns=[col]).values])
        coef, *_ = np.linalg.lstsq(others, y, rcond=None)
        sse = float(((y - others @ coef) ** 2).sum())
        r2 = 1.0 - sse / sst
        rows.append({"feature": col,
                     "vif": np.inf if r2 >= 1.0 - 1e-12 else 1.0 / (1.0 - r2)})
    return (pd.DataFrame(rows).sort_values("vif", ascending=False)
            .reset_index(drop=True))


def vif_prune(df: pd.DataFrame, threshold: float = 10.0) -> List[str]:
    """迭代 VIF 筛除: 每轮剔当前最高 VIF 列,直到全部 < threshold。

    经典 stepwise-VIF 协议(每轮重算——剔除一列后其余列 VIF 会变);每步 log
    (被剔列+其 VIF,审计可溯);返回**保留列清单**(保持 df 原列序)。
    剩两列仍互超阈值时剔其一后终止(单列 VIF 无定义)。
    """
    keep = list(df.columns)
    step = 0
    while len(keep) >= 2:
        tbl = vif_table(df[keep])
        worst = tbl.iloc[0]
        if worst["vif"] < threshold:
            break
        step += 1
        keep.remove(worst["feature"])
        log.info(f"vif_prune step {step}: drop {worst['feature']!r} "
                 f"(VIF={worst['vif']:.2f} >= {threshold}) — {len(keep)} cols left")
    log.info(f"vif_prune done: kept {len(keep)}/{df.shape[1]} cols "
             f"(threshold={threshold}, {step} dropped)")
    return keep
