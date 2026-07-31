"""ingest/fed_text.py — FOMC statement text ingestion (PLAN §5 'Fed 官网 + 声明文本抓取').

Statement URL convention (stable for two decades):
  https://www.federalreserve.gov/newsevents/pressreleases/monetary{YYYYMMDD}a.htm
One row per meeting period in fed_statements; idempotent (present periods skipped).
Text is tag-stripped to plain prose. Degradable: network failures alert, never raise.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import requests

_TAG = re.compile(r"<script.*?</script>|<style.*?</style>|<[^>]+>", re.S)
_WS = re.compile(r"[ \t]+")
_UA = {"User-Agent": "someopark-macro/0.1 (research; contact: local)"}


def _clean(html: str) -> str:
    txt = _TAG.sub(" ", html)
    txt = txt.replace("&nbsp;", " ").replace("&amp;", "&").replace("&quot;", '"')
    txt = _WS.sub(" ", txt)
    lines = [ln.strip() for ln in txt.splitlines()]
    body = "\n".join(ln for ln in lines if ln)
    # keep the statement body: from "For release" or first "The Federal" mention
    m = re.search(r"(For release at.*|Recent indicators.*|Information received.*"
                  r"|The Federal Open Market Committee.*)", body, re.S)
    return (m.group(1) if m else body)[:20000]


def fetch_statements(conn, lookback_days: int = 400) -> int:
    """Fetch any past FOMC meeting statement not yet stored. Returns rows added."""
    from prediction_market_macro.ingest.calendars import CALENDARS
    now = datetime.now(timezone.utc)
    n = 0
    for ev in CALENDARS["FOMC"]:
        if not (now - timedelta(days=lookback_days) <= ev.scheduled_ts <= now):
            continue
        have = conn.execute("SELECT 1 FROM fed_statements WHERE period=?",
                            (ev.period,)).fetchone()
        if have:
            continue
        day = ev.scheduled_ts.astimezone(timezone.utc)
        url = ("https://www.federalreserve.gov/newsevents/pressreleases/"
               f"monetary{day.strftime('%Y%m%d')}a.htm")
        try:
            r = requests.get(url, headers=_UA, timeout=30)
            if r.status_code != 200 or len(r.text) < 2000:
                continue
            text = _clean(r.text)
            if "Committee" not in text:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO fed_statements(period, url, text, fetched_ts)"
                " VALUES(?,?,?,?)", (ev.period, url, text, now.isoformat()))
            n += 1
        except Exception as e:                                # noqa: BLE001
            conn.execute(
                "INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                (now.isoformat(), "warn", "fed_text", f"{ev.period}: {e}"))
    conn.commit()
    return n


def latest_two(conn) -> tuple[dict, dict] | None:
    rows = conn.execute(
        "SELECT period, text FROM fed_statements ORDER BY period DESC LIMIT 2"
    ).fetchall()
    if len(rows) < 2:
        return None
    return dict(rows[0]), dict(rows[1])
