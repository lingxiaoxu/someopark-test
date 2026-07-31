"""ingest/fred.py — FRED/ALFRED with full PIT vintages (PLAN §5, §5-bis).

Every observation is stored per-vintage: (sid, event_time, vintage_date) with a
knowledge_time = vintage_date @ the series' official release time (ET→UTC) — this is
what makes intraday asof-filtering correct (§5-bis.1 口1). y_first (first-print labels,
§5-bis.2) come straight from the earliest vintage per event_time.

fredapi calls:
  * get_series_all_releases(sid)  -> DataFrame[realtime_start, date, value]  (the vintage store)
  * get_series(sid)               -> latest (convenience only, never for PIT paths)
"""
from __future__ import annotations

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from prediction_market_macro.ingest.calendars import RELEASE_TIME_ET

ET = ZoneInfo("America/New_York")

# P0 series pulled every refresh (PLAN §5) — market-data series carry no revisions but are
# stored through the same path with knowledge_time = event date 18:00 ET (post-close).
CORE_SIDS = ["CPIAUCSL", "CPILFESL", "PCEPILFE", "UNRATE", "PAYEMS", "ICSA",
             "DFEDTARU", "DGS2", "DGS5", "DGS10", "DGS30", "T5YIE", "GASREGW",
             "DCOILWTICO", "GDPC1", "A191RL1Q225SBEA"]
_MARKET_SIDS = {"DGS2", "DGS5", "DGS10", "DGS30", "T5YIE", "DCOILWTICO", "DFEDTARU", "GASREGW"}


def _knowledge_time(sid: str, vintage_date) -> str:
    d = pd.Timestamp(vintage_date).date()
    if sid in _MARKET_SIDS and sid not in RELEASE_TIME_ET:
        hh, mm = 18, 0
    else:
        hh, mm = RELEASE_TIME_ET.get(sid, (18, 0))
    return datetime.combine(d, time(hh, mm), tzinfo=ET).astimezone(timezone.utc).isoformat()


class FredPIT:
    def __init__(self, api_key: str, conn):
        from fredapi import Fred
        self._fred = Fred(api_key=api_key)
        self._conn = conn

    # ── ingest ────────────────────────────────────────────────────────────
    def pull(self, sid: str, observation_start: str = "1985-01-01") -> int:
        """Full vintage pull for revised series; latest-only path for market series
        (their single vintage IS the datum). Idempotent upserts."""
        now = datetime.now(timezone.utc).isoformat()
        n = 0
        if sid in _MARKET_SIDS:
            s = self._fred.get_series(sid, observation_start=observation_start)
            for dt, v in s.dropna().items():
                d = pd.Timestamp(dt).date().isoformat()
                self._conn.execute(
                    "INSERT OR IGNORE INTO fred_obs(sid, event_time, value, vintage_date,"
                    " knowledge_time, first_seen_ts) VALUES(?,?,?,?,?,?)",
                    (sid, d, float(v), d, _knowledge_time(sid, d), now))
                n += 1
        else:
            df = self._fred.get_series_all_releases(sid)   # realtime_start, date, value
            df = df.dropna(subset=["value"])
            for _, r in df.iterrows():
                et_ = pd.Timestamp(r["date"]).date().isoformat()
                vd = pd.Timestamp(r["realtime_start"]).date().isoformat()
                self._conn.execute(
                    "INSERT OR IGNORE INTO fred_obs(sid, event_time, value, vintage_date,"
                    " knowledge_time, first_seen_ts) VALUES(?,?,?,?,?,?)",
                    (sid, et_, float(r["value"]), vd, _knowledge_time(sid, vd), now))
                n += 1
        self._conn.commit()
        return n

    def pull_core(self) -> dict[str, int]:
        """Resilient batch: one sid failing (FRED 5xx/rate storm) must never kill
        the whole morning ingest for every other series. Per-sid retry ×2 with
        backoff; failures alert individually and land as -1 in the result."""
        import time as _t
        out: dict[str, int] = {}
        failed = []
        for sid in CORE_SIDS:
            last_err = None
            for attempt in range(3):
                try:
                    out[sid] = self.pull(sid)
                    last_err = None
                    break
                except Exception as e:                     # noqa: BLE001
                    last_err = e
                    _t.sleep(2.0 * (attempt + 1))
            if last_err is not None:
                out[sid] = -1
                failed.append(sid)
                self._conn.execute(
                    "INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                    (datetime.now(timezone.utc).isoformat(), "warn", "refresh",
                     f"fred pull failed after retries: {sid}: {str(last_err)[:160]}"))
        self._conn.commit()
        if failed:
            print(f"  ! fred_core: {len(failed)} sid(s) failed: {failed}")
        return out

    # ── PIT reads (the ONLY read paths models may use, via features.py) ───
    def series_asof(self, sid: str, asof: datetime) -> pd.Series:
        """Latest visible value per event_time with knowledge_time <= asof."""
        rows = self._conn.execute(
            "SELECT event_time, value FROM fred_obs WHERE sid=? AND knowledge_time<=? "
            "AND vintage_date=(SELECT MAX(vintage_date) FROM fred_obs o2 WHERE o2.sid=fred_obs.sid"
            " AND o2.event_time=fred_obs.event_time AND o2.knowledge_time<=?) ORDER BY event_time",
            (sid, asof.isoformat(), asof.isoformat())).fetchall()
        s = pd.Series({pd.Timestamp(r["event_time"]): r["value"] for r in rows}, dtype=float)
        s.name = sid
        return s

    def first_release(self, sid: str, event_time: str) -> tuple[str, float] | None:
        """(knowledge_time, y_first): the FIRST print for a reference period (§5-bis.2)."""
        r = self._conn.execute(
            "SELECT knowledge_time, value FROM fred_obs WHERE sid=? AND event_time=? "
            "ORDER BY vintage_date ASC LIMIT 1", (sid, event_time)).fetchone()
        return (r["knowledge_time"], r["value"]) if r else None

    def latest(self, sid: str) -> tuple[str, float] | None:
        r = self._conn.execute(
            "SELECT event_time, value FROM fred_obs WHERE sid=? AND vintage_date="
            "(SELECT MAX(vintage_date) FROM fred_obs WHERE sid=?) ORDER BY event_time DESC LIMIT 1",
            (sid, sid)).fetchone()
        return (r["event_time"], r["value"]) if r else None
