"""ingest/nowcast.py — public nowcast ingestion (PLAN §5-bis.1 口4; fills the
nowcast_vintages table that sat empty since M0).

GDPNow lives on FRED/ALFRED as series 'GDPNOW' with true vintages — the PIT-clean
route: every vintage row lands in fred_obs via the standard FredPIT pull AND is
mirrored into nowcast_vintages(source='GDPNow', target='KXGDP') keyed by quarter.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd


def _quarter_of(event_time: str) -> str:
    ts = pd.Timestamp(event_time)
    return f"{ts.year}-Q{(ts.month - 1) // 3 + 1}"


def pull_gdpnow(fred, conn) -> int:
    """Pull GDPNOW vintages via FredPIT (→ fred_obs), mirror into nowcast_vintages."""
    n_new = fred.pull("GDPNOW", observation_start="2015-01-01")
    now = datetime.now(timezone.utc).isoformat()
    rows = conn.execute(
        "SELECT event_time, value, knowledge_time FROM fred_obs WHERE sid='GDPNOW'"
    ).fetchall()
    n = 0
    for r in rows:
        conn.execute(
            "INSERT OR IGNORE INTO nowcast_vintages(source, target, event_time, value,"
            " knowledge_time, first_seen_ts) VALUES('GDPNow','KXGDP',?,?,?,?)",
            (_quarter_of(r["event_time"]), r["value"], r["knowledge_time"], now))
        n += 1
    conn.commit()
    return n_new


def latest_nowcast(conn, target: str, asof: datetime,
                   source: str = "GDPNow") -> tuple[str, float, str] | None:
    """(quarter, value, knowledge_time) of the latest PIT-visible nowcast."""
    r = conn.execute(
        "SELECT event_time, value, knowledge_time FROM nowcast_vintages WHERE source=?"
        " AND target=? AND knowledge_time<=? ORDER BY knowledge_time DESC LIMIT 1",
        (source, target, asof.isoformat())).fetchone()
    if r is None:
        return None
    return r["event_time"], float(r["value"]), r["knowledge_time"]
