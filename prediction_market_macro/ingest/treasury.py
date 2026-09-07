"""ingest/treasury.py — same-day Treasury par yields, the source FRED's DGS* copy from.

Why (2026-09-06): FRED publishes H.15 daily rates the NEXT business day, so a Friday
print reaches fred_obs on Monday. On 2026-09-04 (NFP beat, front end repriced a September
hike) the Fed models spent the weekend on Thursday's DGS2 = 4.34 while the 2y had already
printed 4.37. Treasury posts the same curve on its own site ~15:30-17:00 ET the same day,
free, no key: https://home.treasury.gov/.../pages/xml?data=daily_treasury_yield_curve.

Design — a SECOND WRITER for the SAME series, not a new series:
  * rows land under the FRED sids (DGS2/DGS5/DGS10/DGS30) with the exact stamping
    `ingest/fred.py` uses for market sids: vintage_date = event date, knowledge_time =
    event date 18:00 ET (post-close). FRED's own row for that date, arriving a business
    day later, is then an INSERT OR IGNORE duplicate of an identical value — first writer
    wins, and the value is the same number because FRED republishes Treasury's par curve
    verbatim. Every consumer (model/fed.py's DGS2 read, features.fred_series) is
    untouched and simply sees the data on the day the existing PIT declaration already
    said it was knowable.
  * If a FRED row for the same date already exists with a DIFFERENT value, nothing is
    overwritten and a warn alert `treasury_fred_mismatch` records it — the two sources
    are supposed to agree, and disagreement is a fact worth a red flag, not a silent pick.

Refresh pulls the current and previous month (cheap; backfills any gap); the tick calls
`pull_if_due` every fire so the day's curve lands within ~15 minutes of Treasury posting.
"""
from __future__ import annotations

import re
import urllib.request
from datetime import datetime, timezone

from prediction_market_macro.ingest.fred import _knowledge_time

_URL = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/"
        "xml?data=daily_treasury_yield_curve&field_tdr_date_value_month={yyyymm}")
_UA = {"User-Agent": "someopark-macro/1.0"}
FIELDS = {"BC_2YEAR": "DGS2", "BC_5YEAR": "DGS5", "BC_10YEAR": "DGS10", "BC_30YEAR": "DGS30"}
_TOL = 0.005          # bp-level disagreement between Treasury and FRED is a flag
_ENTRY = re.compile(r"<entry>(.*?)</entry>", re.S)
_DATE = re.compile(r"<d:NEW_DATE[^>]*>(\d{4}-\d{2}-\d{2})")
_VAL = {tag: re.compile(rf"<d:{tag}[^>]*>([-0-9.]+)<") for tag in FIELDS}


def fetch(yyyymm: str) -> str:
    req = urllib.request.Request(_URL.format(yyyymm=yyyymm), headers=_UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def parse(xml_text: str) -> list[tuple[str, dict[str, float]]]:
    """[(YYYY-MM-DD, {sid: value})] for every entry carrying a date; missing tenors are
    simply absent from the dict (Treasury drops a tenor on some dates)."""
    out = []
    for m in _ENTRY.finditer(xml_text):
        body = m.group(1)
        dm = _DATE.search(body)
        if not dm:
            continue
        vals = {}
        for tag, sid in FIELDS.items():
            vm = _VAL[tag].search(body)
            if vm:
                try:
                    vals[sid] = float(vm.group(1))
                except ValueError:
                    continue
        if vals:
            out.append((dm.group(1), vals))
    return out


def upsert(conn, entries: list[tuple[str, dict[str, float]]]) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    ins = mis = 0
    for d, vals in entries:
        for sid, v in vals.items():
            ex = conn.execute("SELECT value FROM fred_obs WHERE sid=? AND event_time=?"
                              " AND vintage_date=?", (sid, d, d)).fetchone()
            if ex is not None:
                if abs(float(ex[0]) - v) > _TOL:
                    mis += 1
                    conn.execute(
                        "INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                        (now, "warn", "treasury",
                         f"treasury_fred_mismatch:{sid}:{d}:fred={float(ex[0])}:ust={v}"))
                continue
            ins += conn.execute(
                "INSERT OR IGNORE INTO fred_obs(sid, event_time, value, vintage_date,"
                " knowledge_time, first_seen_ts) VALUES(?,?,?,?,?,?)",
                (sid, d, v, d, _knowledge_time(sid, d), now)).rowcount
    conn.commit()
    return {"inserted": ins, "mismatch": mis,
            "latest": max((d for d, _ in entries), default=None)}


def pull(conn, months: list[str] | None = None, fetcher=fetch) -> dict:
    now = datetime.now(timezone.utc)
    if months is None:
        first = now.replace(day=1)
        prev = (first.replace(day=1) - __import__("datetime").timedelta(days=1))
        months = [prev.strftime("%Y%m"), now.strftime("%Y%m")]
    entries: list = []
    for ym in months:
        entries += parse(fetcher(ym))
    return upsert(conn, entries)


def due(conn, now: datetime) -> bool:
    """Weekday, after 19:45 UTC (Treasury posts ~15:30-17:00 ET), and today's DGS2 not
    yet stored. Cheap enough to ask on every tick fire."""
    if now.weekday() >= 5 or (now.hour, now.minute) < (19, 45):
        return False
    today = now.date().isoformat()
    r = conn.execute("SELECT 1 FROM fred_obs WHERE sid='DGS2' AND event_time=?",
                     (today,)).fetchone()
    return r is None


def pull_if_due(conn, now: datetime | None = None, fetcher=fetch) -> dict | None:
    now = now or datetime.now(timezone.utc)
    if not due(conn, now):
        return None
    return pull(conn, [now.strftime("%Y%m")], fetcher=fetcher)
