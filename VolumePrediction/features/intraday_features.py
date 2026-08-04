"""features/intraday_features — 日内量分布形态特征(E5,2026-08-04 上线)。

数据源(只读): `someopark.stock_data_hour`(小时线)。

**可服务窗口的硬约束**(2026-08-03 查清,决定了整个特征集的形状):
采集任务 `apps.stock.tasks:run_update_stock_data_hour` 跑在 **14:05 ET 盘中**,
所以每天只收到 13:00 那根条,14:00/15:00 两根尾盘条**永远缺**。
因此本模块只用 **4:00-13:00 ET** 的条 —— 训练与服务同窗。即使 2025 上半年
的历史数据有完整 7 小时,也一律砍到同一窗口;否则训练看得见尾盘、服务看不见,
是另一种形式的口径不一致。尾盘右肩(last_hour_share)已放弃,除非采集时间改到
16:30 ET 之后。

特征(全部在 4-13 窗内可算):
  intraday_first_hour_share  h9 / Σ(9..13)         开盘时段量占比(U 形左肩)
  intraday_midday_dry        (h12+h13) / Σ(9..13)  午盘干涸度
  intraday_premkt_share      Σ(4..8) / Σ(4..13)    盘前量占比
  intraday_morning_hhi       Σ((h_i/Σ)²), i=9..13  早盘量集中度
  intraday_open_midday_ratio log1p(h9) − log1p(h12+h13)  开盘/午盘强度(对数比,重尾稳健)
质量门: h9..h13 五根全在(n_mkt5==5);不满足则该 (票,日) 不出特征。

实证(12 窗 walk-forward,lgbm,2026-08-04): 覆盖期四窗 ΔR² 全正,
平均 +0.0016;无覆盖窗 ΔR² 精确 0.000000(join 与 NaN 处理零泄漏)。

PIT: 特征由当日 4:00-13:00 的已成交量派生;面板 A14 阶段与 tech_ 同步下移
一日,故行 T 用的是 T-1 的日内形态,无前视。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger("VolumePrediction.intraday_features")

MKT_HOURS = [9, 10, 11, 12, 13]      # 今天可得的盘中小时(ET 整点条)
PRE_HOURS = [4, 5, 6, 7, 8]          # 盘前
PREFIX = "intraday_"
FEATURES = [f"{PREFIX}{n}" for n in
            ("first_hour_share", "midday_dry", "premkt_share",
             "morning_hhi", "open_midday_ratio")]


def _flush(buf: list) -> pd.DataFrame:
    ts = (pd.to_datetime(pd.Series([d["t"] for d in buf]), unit="ms", utc=True)
          .dt.tz_convert("America/New_York"))
    df = pd.DataFrame({
        "symbol": [d["symbol"] for d in buf],
        "v": pd.to_numeric(pd.Series([d["v"] for d in buf]),
                           errors="coerce").fillna(0.0),
        "day": ts.dt.normalize().dt.tz_localize(None).values,
        "hh": ts.dt.hour.values,
    })
    for h in MKT_HOURS:
        df[f"h{h}"] = df["v"].where(df["hh"] == h, 0.0)
    df["pre"] = df["v"].where(df["hh"].isin(PRE_HOURS), 0.0)
    df["n_mkt5"] = df["hh"].isin(MKT_HOURS).astype(int)
    cols = [f"h{h}" for h in MKT_HOURS] + ["pre", "n_mkt5"]
    return df.groupby(["symbol", "day"])[cols].sum()


def intraday_shape(start: Optional[str] = None, end: Optional[str] = None,
                   symbols: Optional[set] = None,
                   mongo_uri: Optional[str] = None,
                   batch: int = 1_000_000) -> pd.DataFrame:
    """(symbol, day) → 五个日内形态特征。质量门未过的 (票,日) 不出现在结果中。"""
    from pymongo import MongoClient

    uri = mongo_uri or os.environ.get("MONGO_URI")
    if not uri:
        raise RuntimeError("MONGO_URI 未设置(先 source .env)")
    col = MongoClient(uri)["someopark"]["stock_data_hour"]

    q: dict = {}
    if start:
        q.setdefault("t", {})["$gte"] = int(pd.Timestamp(start, tz="America/New_York")
                                            .timestamp() * 1000)
    if end:
        q.setdefault("t", {})["$lte"] = int((pd.Timestamp(end, tz="America/New_York")
                                             + pd.Timedelta(days=1)).timestamp() * 1000)
    if symbols:
        q["symbol"] = {"$in": sorted(symbols)}

    parts, buf, n, t0 = [], [], 0, time.time()
    for d in col.find(q, {"symbol": 1, "t": 1, "v": 1}, batch_size=50000):
        buf.append(d)
        n += 1
        if len(buf) >= batch:
            parts.append(_flush(buf))
            buf = []
            log.info(f"intraday_shape: {n:,} rows, {time.time() - t0:.0f}s")
    if buf:
        parts.append(_flush(buf))
    if not parts:
        log.warning("intraday_shape: 无小时线数据")
        return pd.DataFrame(columns=["symbol", "day"] + FEATURES)

    g = pd.concat(parts).groupby(level=[0, 1]).sum()
    hcols = [f"h{h}" for h in MKT_HOURS]
    mkt = g[hcols].sum(axis=1)
    keep = (mkt > 0) & (g["n_mkt5"] == len(MKT_HOURS))     # 质量门
    n_drop = int((~keep).sum())
    g, mkt = g[keep], mkt[keep]
    share = g[hcols].div(mkt, axis=0)
    mid = g["h12"] + g["h13"]
    out = pd.DataFrame({
        f"{PREFIX}first_hour_share": g["h9"] / mkt,
        f"{PREFIX}midday_dry": mid / mkt,
        f"{PREFIX}premkt_share": g["pre"] / (mkt + g["pre"]),
        f"{PREFIX}morning_hhi": (share ** 2).sum(axis=1),
        f"{PREFIX}open_midday_ratio": np.log1p(g["h9"]) - np.log1p(mid),
    }).reset_index()
    log.info(f"intraday_shape: {len(out):,} (票,日) 出特征, 质量门滤除 {n_drop:,}")
    return out


def add_intraday_features(panel: pd.DataFrame,
                          shape: Optional[pd.DataFrame] = None,
                          **kw) -> pd.DataFrame:
    """把日内形态并入面板((date,ticker) MultiIndex);缺失留 NaN 交由 A11 填充。

    对齐: shape 的 day == 面板的 date(同一交易日的日内形态);面板 A14 阶段
    会与 tech_ 一同下移一日 → 行 T 实际用 T-1 的形态,无前视。
    """
    if shape is None:
        dates = panel.index.get_level_values("date")
        shape = intraday_shape(start=str(dates.min().date()),
                               end=str(dates.max().date()),
                               symbols=set(panel.index.get_level_values("ticker")),
                               **kw)
    if shape.empty:
        log.warning("add_intraday_features: 无数据,面板不变")
        return panel
    s = shape.rename(columns={"symbol": "ticker", "day": "date"})
    s["date"] = pd.to_datetime(s["date"])
    s = s.set_index(["date", "ticker"])
    out = panel.copy()
    for c in FEATURES:
        out[c] = s[c].reindex(out.index).values
    cov = float(out[FEATURES[0]].notna().mean())
    log.info(f"add_intraday_features: {len(FEATURES)} 列, 覆盖率 {cov:.2%}")
    return out


def open_auction_share(*args, **kwargs):
    """开盘集合竞价占比 —— 需要分钟线,当前数据源(小时线)无法拆分,未实现。"""
    raise NotImplementedError("需要分钟线数据;stock_data_hour 只有小时粒度")
