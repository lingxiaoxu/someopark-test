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


_ZQ_MONTH = "FGHJKMNQUVXZ"                    # CME month codes Jan..Dec


# How far out to REQUEST contracts. Not how far model/fed.py may price — see
# fed._FF_MAX_MONTHS, which is capped separately and deliberately lower.
#
# This was +7 until 2026-08-27, and that number quietly cost the project its entire ZQ
# history. Two facts, both measured that day against the live feed:
#
#   * a ZQ contract is listed ~4.5 YEARS before expiry and carries daily bars for all of
#     it (ZQJ27 1087 bars back to 2022-04; ZQZ27 917 back to 2022-12), and
#   * yfinance 404s a contract the moment it expires — ZQH24/ZQZ23/ZQF25/ZQM26 all return
#     nothing at all, not "a handful of bars".
#
# So every month that passes destroys ~1000 bars permanently, and only the contracts we
# have ALREADY stored survive. Requesting the whole listed strip is the only way to bank
# it. The cost of asking for a root that does not exist is one 404 that pull_futures
# already swallows, so the range is set past the end of the strip on purpose.
#
# What this cost concretely: model/fed.py's ZQ source carries WEIGHT 0.50 — the model's
# single deepest read — and on the 40 settled KXFED events it fired on ONE. 36 failed for
# exactly one reason, "no ZQ bar for the meeting's own month". The historical KXFED
# calibration therefore measures a rule+dgs2 fallback that production does not run, and
# it cannot be repaired retroactively: the near-month contracts those chains need are
# already gone from the source. Forward capture is the only remaining move.
_ZQ_STRIP_MONTHS = 24


def _zq_contracts(today) -> dict[str, str]:
    """root 'ZQU26' → yfinance 'ZQU26.CBT' for the current month through
    +_ZQ_STRIP_MONTHS, i.e. the whole listed strip rather than the near end of it."""
    out = {}
    y, m = today.year, today.month
    for k in range(0, _ZQ_STRIP_MONTHS + 1):
        mm = (m - 1 + k) % 12 + 1
        yy = y + (m - 1 + k) // 12
        code = f"ZQ{_ZQ_MONTH[mm - 1]}{yy % 100:02d}"
        out[code] = f"{code}.CBT"
    return out


def pull_futures(conn, roots: list[str] | None = None, lookback_days: int = 900,
                 period: str | None = None) -> int:
    """Daily lane by default (900d). `period='max'` is the backfill lane.

    The 900d window was not enough for its own consumers: `model/energy.py` asks
    fut_closes for 1500 bars to build the bootstrap innovation pool and could only ever
    be handed ~750, so the pool was silently a third of the intended length. yfinance
    carries these roots back to 2000-08 (~6500 bars), so `ops.backfill --futures` pulls
    the lot once and the daily lane keeps topping up the tail.
    """
    import yfinance as yf
    now = datetime.now(timezone.utc).isoformat()
    today = datetime.now(ET).date()
    n = 0
    all_roots = {**FUT_ROOTS, **_zq_contracts(today)} if roots is None \
        else {r: FUT_ROOTS[r] for r in roots}
    for root in all_roots:
        tkr = all_roots[root]
        try:
            df = yf.Ticker(tkr).history(period=period or f"{lookback_days}d",
                                        interval="1d", auto_adjust=False)
        except Exception:                            # noqa: BLE001 — dead contract
            continue
        if df is None or df.empty:
            continue
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


_NEWS_URL = "https://api.polygon.io/v2/reference/news"

# The old code ran five `search=` queries ("federal reserve", "CPI inflation", ...) and
# believed it was pulling a macro subset. Measured 2026-08-04: Polygon SILENTLY IGNORES
# `search` on our plan — a nonsense term returns the identical id list as no search at
# all, status OK, no warning, while `ticker=` does filter. So those were five identical
# calls to the general newswire, deduped by id, and every headline the LLM tagger has
# ever seen came from an unfiltered feed ("Beyond Meat Rolls Out Breakfast Sausages" is
# a real row). One call now, filtered client-side by analysis.news_tags.is_macro, which
# is also exactly what the 2021→ backfill stores — live and history share one definition.


def _news_page(url: str, timeout: int = 60) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def _store_news(conn, articles, now_iso: str) -> int:
    from prediction_market_macro.analysis.news_tags import is_macro
    n = 0
    for a in articles:
        if not is_macro(a.get("title")):
            continue
        conn.execute(
            "INSERT OR IGNORE INTO news(id, published_utc, title, publisher, tickers, url,"
            " first_seen_ts) VALUES(?,?,?,?,?,?,?)",
            (a["id"], a.get("published_utc"), a.get("title"),
             (a.get("publisher") or {}).get("name"),
             ",".join(a.get("tickers") or []), a.get("article_url"), now_iso))
        n += 1
    return n


def pull_news(conn, api_key: str, since_hours: int = 48, limit: int = 1000) -> int:
    """Daily lane: the macro headlines published in the last `since_hours`."""
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=since_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = (f"{_NEWS_URL}?limit={limit}&order=desc&sort=published_utc"
           f"&published_utc.gte={since}&apiKey={api_key}")
    n = 0
    while url:
        try:
            doc = _news_page(url)
        except Exception:                                        # noqa: BLE001
            break                                                # degradable: keep what we got
        n += _store_news(conn, doc.get("results") or [], now.isoformat())
        nxt = doc.get("next_url")
        url = f"{nxt}&apiKey={api_key}" if nxt else None
    conn.commit()
    return n


def backfill_news(conn, api_key: str, since: str = "2021-01-01",
                  until: str | None = None, page_limit: int = 1000) -> dict:
    """Walk the whole archive from `since` forward, keeping only macro headlines.

    Depth, measured 2026-08-04 by counting the firehose year by year:

        2016  22   2017    5   2018    14   2019    45   2020    201
        2021  132,928   2022 200,000+   2023 200,000+   2024 145,756
        2025  58,601    2026  40,470 (to Aug)

    So the archive effectively starts 2021-01 — 2016-2020 hold 287 articles between
    them, which is why the default `since` is 2021 rather than the 2016-06-22 first
    article. That is 5.5 years of daily macro-news intensity, which is a real
    walk-forward sample against weekly claims and monthly CPI prints, unlike the 8 days
    (§25.3) this table held before.

    knowledge_time for news is `published_utc` itself — there is no vintage problem, an
    article is known when it is published. `first_seen_ts` honestly records that we
    backfilled it today; nothing may filter on that column.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    url = (f"{_NEWS_URL}?limit={page_limit}&order=asc&sort=published_utc"
           f"&published_utc.gte={since}&apiKey={api_key}")
    if until:
        url += f"&published_utc.lt={until}"
    seen = kept = pages = 0
    while url:
        doc = _news_page(url)
        res = doc.get("results") or []
        seen += len(res)
        kept += _store_news(conn, res, now_iso)
        pages += 1
        if pages % 20 == 0:
            conn.commit()
        nxt = doc.get("next_url")
        url = f"{nxt}&apiKey={api_key}" if nxt else None
    conn.commit()
    return {"pages": pages, "scanned": seen, "kept": kept}
