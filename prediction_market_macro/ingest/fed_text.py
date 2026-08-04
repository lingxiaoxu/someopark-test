"""ingest/fed_text.py — FOMC statement text, live lane + 1994→ PIT backfill (PLAN §5, §28).

Two entry points, one writer:
  * `fetch_statements(conn)`   — daily lane, the meetings on our own calendar;
  * `backfill_statements(conn)`— every statement the Fed publishes back to 1994, walked
    off the FOMC calendar pages rather than guessed from a URL template.

Why the calendar walk. The old code built
`pressreleases/monetary{YYYYMMDD}a.htm` from `CALENDARS["FOMC"]`, which is right only
for 2006+ and only for meetings we happen to list. The Fed has used four URL shapes:

    1994-1995   /fomc/{YYYYMMDD}default.htm
    1996-2005   /boarddocs/press/{general,monetary}/{YYYY}/{YYYYMMDD}/
    2006-2018   /newsevents/press/monetary/{YYYYMMDD}a.htm
    2019-       /newsevents/pressreleases/monetary{YYYYMMDD}a.htm

`fomchistorical{YYYY}.htm` (≤5y back) and `fomccalendars.htm` (recent) both link the
statement directly under the anchor text "Statement", so the calendar pages are the
authority for both the date list AND the URL shape. That also picks up intermeeting
actions (2008-01-22, 2020-03-15) which no meeting calendar contains.

knowledge_time (PLAN §5-bis: every backfilled row must carry one):
  * `time_source='page'` — the statement says "For release at 2:00 p.m. EDT"; we use it.
    Measured over the 242 backfilled statements this line becomes universal from 2016
    (8/8 in 2016, 0/8 in 2015), with two earlier one-offs on coordinated actions
    (2008-10-08, 2010-05-09). It is also what makes emergency actions honest —
    2020-03-15 parses to 5:00 p.m. on a Sunday, not the usual 2 p.m.
  * `time_source='eod'`  — 1994-2015 pages only say "For immediate release" (152 rows).
    The real time was usually 14:15 ET, but "usually" is not a PIT stamp, so we take
    23:59 ET: the statement is certainly known by then. Cost: those statements become
    usable one day late, and only in an era with no Kalshi market to trade anyway.
    §5-bis prefers a late stamp to a guessed one.

Coverage check on the 2026-08-04 backfill: 242 statements, 1994-02-04 → 2026-07-29.
The 1990s contribute only 17 because the FOMC announced outcomes only when policy
changed until 1999 — that is the real record, not a gap in the walk. Seven months hold
more than one statement (2007-08 and 2020-03 hold three); under the old `period`-only
primary key eight of those were silently unstorable.
"""
from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

_TAG = re.compile(r"<script.*?</script>|<style.*?</style>|<[^>]+>", re.S)
_WS = re.compile(r"[ \t]+")
_UA = {"User-Agent": "someopark-macro/0.1 (research; contact: local)"}
_ET = ZoneInfo("America/New_York")

_HOST = "https://www.federalreserve.gov"
_CAL_CURRENT = f"{_HOST}/monetarypolicy/fomccalendars.htm"
_CAL_HIST = _HOST + "/monetarypolicy/fomchistorical{year}.htm"

# Two page layouts, both live:
#  * fomchistorical{YYYY}.htm — anchor text is exactly "Statement". It must be exact:
#    "Statement on Longer-Run Goals and Monetary Policy Strategy" is a different
#    document sitting on the same pages, and it is a PDF of a framework, not a decision.
#  * fomccalendars.htm        — anchor text is "PDF | HTML" under a <strong>Statement:
#    </strong> label, so the label has to be matched first and the .htm taken from
#    inside its block. Matching bare monetary\d{8}a.htm across the whole page would also
#    swallow the implementation note and the minutes.
_STMT_LINK = re.compile(r'href="([^"]+)"[^>]*>\s*Statement\s*<', re.I)
_STMT_BLOCK = re.compile(r"<strong>\s*Statement:\s*</strong>(.{0,400}?)</div>", re.S | re.I)
_BLOCK_HTML = re.compile(r'href="([^"]+\.htm)"[^>]*>\s*HTML\s*<', re.I)
_DATE8 = re.compile(r"(19|20)(\d{2})(\d{2})(\d{2})")
# the modern release line; "p.m." also written "pm" / "p.m" on a few pages
_REL_AT = re.compile(r"[Ff]or release at\s+(\d{1,2}):(\d{2})\s*([ap])\.?\s*m\.?", re.I)


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


def _et_to_utc(release_date: str, hh: int, mm: int) -> str:
    """ET wall clock → UTC ISO string.

    Two things this must not skip. zoneinfo rather than a hardcoded offset, because half
    the FOMC calendar sits in EST. And the result is converted to UTC, because every PIT
    filter in this codebase is a *string* comparison against a `+00:00` timestamp — an
    ET-offset string sorts by its wall clock, so '2026-07-29T14:00-04:00' would compare
    less than an asof of '2026-07-29T17:00+00:00' and hand a replay the statement an hour
    before the Fed published it. (That is not hypothetical: the first draft stored ET and
    `test_statements_asof_respects_the_hour_not_just_the_day` caught it.)
    """
    d = datetime.strptime(release_date, "%Y-%m-%d").date()
    return datetime.combine(d, time(hh, mm), tzinfo=_ET).astimezone(timezone.utc).isoformat()


def eod_knowledge_time(release_date: str) -> str:
    """23:59 ET on the release date, in UTC."""
    return _et_to_utc(release_date, 23, 59)


def _release_ts(release_date: str, text: str) -> tuple[str, str]:
    """(knowledge_time, time_source) for one statement. See module docstring."""
    m = _REL_AT.search(text)
    if not m:
        return eod_knowledge_time(release_date), "eod"
    hh, mm, ap = int(m.group(1)), int(m.group(2)), m.group(3).lower()
    if hh == 12:
        hh = 0
    if ap == "p":
        hh += 12
    if not (0 <= hh <= 23):
        return eod_knowledge_time(release_date), "eod"
    return _et_to_utc(release_date, hh, mm), "page"


def _statement_links(url: str, timeout: int = 30) -> list[tuple[str, str]]:
    """[(release_date, absolute_url)] from one FOMC calendar page, deduped."""
    try:
        r = requests.get(url, headers=_UA, timeout=timeout)
        if r.status_code != 200:
            return []
    except Exception:                                          # noqa: BLE001
        return []
    hrefs = list(_STMT_LINK.findall(r.text))
    for blk in _STMT_BLOCK.findall(r.text):
        m = _BLOCK_HTML.search(blk)
        if m:
            hrefs.append(m.group(1))
    out: dict[str, str] = {}
    for href in hrefs:
        m = _DATE8.search(href)
        if not m:
            continue
        y, mo, dd = m.group(1) + m.group(2), m.group(3), m.group(4)
        out.setdefault(f"{y}-{mo}-{dd}", href if href.startswith("http") else _HOST + href)
    return sorted(out.items())


def _store(conn, release_date: str, url: str, now: datetime) -> bool:
    """Fetch one statement and write it. False when the page is not a usable statement."""
    try:
        r = requests.get(url, headers=_UA, timeout=30)
    except Exception:                                          # noqa: BLE001
        return False
    if r.status_code != 200 or len(r.text) < 500:
        return False
    text = _clean(r.text)
    if "Committee" not in text and "Federal Open Market" not in text:
        return False
    kt, src = _release_ts(release_date, text)
    conn.execute(
        "INSERT OR REPLACE INTO fed_statements(period, release_date, url, text,"
        " knowledge_time, time_source, fetched_ts) VALUES(?,?,?,?,?,?,?)",
        (release_date[:7], release_date, url, text, kt, src, now.isoformat()))
    return True


def backfill_statements(conn, since_year: int = 1994, until_year: int | None = None,
                        refetch: bool = False) -> int:
    """Walk every FOMC calendar page from `since_year` and store each statement.

    Idempotent: a (period, release_date) already present is skipped unless `refetch`.
    `refetch=True` is the path that upgrades migrated v1 rows from an 'eod' stamp to the
    page-parsed one. Degradable — a dead page alerts and the walk continues.
    """
    now = datetime.now(timezone.utc)
    until = until_year or now.year
    have = {r[0] for r in conn.execute("SELECT release_date FROM fed_statements")}
    seen: dict[str, str] = {}
    for y in range(since_year, until + 1):
        for d, u in _statement_links(_CAL_HIST.format(year=y)):
            seen.setdefault(d, u)
    for d, u in _statement_links(_CAL_CURRENT):                # recent years live here
        seen.setdefault(d, u)
    n = 0
    for d, u in sorted(seen.items()):
        if not (since_year <= int(d[:4]) <= until):
            continue
        if d in have and not refetch:
            continue
        try:
            if _store(conn, d, u, now):
                n += 1
        except Exception as e:                                 # noqa: BLE001
            conn.execute("INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                         (now.isoformat(), "warn", "fed_text_backfill", f"{d}: {e}"))
        if n % 25 == 0:
            conn.commit()
    conn.commit()
    return n


def fetch_statements(conn, lookback_days: int = 400) -> int:
    """Daily lane: any recent meeting on our calendar we do not yet have.

    Still template-driven (cheap, one GET per missing meeting) because for the current
    era the template is correct; `backfill_statements` is the authority for history.
    """
    from prediction_market_macro.ingest.calendars import CALENDARS
    now = datetime.now(timezone.utc)
    n = 0
    for ev in CALENDARS["FOMC"]:
        if not (now - timedelta(days=lookback_days) <= ev.scheduled_ts <= now):
            continue
        day = ev.scheduled_ts.astimezone(_ET).date().isoformat()
        if conn.execute("SELECT 1 FROM fed_statements WHERE release_date=?",
                        (day,)).fetchone():
            continue
        url = (f"{_HOST}/newsevents/pressreleases/"
               f"monetary{day.replace('-', '')}a.htm")
        try:
            if _store(conn, day, url, now):
                n += 1
        except Exception as e:                                 # noqa: BLE001
            conn.execute(
                "INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                (now.isoformat(), "warn", "fed_text", f"{ev.period}: {e}"))
    conn.commit()
    return n


def statements_asof(conn, asof: datetime, limit: int = 2) -> list[dict]:
    """The `limit` most recent statements KNOWN at `asof`, newest first.

    The PIT accessor. Anything that feeds a model or a backtest must come through here
    rather than ordering the whole table, or a replay of 2026-06 reads the July
    statement.
    """
    rows = conn.execute(
        "SELECT period, release_date, url, text, knowledge_time, time_source"
        " FROM fed_statements WHERE knowledge_time<=? ORDER BY release_date DESC LIMIT ?",
        (asof.isoformat(), limit)).fetchall()
    return [dict(r) for r in rows]


def latest_two(conn) -> tuple[dict, dict] | None:
    """Live-lane convenience: the two newest statements known *now*."""
    rows = statements_asof(conn, datetime.now(timezone.utc), limit=2)
    if len(rows) < 2:
        return None
    return rows[0], rows[1]
