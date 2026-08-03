"""富矿 ablation Group A ETL: analyst estimate 修订事件(每日快照差分)。

fmp_analyst_estimates 是每日全量快照(mini-audit 2026-08-01 实证:同一目标期
每天重插、值多不变)→ 特征 = 当日 vs 上一快照 epsAvg/revenueAvg 发生变化的
目标期数(真修订事件)。只看未来目标期(period > 快照日,≤2年,过滤 1998 类古董期)。

PIT: 事件日=create_time 日(我们何时得知);join 面板时由 join 脚本再 shift 1
交易日(与 prod_v6 tech 惯例一致)。首月(2025-01 批量灌库)整月丢弃。
输出: scratchpad/ablation/est_revisions.parquet  [symbol, day, n_rev, n_periods]
"""
import os
import sys
import time
import datetime as dt
from collections import defaultdict

import pandas as pd
from pymongo import MongoClient

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "est_revisions.parquet")
BULK_CUTOFF = dt.datetime(2025, 2, 1)   # 首月批量灌库整月丢弃

db = MongoClient(os.environ["MONGO_URI"])["someopark"]
c = db["fmp_analyst_estimates"]
symbols = sorted(c.distinct("symbol"))
print(f"symbols: {len(symbols)}", flush=True)

rows = []
t0 = time.time()
for i, sym in enumerate(symbols):
    docs = list(c.find(
        {"symbol": sym, "create_time": {"$gte": BULK_CUTOFF}},
        {"date": 1, "create_time": 1, "estimatedRevenueAvg": 1, "estimatedEpsAvg": 1},
    ))
    if not docs:
        continue
    df = pd.DataFrame(docs)
    df["day"] = pd.to_datetime(df["create_time"]).dt.normalize()
    df["period"] = pd.to_datetime(df["date"])
    # 只看快照日之后 2 年内的目标期(未来估值;剔古董期与超远期)
    df = df[(df["period"] > df["day"]) & (df["period"] <= df["day"] + pd.Timedelta(days=730))]
    if df.empty:
        continue
    df = df.sort_values(["period", "day"])
    for col in ("estimatedRevenueAvg", "estimatedEpsAvg"):
        prev = df.groupby("period")[col].shift(1)
        df[f"chg_{col}"] = prev.notna() & (df[col] != prev)
    df["changed"] = df["chg_estimatedRevenueAvg"] | df["chg_estimatedEpsAvg"]
    g = df.groupby("day").agg(n_rev=("changed", "sum"), n_periods=("period", "nunique"))
    g = g.reset_index()
    g["symbol"] = sym
    rows.append(g)
    if (i + 1) % 500 == 0:
        print(f"  {i+1}/{len(symbols)} symbols, {time.time()-t0:.0f}s", flush=True)

out = pd.concat(rows, ignore_index=True)
out.to_parquet(OUT)
print(f"DONE: {len(out):,} (symbol,day) rows → {OUT}", flush=True)
print(out["n_rev"].describe(), flush=True)
