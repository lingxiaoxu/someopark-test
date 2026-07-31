"""ingest/market_data.py — futures / FX / news (PLAN §5).

Sources (probed live 2026-07-27 with the production keys):
  * Futures: Polygon key has NO futures product → front-month continuous daily bars come
    from yfinance (CL=F, NG=F, RB=F, GC=F) — the same dependency this repo's stock system
    (DailySignal) uses in production; CME-sourced levels, adequate for model inputs
    (front-month anchor + realized vol). NOT a fallback: a working source for the same data.
  * FX: Polygon v2 aggs (C:EURUSD ...) — verified working.
  * News: Polygon /v2/reference/news — verified working.

PIT: only COMPLETED daily bars are stored; knowledge_time = bar date 18:00 ET
(post-settlement, conservative) for futures, 17:30 ET for FX; news = published_utc.
"""
from __future__ import annotations

import json
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
FUT_ROOTS = {"CL": "CL=F", "NG": "NG=F", "RB": "RB=F", "GC": "GC=F"}
FX_PAIRS = ["C:EURUSD", "C:USDJPY"]


def _kt(d: date, hh: int, mm: int = 0) -> str:
    return datetime.combine(d, time(hh, mm), tzinfo=ET).astimezone(timezone.utc).isoformat()


def pull_futures(conn, roots: list[str] | None = None, lookback_days: int = 900) -> int:
    import yfinance as yf
    now = datetime.now(timezone.utc).isoformat()
    today = datetime.now(ET).date()
    n = 0
    for root in roots or list(FUT_ROOTS):
        tkr = FUT_ROOTS[root]
        df = yf.Ticker(tkr).history(period=f"{lookback_days}d", interval="1d",
                                    auto_adjust=False)
        for ts, r in df.iterrows():
            d = ts.date()
            if d >= today:                      # §5-bis 口2: today's bar is incomplete
                continue
            conn.execute(
                "INSERT OR REPLACE INTO fut_daily(root, event_time, open, high, low, close,"
                " volume, knowledge_time, first_seen_ts) VALUES(?,?,?,?,?,?,?,?,"
                " COALESCE((SELECT first_seen_ts FROM fut_daily WHERE root=? AND event_time=?), ?))",
                (root, d.isoformat(), float(r["Open"]), float(r["High"]), float(r["Low"]),
                 float(r["Close"]), float(r.get("Volume") or 0), _kt(d, 18, 0),
                 root, d.isoformat(), now))
            n += 1
    conn.commit()
    return n


def pull_fx(conn, api_key: str, lookback_days: int = 400) -> int:
    now = datetime.now(timezone.utc).isoformat()
    today = datetime.now(ET).date()
    a = (today - timedelta(days=lookback_days)).isoformat()
    b = (today - timedelta(days=1)).isoformat()
    n = 0
    for pair in FX_PAIRS:
        url = (f"https://api.polygon.io/v2/aggs/ticker/{pair}/range/1/day/{a}/{b}"
               f"?limit=50000&apiKey={api_key}")
        with urllib.request.urlopen(url, timeout=30) as r:
            doc = json.load(r)
        for row in doc.get("results") or []:
            d = datetime.fromtimestamp(row["t"] / 1000, tz=timezone.utc).date()
            if d >= today:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO fx_daily(pair, event_time, open, high, low, close,"
                " knowledge_time, first_seen_ts) VALUES(?,?,?,?,?,?,?,"
                " COALESCE((SELECT first_seen_ts FROM fx_daily WHERE pair=? AND event_time=?), ?))",
                (pair, d.isoformat(), row["o"], row["h"], row["l"], row["c"],
                 _kt(d, 17, 30), pair, d.isoformat(), now))
            n += 1
    conn.commit()
    return n


NEWS_QUERIES = ["federal reserve", "CPI inflation", "jobless claims", "layoffs", "oil prices"]


def pull_news(conn, api_key: str, since_hours: int = 48, limit: int = 40) -> int:
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=since_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    n = 0
    for q in NEWS_QUERIES:
        url = (f"https://api.polygon.io/v2/reference/news?limit={limit}"
               f"&published_utc.gte={since}&search={urllib.request.quote(q)}&apiKey={api_key}")
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                doc = json.load(r)
        except Exception:                                        # noqa: BLE001
            continue                                             # one query failing ≠ ingest failing
        for a in doc.get("results") or []:
            conn.execute(
                "INSERT OR IGNORE INTO news(id, published_utc, title, publisher, tickers, url,"
                " first_seen_ts) VALUES(?,?,?,?,?,?,?)",
                (a["id"], a.get("published_utc"), a.get("title"),
                 (a.get("publisher") or {}).get("name"),
                 ",".join(a.get("tickers") or []), a.get("article_url"), now.isoformat()))
            n += 1
    conn.commit()
    return n
