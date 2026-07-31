"""ingest/eia.py — EIA weekly natural gas storage (backlog 'EIA 库存喂 NG').

The weekly Underground Storage report (Thu 10:30 ET) is THE scheduled information
event for NG — storage surprise vs the seasonal norm moves the Friday settle our
KXNATGASW ladder prices off. FRED carries only a months-lagged monthly series, so
this uses the official EIA Open Data v2 API.

ACTIVATION: requires EIA_API_KEY in the environment (free, instant:
https://www.eia.gov/opendata/register.php). Without it this module is a no-op
that alerts once — the NG model keeps its plain-GBM path until the key appears.

Rows land in fred_obs (sid='NG_STORAGE_WEEKLY', Bcf, lower-48 total) with
knowledge_time = report week's Thursday 10:30 ET → PIT-safe backfill: the API
serves full history, and each week's kt is derived from its own period date.
"""
from __future__ import annotations

import os
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

_SERIES = "NW2_EPG0_SWO_R48_BCF.W"          # working gas, lower 48, weekly
_URL = ("https://api.eia.gov/v2/natural-gas/stor/wkly/data/"
        "?frequency=weekly&data[0]=value&facets[series][]={sid}"
        "&sort[0][column]=period&sort[0][direction]=desc&length=500&api_key={key}")
_ET = ZoneInfo("America/New_York")


def _kt_for(period_iso: str) -> str:
    """Report covering week ending Friday `period` publishes the FOLLOWING
    Thursday 10:30 ET."""
    d = datetime.fromisoformat(period_iso).date()
    days_to_thu = (3 - d.weekday()) % 7 or 7
    pub = d + timedelta(days=days_to_thu)
    return datetime.combine(pub, time(10, 30), tzinfo=_ET) \
        .astimezone(timezone.utc).isoformat()


def pull_storage(conn) -> int:
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
                 "EIA_API_KEY not set — NG storage feed dormant (free key:"
                 " eia.gov/opendata/register.php)"))
            conn.commit()
        return 0
    try:
        r = requests.get(_URL.format(sid=_SERIES, key=key), timeout=45)
        r.raise_for_status()
        rows = r.json().get("response", {}).get("data", [])
    except Exception as e:                                # noqa: BLE001
        conn.execute(
            "INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
            (now.isoformat(), "warn", "eia", f"storage pull failed: {str(e)[:140]}"))
        conn.commit()
        return 0
    n = 0
    for row in rows:
        period, val = row.get("period"), row.get("value")
        if not period or val is None:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO fred_obs(sid, event_time, value, vintage_date,"
            " knowledge_time, first_seen_ts) VALUES('NG_STORAGE_WEEKLY',?,?,?,?,?)",
            (period, float(val), period, _kt_for(period), now.isoformat()))
        n += 1
    conn.commit()
    return n
