"""ingest/aaa_daily.py — AAA DAILY national average gasoline price (the exact
number KXAAAGASW settles on; PLAN_EXTENSION backlog 'AAA 日度读数接入').

Why: the market watches gasprices.aaa.com every day; our weekly EIA proxy is up
to 6 days stale — a structural information gap no regression can close (the 7x
Brier deficit). This scraper closes it going FORWARD: one row per US day into
fred_obs (sid='AAA_DAILY') with knowledge_time = scrape instant. No history is
available for free, so the model's edge accrues as readings accumulate.

Degradable: page layout drift / 403 alerts and skips — the model falls back to
the GASREGW path automatically.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

_URL = "https://gasprices.aaa.com/"
_HDRS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.google.com/",
}
_RX = re.compile(r"Current\s+Avg.*?\$(\d\.\d{3})", re.S | re.I)


def parse_current_avg(html: str) -> float | None:
    m = _RX.search(html)
    return float(m.group(1)) if m else None


def fetch_daily(conn) -> int:
    """Scrape today's national regular average → fred_obs('AAA_DAILY'). Idempotent
    per US-Eastern date. Returns rows added (0 = already had it / failed)."""
    now = datetime.now(timezone.utc)
    et_date = now.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    have = conn.execute(
        "SELECT 1 FROM fred_obs WHERE sid='AAA_DAILY' AND event_time=?",
        (et_date,)).fetchone()
    if have:
        return 0
    try:
        r = requests.get(_URL, headers=_HDRS, timeout=30)
        r.raise_for_status()
        px = parse_current_avg(r.text)
        if px is None or not (1.0 < px < 10.0):
            raise ValueError(f"parse failed / implausible price {px}")
    except Exception as e:                                # noqa: BLE001
        conn.execute(
            "INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
            (now.isoformat(), "warn", "aaa_daily", f"scrape failed: {str(e)[:140]}"))
        conn.commit()
        return 0
    conn.execute(
        "INSERT OR IGNORE INTO fred_obs(sid, event_time, value, vintage_date,"
        " knowledge_time, first_seen_ts) VALUES('AAA_DAILY',?,?,?,?,?)",
        (et_date, px, et_date, now.isoformat(), now.isoformat()))
    conn.commit()
    return 1
