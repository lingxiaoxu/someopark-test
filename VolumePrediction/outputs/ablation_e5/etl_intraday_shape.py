"""富矿 ablation Group C/D ETL v2: 日内量形态 + 盘后量占比(stock_data_hour)。

条形实证(2026-08-01): 整点 ET 对齐(mm=0),覆盖 4:00-19:00。
桶定义: mkt = hh∈[9,15](9 点条含 9:00-9:30 盘前小尾,可接受,量级远小于开盘);
first=h9(含开盘), last=h15(15:00-16:00), midday=h12+h13; ah = hh<9 或 hh≥16。
确定性聚合,PIT 天然干净;join 时 shift 1 交易日。
输出: scratchpad/ablation/intraday_shape.parquet
"""
import os
import time

import pandas as pd
from pymongo import MongoClient

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "intraday_shape.parquet")
db = MongoClient(os.environ["MONGO_URI"])["someopark"]
c = db["stock_data_hour"]

parts = []
t0 = time.time()
buf = []
n = 0


def flush(buf):
    ts = pd.to_datetime(pd.Series([d["t"] for d in buf]), unit="ms", utc=True) \
           .dt.tz_convert("America/New_York")
    df = pd.DataFrame({
        "symbol": [d["symbol"] for d in buf],
        "v": pd.to_numeric(pd.Series([d["v"] for d in buf]), errors="coerce").fillna(0.0),
        "day": ts.dt.normalize().dt.tz_localize(None).values,
        "hh": ts.dt.hour.values,
    })
    mkt = (df["hh"] >= 9) & (df["hh"] <= 15)
    df["b_mkt"] = df["v"].where(mkt, 0.0)
    df["b_first"] = df["v"].where(df["hh"] == 9, 0.0)
    df["b_last"] = df["v"].where(df["hh"] == 15, 0.0)
    df["b_mid"] = df["v"].where(df["hh"].isin([12, 13]), 0.0)
    df["b_ah"] = df["v"].where(~mkt, 0.0)
    df["b_nmkt"] = mkt.astype(int)
    return df.groupby(["symbol", "day"])[
        ["b_mkt", "b_first", "b_last", "b_mid", "b_ah", "b_nmkt"]].sum()


for d in c.find({}, {"symbol": 1, "t": 1, "v": 1}, batch_size=50000):
    buf.append(d)
    n += 1
    if len(buf) >= 1000000:
        parts.append(flush(buf))
        buf = []
        print(f"  {n:,} rows, {time.time()-t0:.0f}s", flush=True)
if buf:
    parts.append(flush(buf))

# 跨批合并(同 (symbol,day) 可能跨批 → 再 groupby sum)
g = pd.concat(parts).groupby(level=[0, 1]).sum()
g = g[g["b_mkt"] > 0]
out = pd.DataFrame({
    "first_hour_share": g["b_first"] / g["b_mkt"],
    "last_hour_share": g["b_last"] / g["b_mkt"],
    "midday_dry": g["b_mid"] / g["b_mkt"],
    "ah_share": g["b_ah"] / (g["b_mkt"] + g["b_ah"]),
    "n_mkt_hours": g["b_nmkt"].astype(int),
}).reset_index()
out.to_parquet(OUT)
print(f"DONE: {len(out):,} (symbol,day) rows → {OUT}", flush=True)
print(out[["first_hour_share", "last_hour_share", "midday_dry", "ah_share",
           "n_mkt_hours"]].describe(), flush=True)
