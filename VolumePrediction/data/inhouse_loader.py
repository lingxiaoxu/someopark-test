"""
inhouse_loader — 我方既有库只读接入(§6.1/§6.5/§6.8 + A16)
=========================================================
- Mongo(someopark 库,MONGO_URI): 盈余意外/行业/三大报表(年报,acceptedDate PIT)/
  股本/市值对拍/stock_data 对拍——**日期查询一律 datetime 对象**(字符串静默 0 条,
  §6.5 工程要点;本模块所有查询经 _dt() 强制转换并有单测)
- 报表主源升级(§6.8): Polygon financials(vX,含退市+季度,PIT=filing_date)
  → fetch_financials() 自有缓存 price_data/volume_prediction/financials/
- FRED: fredapi + .env key;A16 fred_merge 的 72 系列精确清单在 config.fred.series
- SUE(§6.8): 幸存者掩码特征——actual/estimated 仅 FMP 有;(actual−est)/price 标准化
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import pandas as pd
import requests

from VolumePrediction.common import REPO, DATA_ROOT, load_config, get_logger

log = get_logger("inhouse")
FIN_DIR = DATA_ROOT / "financials"


# ── Mongo 基础 ────────────────────────────────────────────────────────────────
def _mongo():
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env")
    from pymongo import MongoClient
    # socketTimeoutMS 必设: pymongo 默认读无超时,死连接游标读永久阻塞(2026-07-24 排障)
    return MongoClient(os.environ["MONGO_URI"], serverSelectionTimeoutMS=10000,
                       socketTimeoutMS=60000, connectTimeoutMS=20000)["someopark"]


def _dt(x) -> datetime:
    """任何日期表达 → datetime(BSON 查询强制类型;见 §6.5)。"""
    if isinstance(x, datetime):
        return x
    if isinstance(x, date):
        return datetime.combine(x, datetime.min.time())
    return datetime.fromisoformat(str(x)[:10])


# ── 盈余意外(SUE, fund-1;幸存者掩码) ─────────────────────────────────────────
def earnings_surprises(symbols: Iterable[str], start, end) -> pd.DataFrame:
    """[symbol,date,actual,estimated] 长表;仅现存 ~2,660 票有值(§6.7)。"""
    col = _mongo()["fmp_earnings_surprises"]
    cur = col.find({"symbol": {"$in": list(symbols)},
                    "date": {"$gte": _dt(start), "$lte": _dt(end)}},
                   {"symbol": 1, "date": 1, "actualEarningResult": 1,
                    "estimatedEarning": 1})
    rows = [(d["symbol"], d["date"].date(), d.get("actualEarningResult"),
             d.get("estimatedEarning")) for d in cur]
    return pd.DataFrame(rows, columns=["symbol", "date", "actual", "estimated"])


# ── 行业分类(SIC;fund-2 行业哑变量/分层) ─────────────────────────────────────
def industry_map() -> pd.DataFrame:
    """symbol → sicCode/industryTitle(静态映射,PIT 局限已档 §6.1)。"""
    col = _mongo()["fmp_industry_classification"]
    rows = [(d.get("symbol"), d.get("sicCode"), d.get("industryTitle"))
            for d in col.find({}, {"symbol": 1, "sicCode": 1, "industryTitle": 1})]
    return pd.DataFrame(rows, columns=["symbol", "sic", "industry"]).dropna(subset=["symbol"])


def sic_from_polygon(ticker: str) -> Optional[str]:
    """退市股 SIC 兜底: Polygon reference ticker details(§6.8)。"""
    from VolumePrediction.data.polygon_loader import _api_key
    try:
        r = requests.get(f"https://api.polygon.io/v3/reference/tickers/{ticker}",
                         params={"apiKey": _api_key()}, timeout=15)
        if r.status_code == 200:
            return (r.json().get("results") or {}).get("sic_code")
    except Exception:  # noqa: BLE001
        pass
    return None


# ── 年报三表(现存票;acceptedDate PIT;balance 去重=P0 审计 9) ─────────────────
def annual_statement(collection: str, symbols: Iterable[str],
                     fields: List[str]) -> pd.DataFrame:
    """fmp_{income,balance_sheet,cash_flow}_statement 读取;
    (symbol,date) 取 create_time 最新去重;附 acceptedDate。"""
    col = _mongo()[collection]
    proj = {f: 1 for f in fields}
    proj.update({"symbol": 1, "date": 1, "acceptedDate": 1, "create_time": 1})
    cur = col.find({"symbol": {"$in": list(symbols)}}, proj)
    df = pd.DataFrame(list(cur))
    if df.empty:
        return df
    df = df.sort_values("create_time").drop_duplicates(["symbol", "date"], keep="last")
    df = df.drop(columns=["_id", "create_time"], errors="ignore")
    return df.reset_index(drop=True)


# ── 股本/市值(§6.2 拼接的后段与对拍段) ───────────────────────────────────────
def share_float(symbols: Iterable[str], start, end) -> pd.DataFrame:
    col = _mongo()["fmp_share_float"]
    # 索引为 (is_deleted, symbol, date) 复合——必须带前缀字段才走 IXSCAN;
    # 全集合 is_deleted 均为 None(2026-07-24 实测),不带则 COLLSCAN 400 万行
    # 且服务器逐文档评估 $in(4933) → 龟速挂死数小时
    cur = col.find({"is_deleted": None,
                    "symbol": {"$in": list(symbols)},
                    "date": {"$gte": _dt(start), "$lte": _dt(end)}},
                   {"symbol": 1, "date": 1, "outstandingShares": 1, "floatShares": 1})
    rows = [(d["symbol"], d["date"].date(), d.get("outstandingShares"),
             d.get("floatShares")) for d in cur]
    df = pd.DataFrame(rows, columns=["symbol", "date", "shares_out", "float_shares"])
    # Mongo 里两列是字符串(2026-07-25 P0 审计项6发现) —— 不转数值则下游
    # 股本×价格抛 TypeError,被调用侧 try/except 吞掉 → 市值整票静默置空
    for c in ("shares_out", "float_shares"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def market_cap(symbols: Iterable[str], start, end) -> pd.DataFrame:
    col = _mongo()["fmp_market_cap"]
    cur = col.find({"symbol": {"$in": list(symbols)},
                    "date": {"$gte": _dt(start), "$lte": _dt(end)}},
                   {"symbol": 1, "date": 1, "marketCap": 1})
    rows = [(d["symbol"], d["date"].date(), d.get("marketCap")) for d in cur]
    return pd.DataFrame(rows, columns=["symbol", "date", "market_cap"])


def stock_data_reference(symbols: Iterable[str], start, end) -> pd.DataFrame:
    """对拍源 stock_data(Polygon 同构)。t=毫秒 epoch(2026-07-25 实测全量毫秒,
    修正 §6.5 的"秒"记载;防御性双单位兼容)。"""
    col = _mongo()["stock_data"]
    t0, t1 = _dt(start).timestamp() * 1000, (_dt(end).timestamp() + 86400) * 1000
    cur = col.find({"symbol": {"$in": list(symbols)},
                    "t": {"$gte": t0, "$lt": t1}},
                   {"symbol": 1, "t": 1, "v": 1, "vw": 1, "c": 1})
    def _d(t):
        t = float(t)
        return datetime.utcfromtimestamp(t / 1000 if t > 1e11 else t).date()
    rows = [(d["symbol"], _d(d["t"]), d.get("v"), d.get("vw"), d.get("c"))
            for d in cur]
    return pd.DataFrame(rows, columns=["symbol", "date", "v", "vw", "c"])


# ── Polygon financials(报表主源升级,§6.8;含退市+季度;PIT=filing_date) ────────
def fetch_financials(ticker: str, limit: int = 60,
                     force: bool = False) -> List[dict]:
    """单票财报(SEC 标准化);自有缓存 financials/{T}.json;原始档案不可变。"""
    FIN_DIR.mkdir(parents=True, exist_ok=True)
    p = FIN_DIR / f"{ticker}.json"
    today = str(date.today())
    if p.exists() and not force:
        try:
            c = json.loads(p.read_text())
            if c.get("fetched_at", "")[:7] == today[:7]:   # 月度新鲜度足够(年报/季报)
                return c["results"]
        except Exception:  # noqa: BLE001
            pass
    from VolumePrediction.data.polygon_loader import _api_key, _sanitize
    results, url = [], "https://api.polygon.io/vX/reference/financials"
    params = {"ticker": ticker, "limit": min(limit, 100), "apiKey": _api_key()}
    try:
        while url and len(results) < limit:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(2)
                continue
            r.raise_for_status()
            body = r.json()
            results.extend(body.get("results") or [])
            url = body.get("next_url")
            params = {"apiKey": _api_key()}
    except Exception as e:  # noqa: BLE001
        log.warning(_sanitize(f"financials fetch {ticker}: {e}"))
        if not results and p.exists():
            return json.loads(p.read_text())["results"]
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"fetched_at": today, "results": results}))
    tmp.rename(p)
    return results


def shares_outstanding_series(ticker: str) -> pd.DataFrame:
    """[date(filing PIT), shares] —— SIZE 拼接前段(§6.2);weighted_average_shares。"""
    recs = fetch_financials(ticker)
    rows = []
    for r in recs:
        fin = r.get("financials") or {}
        inc = fin.get("income_statement") or {}
        sh = (inc.get("basic_average_shares") or {}).get("value") \
            or (inc.get("diluted_average_shares") or {}).get("value")
        fdate = r.get("filing_date") or r.get("acceptance_datetime", "")[:10]
        if sh and fdate:
            rows.append((date.fromisoformat(fdate[:10]), float(sh),
                         r.get("fiscal_period"), r.get("fiscal_year")))
    df = pd.DataFrame(rows, columns=["filing_date", "shares", "period", "fy"])
    return df.sort_values("filing_date").drop_duplicates("filing_date", keep="last")


# ── FRED(A16;72 系列精确清单) ────────────────────────────────────────────────
def fred_macro(start: str = "2019-01-01", end: Optional[str] = None,
               series: Optional[List[str]] = None) -> pd.DataFrame:
    """72 列日频宏观(outer-merge + ffill,旧作语义);列名=系列 id。"""
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env")
    from fredapi import Fred
    fred = Fred(api_key=os.environ["FRED_API_KEY"])
    series = series or load_config()["fred"]["series"]
    frames = {}
    for sid in series:
        try:
            s = fred.get_series(sid, observation_start=start, observation_end=end)
            frames[sid] = s
        except Exception as e:  # noqa: BLE001
            log.warning(f"FRED {sid} failed: {e}")
            frames[sid] = pd.Series(dtype=float)
        time.sleep(0.05)
    df = pd.DataFrame(frames)
    df.index.name = "date"
    return df.sort_index().ffill()
