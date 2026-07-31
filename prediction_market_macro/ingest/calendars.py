"""ingest/calendars.py — official release calendars (PLAN §5, §5-bis.4).

Forward schedules hand-verified from official sources on 2026-07-27:
  * BLS CPI:   bls.gov schedule (via cpiinflationcalculator mirror; Jul-14 cross-checked live)
  * BLS jobs:  Employment Situation — first-Friday cadence, H2-2026 verified (PFEI schedule)
  * BEA PCE:   Personal Income & Outlays — verified through Dec 2026
  * FOMC:      federalreserve.gov 2026+2027 (SEP meetings flagged)
  * DOL claims: every Thursday 08:30 ET, generated with holiday exceptions (Thanksgiving)

Times are defined in ET and converted per-date via zoneinfo (DST-correct, §5-bis.4-5);
storage/comparison is UTC everywhere. HISTORICAL knowledge_time for revised series comes
from ALFRED vintages (ingest/fred.py), not from this file — this file is the FORWARD schedule
+ the release-time-of-day rule used to sharpen ALFRED's date-granularity vintages.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


@dataclass(frozen=True)
class ReleaseEvent:
    cal: str
    period: str            # reference period: "2026-07" (monthly/FOMC) or ISO date (claims=release date)
    scheduled_ts: datetime # UTC
    note: str = ""


def _et(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=ET).astimezone(UTC)


# ── BLS CPI (ref month -> release date), 08:30 ET ────────────────────────────
_CPI = {
    "2025-12": date(2026, 1, 13), "2026-01": date(2026, 2, 11), "2026-02": date(2026, 3, 11),
    "2026-03": date(2026, 4, 10), "2026-04": date(2026, 5, 12), "2026-05": date(2026, 6, 10),
    "2026-06": date(2026, 7, 14), "2026-07": date(2026, 8, 12), "2026-08": date(2026, 9, 11),
    "2026-09": date(2026, 10, 14), "2026-10": date(2026, 11, 10), "2026-11": date(2026, 12, 10),
}

# ── BLS Employment Situation (ref month -> release date), 08:30 ET ───────────
_JOBS = {
    "2026-06": date(2026, 7, 2),   # Jul-3 holiday-shifted (already released)
    "2026-07": date(2026, 8, 7), "2026-08": date(2026, 9, 4), "2026-09": date(2026, 10, 2),
    "2026-10": date(2026, 11, 6), "2026-11": date(2026, 12, 4),
}

# ── BEA Personal Income & Outlays / core PCE (ref month -> release), 08:30 ET ─
_PCE = {
    "2026-06": date(2026, 7, 30), "2026-07": date(2026, 8, 26), "2026-08": date(2026, 9, 30),
    "2026-09": date(2026, 10, 29), "2026-10": date(2026, 11, 25), "2026-11": date(2026, 12, 23),
}

# ── FOMC decision days (statement 14:00 ET on day 2); SEP meetings flagged ───
_FOMC: list[tuple[str, date, bool]] = [
    ("2026-01", date(2026, 1, 28), False), ("2026-03", date(2026, 3, 18), True),
    ("2026-04", date(2026, 4, 29), False), ("2026-06", date(2026, 6, 17), True),
    ("2026-07", date(2026, 7, 29), False), ("2026-09", date(2026, 9, 16), True),
    ("2026-10", date(2026, 10, 28), False), ("2026-12", date(2026, 12, 9), True),
    ("2027-01", date(2027, 1, 27), False), ("2027-03", date(2027, 3, 17), True),
    ("2027-04", date(2027, 4, 28), False), ("2027-06", date(2027, 6, 9), True),
    ("2027-07", date(2027, 7, 28), False), ("2027-09", date(2027, 9, 15), True),
    ("2027-10", date(2027, 10, 27), False), ("2027-12", date(2027, 12, 8), True),
]

# claims: Thursday whose release moves to Wednesday (federal holiday on Thursday)
_CLAIMS_SHIFT = {date(2026, 11, 26): date(2026, 11, 25),
                 date(2027, 11, 25): date(2027, 11, 24),
                 date(2027, 12, 23): date(2027, 12, 23)}   # placeholder-safe (no shift)


def _claims_events(start: date, end: date) -> list[ReleaseEvent]:
    out = []
    d = start + timedelta(days=(3 - start.weekday()) % 7)      # first Thursday >= start
    while d <= end:
        rel = _CLAIMS_SHIFT.get(d, d)
        out.append(ReleaseEvent("DOL_CLAIMS", rel.isoformat(), _et(rel, 8, 30),
                                note="initial claims (advance, SA)"))
        d += timedelta(days=7)
    return out


def _weekly_events(cal: str, weekday: int, hh: int, mm: int,
                   start: date, end: date, note: str) -> list[ReleaseEvent]:
    out = []
    d = start + timedelta(days=(weekday - start.weekday()) % 7)
    while d <= end:
        out.append(ReleaseEvent(cal, d.isoformat(), _et(d, hh, mm), note=note))
        d += timedelta(days=7)
    return out


def build_calendars() -> dict[str, list[ReleaseEvent]]:
    cals: dict[str, list[ReleaseEvent]] = {
        "BLS_CPI": [ReleaseEvent("BLS_CPI", p, _et(d, 8, 30)) for p, d in _CPI.items()],
        "BLS_JOBS": [ReleaseEvent("BLS_JOBS", p, _et(d, 8, 30)) for p, d in _JOBS.items()],
        "BEA_PCE": [ReleaseEvent("BEA_PCE", p, _et(d, 8, 30)) for p, d in _PCE.items()],
        "FOMC": [ReleaseEvent("FOMC", p, _et(d, 14, 0), note="SEP" if sep else "")
                 for p, d, sep in _FOMC],
        "DOL_CLAIMS": _claims_events(date(2026, 7, 1), date(2027, 12, 31)),
        # energy weekly settles: WTI/NG Friday NYMEX close, AAA gas Monday morning read
        "WTI_WEEKLY": _weekly_events("WTI_WEEKLY", 4, 14, 30, date(2026, 7, 1),
                                     date(2027, 12, 31), "WTI front-month Friday close"),
        "NG_WEEKLY": _weekly_events("NG_WEEKLY", 4, 14, 30, date(2026, 7, 1),
                                    date(2027, 12, 31), "Henry Hub front-month Friday close"),
        "AAA_WEEKLY": _weekly_events("AAA_WEEKLY", 0, 9, 0, date(2026, 7, 1),
                                     date(2027, 12, 31), "AAA national average Monday"),
    }
    for k in cals:
        cals[k].sort(key=lambda e: e.scheduled_ts)
    return cals


CALENDARS = build_calendars()

# release-time-of-day rule per FRED sid (sharpens ALFRED date-granularity vintages, §5-bis.1 口1)
RELEASE_TIME_ET: dict[str, tuple[int, int]] = {
    "CPIAUCSL": (8, 30), "CPILFESL": (8, 30), "PCEPILFE": (8, 30),
    "UNRATE": (8, 30), "PAYEMS": (8, 30), "ICSA": (8, 30),
    "GDPC1": (8, 30), "DFEDTARU": (14, 0),
}


def next_release(cal: str, now: datetime) -> ReleaseEvent | None:
    for e in CALENDARS[cal]:
        if e.scheduled_ts > now:
            return e
    return None


def releases_between(cal: str, a: datetime, b: datetime) -> list[ReleaseEvent]:
    return [e for e in CALENDARS[cal] if a <= e.scheduled_ts <= b]


def sync_to_db(conn) -> int:
    """Idempotent upsert of the forward schedule into releases(cal, period, scheduled_ts)."""
    n = 0
    for cal, evs in CALENDARS.items():
        for e in evs:
            conn.execute(
                "INSERT INTO releases(cal, period, scheduled_ts) VALUES(?,?,?) "
                "ON CONFLICT(cal, period) DO UPDATE SET scheduled_ts=excluded.scheduled_ts "
                "WHERE releases.actual_ts IS NULL",
                (cal, e.period, e.scheduled_ts.isoformat()))
            n += 1
    conn.commit()
    return n
