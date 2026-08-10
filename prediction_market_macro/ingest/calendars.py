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
    # 2026-01 slipped: BLS printed Feb 13, not the Feb 11 originally scheduled
    # (fred_obs CPIAUCSL 2026-01-01 first knowledge_time = 2026-02-13T13:30Z).
    "2025-12": date(2026, 1, 13), "2026-01": date(2026, 2, 13), "2026-02": date(2026, 3, 11),
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


# ── BEA GDP advance estimates (quarter -> release), 08:30 ET ─────────────────
_GDP = {
    "2026-Q2": date(2026, 7, 30), "2026-Q3": date(2026, 10, 29),
    "2026-Q4": date(2027, 1, 28),
}


def build_calendars() -> dict[str, list[ReleaseEvent]]:
    cals: dict[str, list[ReleaseEvent]] = {
        "BLS_CPI": [ReleaseEvent("BLS_CPI", p, _et(d, 8, 30)) for p, d in _CPI.items()],
        "BLS_JOBS": [ReleaseEvent("BLS_JOBS", p, _et(d, 8, 30)) for p, d in _JOBS.items()],
        "BEA_PCE": [ReleaseEvent("BEA_PCE", p, _et(d, 8, 30)) for p, d in _PCE.items()],
        "BEA_GDP": [ReleaseEvent("BEA_GDP", p, _et(d, 8, 30), note="advance")
                    for p, d in _GDP.items()],
        "FOMC": [ReleaseEvent("FOMC", p, _et(d, 14, 0), note="SEP" if sep else "")
                 for p, d, sep in _FOMC],
        "DOL_CLAIMS": _claims_events(date(2026, 7, 1), date(2027, 12, 31)),
        # EIA weekly reports (status data for energy models; not tradable series yet)
        "EIA_PETRO": _weekly_events("EIA_PETRO", 2, 10, 30, date(2026, 7, 1),
                                    date(2027, 12, 31), "EIA weekly petroleum status"),
        "EIA_NG": _weekly_events("EIA_NG", 3, 10, 30, date(2026, 7, 1),
                                 date(2027, 12, 31), "EIA weekly nat gas storage"),
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
    "GDPC1": (8, 30), "A191RL1Q225SBEA": (8, 30), "GDPNOW": (11, 0),
    "DFEDTARU": (14, 0),
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


# scheduled-vs-actual reconciliation (§5-bis.4-4): which first print settles which cal
_CAL_SID = {"BLS_CPI": "CPIAUCSL", "BLS_JOBS": "PAYEMS", "BEA_PCE": "PCEPILFE",
            "DOL_CLAIMS": "ICSA", "FOMC": "DFEDTARU", "AAA_WEEKLY": "GASREGW"}


def reconcile_actuals(conn, now: datetime | None = None) -> dict:
    """Fill releases.actual_ts from the label's first-print knowledge_time; releases
    >26h overdue with no print flip coverage to 'postponed' + alert (shutdown/delay
    handling, §19-8 operational leg)."""
    from datetime import timezone as _tz
    now = now or datetime.now(_tz.utc)
    filled, postponed = 0, 0
    rows = conn.execute(
        "SELECT cal, period, scheduled_ts FROM releases WHERE actual_ts IS NULL"
        " AND scheduled_ts < ?", ((now - timedelta(hours=2)).isoformat(),)).fetchall()
    for r in rows:
        sid = _CAL_SID.get(r["cal"])
        if sid is None:
            continue
        sch = datetime.fromisoformat(r["scheduled_ts"])
        # FORWARD-anchored: a release slips late, never early. The window used to open
        # at sch-3d, which is fatal for DAILY-published sids — DFEDTARU prints every
        # single day, so MIN() always returned sch-3d and every FOMC actual_ts landed 3
        # days early (2026-01 "settled" on Sunday Jan 25). That also meant actual_ts was
        # ALWAYS non-null, so a genuinely postponed meeting could never trip the
        # postponed branch below, and sync_to_db's `WHERE actual_ts IS NULL` froze the
        # bad scheduled_ts forever. -2h absorbs DST/rounding jitter while staying far
        # inside the 24h that would let a daily sid's previous print sneak in.
        kt = conn.execute(
            "SELECT MIN(knowledge_time) m FROM fred_obs WHERE sid=? AND"
            " knowledge_time BETWEEN ? AND ?",
            (sid, (sch - timedelta(hours=2)).isoformat(),
             (sch + timedelta(days=3)).isoformat())).fetchone()
        if kt and kt["m"]:
            conn.execute("UPDATE releases SET actual_ts=? WHERE cal=? AND period=?",
                         (kt["m"], r["cal"], r["period"]))
            filled += 1
        elif (now - sch) > timedelta(hours=26):
            # only the series bound to THIS calendar — period strings like
            # '2026-07' are shared across calendars and must not cross-flag
            from prediction_market_macro.config.registry import REGISTRY
            for spec in REGISTRY.values():
                if spec.calendar != r["cal"]:
                    continue
                conn.execute(
                    "UPDATE coverage SET state='postponed', updated_ts=? WHERE"
                    " series=? AND period=? AND state='scheduled'",
                    (now.isoformat(), spec.ticker, r["period"]))
            conn.execute(
                "INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                (now.isoformat(), "warn", "calendar",
                 f"POSTPONED? {r['cal']}/{r['period']} scheduled {r['scheduled_ts']}"
                 f" but no first print within 26h"))
            postponed += 1
    conn.commit()
    return {"actual_filled": filled, "postponed_flagged": postponed}


def refresh_from_web(conn) -> dict:
    """Weekly drift check of the hardcoded forward schedule against official sources
    (BLS CPI + Employment Situation pages). Detect-and-alert only — the hardcoded
    schedule stays the source of truth until hand-verified. Degradable."""
    import re as _re

    import requests as _rq
    from datetime import timezone as _tz
    now = datetime.now(_tz.utc)
    checked, drifts = 0, []
    pages = {"BLS_CPI": "https://www.bls.gov/schedule/news_release/cpi.htm",
             "BLS_JOBS": "https://www.bls.gov/schedule/news_release/empsit.htm"}
    # BLS blocks by User-Agent, and the rule is the opposite of the usual one: a
    # browser-spoofing UA gets 403, an identified bot UA carrying a contact address
    # gets 200 (bls.gov data-access policy). The bare "someopark-macro/0.1" this used
    # to send was 403'd on every run since deployment — deterministically, both pages,
    # verified 2026-08-09.
    ua = "someopark-macro/0.1 (+lxu912@gmail.com)"
    months = {}
    for i, m in enumerate(["January", "February", "March", "April", "May", "June",
                           "July", "August", "September", "October", "November",
                           "December"], 1):
        months[m] = i
        months[m[:3]] = i                    # BLS pages abbreviate ("Jan. 13, 2026")
    for cal, url in pages.items():
        try:
            resp = _rq.get(url, timeout=30, headers={"User-Agent": ua})
        except Exception as e:                                # noqa: BLE001
            conn.execute("INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                         (now.isoformat(), "warn", "calendar",
                          f"refresh_from_web {cal}: {e}"))
            continue
        # Check status BEFORE parsing. Without this a 403 block page (1.3KB of "Access
        # Denied", still text/html, still a 200-shaped .text attribute) fell straight
        # through to the parser and surfaced as "zero dates parsed — check regex",
        # which sent every reader hunting for a page-format change that never happened.
        if resp.status_code != 200:
            conn.execute("INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                         (now.isoformat(), "warn", "calendar",
                          f"refresh_from_web {cal}: HTTP {resp.status_code} from {url}"
                          f" — not a parse problem; check User-Agent / blocking"))
            continue
        html = resp.text
        web_dates = set()
        for m in _re.finditer(
                r"(January|February|March|April|May|June|July|August|September|"
                r"October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|"
                r"Oct|Nov|Dec)\.?\s+(\d{1,2}),?\s+(20\d\d)", html):
            web_dates.add(date(int(m.group(3)), months[m.group(1)], int(m.group(2))))
        if not web_dates:
            conn.execute("INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                         (now.isoformat(), "warn", "calendar",
                          f"refresh_from_web {cal}: page fetched but zero dates"
                          f" parsed — check regex vs page format"))
            continue
        src = _CPI if cal == "BLS_CPI" else _JOBS
        for period, d in src.items():
            if d < now.date():
                continue
            checked += 1
            if d not in web_dates:
                drifts.append(f"{cal}/{period}: local {d} not on official schedule page")
    for msg in drifts:
        conn.execute("INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                     (now.isoformat(), "error", "calendar", f"SCHEDULE-DRIFT {msg}"))
    conn.commit()
    return {"checked": checked, "drifts": len(drifts)}
