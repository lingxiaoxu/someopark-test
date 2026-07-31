"""ingest/eia.py — EIA Open Data v2 feeds (docs: eia.gov/opendata/documentation.php).

Feeds (config-driven; add a dict to FEEDS to onboard another route):
  NG_STORAGE_WEEKLY       natural-gas/stor/wkly  NW2_EPG0_SWO_R48_BCF  (Bcf, lower 48)
                          — THE weekly NG information event (Thu 10:30 ET)
  GASOLINE_STOCKS_WEEKLY  petroleum/stoc/wstk    WGTSTUS1  (kbbl, US total)
                          — supply-side input for the gasoline family (Wed 10:30 ET)

Per the v2 docs: length<=5000 per call with offset pagination; facet ids carry NO
v1 '.W' suffix (probed live against /facet/series). Requires EIA_API_KEY (root
.env; free at eia.gov/opendata/register.php) — dormant with a daily info alert
until present.

Storage, two layers:
  1. RAW MIRROR (user-mandated home): price_data/eia/<name>.json at the repo
     root — the full API payload snapshot, refreshed daily by the pipeline.
  2. PIT rows in fred_obs (sid per feed): knowledge_time = each report week's OWN
     publication instant (week ends Friday; NG publishes next Thu 10:30 ET,
     petroleum next Wed 10:30 ET) → historical backfill is PIT-safe.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

_ET = ZoneInfo("America/New_York")
_REPO_ROOT = Path(__file__).resolve().parents[2]
_MIRROR_DIR = _REPO_ROOT / "price_data" / "eia"

FEEDS = [
    {"name": "ng_storage_weekly", "route": "natural-gas/stor/wkly",
     "series": "NW2_EPG0_SWO_R48_BCF", "sid": "NG_STORAGE_WEEKLY",
     "pub_weekday": 3},                       # Thursday
    {"name": "gasoline_stocks_weekly", "route": "petroleum/stoc/wstk",
     "series": "WGTSTUS1", "sid": "GASOLINE_STOCKS_WEEKLY",
     "pub_weekday": 2},                       # Wednesday
    {"name": "crude_stocks_weekly", "route": "petroleum/stoc/wstk",
     "series": "WCESTUS1", "sid": "CRUDE_STOCKS_WEEKLY",
     "pub_weekday": 2},                       # Wednesday (same report)
]


def _kt_for(period_iso: str, pub_weekday: int) -> str:
    """Report week ends Friday `period`; publication = NEXT `pub_weekday` 10:30 ET."""
    d = datetime.fromisoformat(period_iso).date()
    days = (pub_weekday - d.weekday()) % 7 or 7
    pub = d + timedelta(days=days)
    return datetime.combine(pub, time(10, 30), tzinfo=_ET) \
        .astimezone(timezone.utc).isoformat()


def _fetch_all(route: str, series: str, key: str) -> list[dict]:
    """Full history with offset pagination (docs: length<=5000)."""
    out, offset = [], 0
    while True:
        url = (f"https://api.eia.gov/v2/{route}/data/?frequency=weekly"
               f"&data[0]=value&facets[series][]={series}"
               f"&sort[0][column]=period&sort[0][direction]=desc"
               f"&length=5000&offset={offset}&api_key={key}")
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        batch = r.json().get("response", {}).get("data", [])
        out.extend(batch)
        if len(batch) < 5000:
            return out
        offset += 5000


def pull_storage(conn) -> int:
    """Daily refresh of every FEED: raw mirror + PIT fred_obs upserts.
    Returns total new rows across feeds; degradable per feed."""
    key = os.environ.get("EIA_API_KEY")
    now = datetime.now(timezone.utc)
    if not key:
        dup = conn.execute(
            "SELECT 1 FROM alerts WHERE source='eia' AND ts>=?",
            (now.date().isoformat(),)).fetchone()
        if not dup:
            conn.execute(
                "INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                (now.isoformat(), "info", "eia",
                 "EIA_API_KEY not set — feeds dormant (free key:"
                 " eia.gov/opendata/register.php)"))
            conn.commit()
        return 0
    _MIRROR_DIR.mkdir(parents=True, exist_ok=True)
    total = 0
    for feed in FEEDS:
        try:
            rows = _fetch_all(feed["route"], feed["series"], key)
        except Exception as e:                            # noqa: BLE001
            conn.execute(
                "INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                (now.isoformat(), "warn", "eia",
                 f"{feed['name']} pull failed: {str(e)[:140]}"))
            conn.commit()
            continue
        # 1. raw mirror snapshot (user-mandated home: price_data/eia/)
        (_MIRROR_DIR / f"{feed['name']}.json").write_text(json.dumps(
            {"pulled_at": now.isoformat(), "series": feed["series"],
             "route": feed["route"], "n": len(rows), "data": rows},
            ensure_ascii=False))
        # 2. PIT rows
        n = 0
        for row in rows:
            period, val = row.get("period"), row.get("value")
            if not period or val is None:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO fred_obs(sid, event_time, value, vintage_date,"
                " knowledge_time, first_seen_ts) VALUES(?,?,?,?,?,?)",
                (feed["sid"], period, float(val), period,
                 _kt_for(period, feed["pub_weekday"]), now.isoformat()))
            n += 1
        conn.commit()
        total += n
    return total
