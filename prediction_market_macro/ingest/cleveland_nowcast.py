"""ingest/cleveland_nowcast.py — Cleveland Fed daily inflation nowcasts, PIT.

Why this source: the bias study (2026-08-15, /tmp) showed the CPI-family models
carry no fixable systematic bias — they are simply behind the market, and the
canonical public input CPI traders anchor on is this nowcast. It is exactly the
"information the market has that we don't" candidate, and unlike the weather line
it IS the quantity being traded, not an upstream proxy.

Source: the JSON files behind the official charting tool (CC-BY-4.0, credited to
the Federal Reserve Bank of Cleveland):
    /-/media/files/webcharts/inflationnowcasting/nowcast_{month,year,quarter}.json
Each file carries the FULL history — one chart element per nowcast target back to
2013-07, with the daily sequence of nowcast values (four measures: CPI, core CPI,
PCE, core PCE) generated on each business day. One fetch = complete vintage
backfill; the daily refresh re-fetches and upserts the tail. ~7.5MB per file.

Parsing judgment calls, stated plainly:
  * category arrays mix day labels ("08/12") with FusionCharts vline objects
    (release-date markers) — only entries carrying a label become dates;
  * labels are MM/DD with no year: a label's month >= the target's month means the
    target's year, else the following year (a December target's window runs into
    January);
  * the "Actual ..." series are NOT stored — BLS/BEA actuals already live in
    fred_obs with real vintages, and a second copy invites drift;
  * knowledge_time = nowcast day 18:00 UTC. The Fed says "released each business
    day" without a timestamp; contemporaneous coverage shows morning updates, so
    13:00 ET is conservative in the safe direction (we admit knowing it later
    than the world did). If the exact schedule ever surfaces, tighten it here.

Consumption status (2026-08-15): `model/cpi.py` 0.3.0 anchors HEADLINE YoY mu on
`latest()` — adopted on the user-ordered leak-free historical replay (45 settled
KXCPIYOY events, per-leg Brier −33%, every year slice 2023-2026 improves), which
superseded PR-8's forward count; PR-8's forward tally continues as confirmation.
Core YoY was a wash in the same replay and is NOT anchored (PREREGISTER.md PR-8).
"""
from __future__ import annotations

import json
import urllib.request
from datetime import date, datetime, time, timedelta, timezone

_BASE = ("https://www.clevelandfed.org/-/media/files/webcharts/inflationnowcasting/"
         "nowcast_{kind}.json?sc_lang=en")
_UA = "someopark-macro/0.1 (+lxu912@gmail.com)"   # calendars.py convention
KINDS = {"month": "mom", "year": "yoy", "quarter": "q"}
MEASURES = {"CPI Inflation": "cpi", "Core CPI Inflation": "corecpi",
            "PCE Inflation": "pce", "Core PCE Inflation": "corepce"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS cleveland_nowcast(
  measure TEXT NOT NULL,               -- cpi | corecpi | pce | corepce
  freq TEXT NOT NULL,                  -- mom | yoy | q
  target TEXT NOT NULL,                -- nowcasted period, e.g. '2026-08' / '2026-Q3'
  nowcast_date TEXT NOT NULL,          -- business day the nowcast was generated
  value REAL NOT NULL,
  knowledge_time TEXT NOT NULL,        -- nowcast_date 18:00 UTC (module docstring)
  first_seen_ts TEXT NOT NULL,
  PRIMARY KEY(measure, freq, target, nowcast_date));
CREATE TABLE IF NOT EXISTS cleveland_nowcast_meta(
  k TEXT PRIMARY KEY, v TEXT NOT NULL);   -- 'last_attempt' throttles refresh_if_stale
"""


def ensure_schema(conn) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _kt(day: str) -> str:
    return datetime.combine(date.fromisoformat(day), time(18, 0),
                            tzinfo=timezone.utc).isoformat()


def _target_norm(subcaption: str) -> tuple[str, int | None]:
    """'2026-8' -> ('2026-08', 8); quarterly '2026:Q3'/'2026-Q3' -> ('2026-Q3', None)."""
    s = subcaption.strip().replace(":", "-")
    parts = s.split("-")
    if len(parts) == 2 and parts[1].isdigit():
        return f"{parts[0]}-{int(parts[1]):02d}", int(parts[1])
    return s, None


def _label_dates(cats: list[dict], target_year: int, target_month: int | None):
    """MM/DD labels -> ISO dates; month < target month rolls into the next year.
    Quarterly targets (no month) infer the year from the label sequence itself:
    the window starts inside the target year and a January rollover increments it."""
    out = []
    prev_m = None
    year = target_year
    for c in cats:
        lab = c.get("label")
        if not lab or "/" not in str(lab):
            continue                                   # vline / decoration entries
        m, d = (int(x) for x in str(lab).split("/"))
        if target_month is not None:
            year = target_year + (1 if m < target_month else 0)
        else:
            if prev_m is not None and m < prev_m:      # quarterly: Jan rollover
                year += 1
            prev_m = m
        out.append(f"{year:04d}-{m:02d}-{d:02d}")
    return out


def parse(blob: bytes, freq: str):
    """→ [(measure, freq, target, nowcast_date, value)] for every nowcast point."""
    charts = json.loads(blob)
    rows = []
    for el in charts:
        target, t_month = _target_norm(el["chart"]["subcaption"])
        t_year = int(target[:4])
        days = _label_dates(el["categories"][0]["category"], t_year, t_month)
        for s in el.get("dataset", []):
            meas = MEASURES.get(s.get("seriesname"))
            if meas is None:                           # 'Actual ...' series skipped
                continue
            vals = [v.get("value") for v in s.get("data", [])]
            for day, v in zip(days, vals):
                if v in (None, ""):
                    continue
                rows.append((meas, freq, target, day, float(v)))
    return rows


def _fetch(kind: str, timeout: int = 120) -> bytes:
    req = urllib.request.Request(_BASE.format(kind=kind),
                                 headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def refresh(conn, kinds=("month", "year", "quarter")) -> dict:
    """Fetch + upsert. The files carry full history, so this IS the backfill and
    the daily tail-update in one idempotent call (weather.py's INSERT OR REPLACE
    + first_seen_ts preservation idiom)."""
    ensure_schema(conn)
    now = datetime.now(timezone.utc).isoformat()
    out = {}
    for kind in kinds:
        freq = KINDS[kind]
        rows = parse(_fetch(kind), freq)
        n = 0
        for meas, fq, target, day, val in rows:
            conn.execute(
                "INSERT OR REPLACE INTO cleveland_nowcast(measure, freq, target,"
                " nowcast_date, value, knowledge_time, first_seen_ts)"
                " VALUES(?,?,?,?,?,?,"
                " COALESCE((SELECT first_seen_ts FROM cleveland_nowcast WHERE"
                "  measure=? AND freq=? AND target=? AND nowcast_date=?), ?))",
                (meas, fq, target, day, val, _kt(day),
                 meas, fq, target, day, now))
            n += 1
        conn.commit()
        out[freq] = n
    return out


def _expected_day(now: datetime) -> str:
    """Latest business day whose knowledge_time (18:00 UTC) has passed at `now`."""
    d = now.date()
    if now < datetime.combine(d, time(18, 0), tzinfo=timezone.utc):
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat()


def refresh_if_stale(conn, now: datetime | None = None,
                     min_gap_min: int = 55) -> dict | None:
    """Intraday tail-guard, hooked into `predict_all.run` (the single door every live
    prediction passes through). The daily 05:00 ET refresh ingests YESTERDAY's
    nowcast; today's — published in the Cleveland morning, admitted by kt at 18:00
    UTC — would otherwise reach live trading a day late.

    Fetches ONLY the yoy file (the one freq the model consumes; the full 3-kind
    refresh stays in the daily pipeline) and only when BOTH hold:
      (a) the newest stored yoy nowcast is older than the latest business day whose
          kt has passed — before 18:00 UTC today's row would be PIT-invisible anyway,
          so fetching it early buys nothing;
      (b) no attempt ran in the last `min_gap_min` — holidays publish no new file,
          and without the throttle a holiday means a 7.5MB fetch per 900s tick for
          the rest of the day. The attempt is recorded BEFORE the fetch, so a dead
          feed also degrades to ~one attempt (and one alert upstream) per hour.
    Returns the refresh() dict when a fetch ran, None when skipped."""
    now = now or datetime.now(timezone.utc)
    ensure_schema(conn)
    have = conn.execute(
        "SELECT MAX(nowcast_date) FROM cleveland_nowcast WHERE freq='yoy'").fetchone()[0]
    if have is not None and have >= _expected_day(now):
        return None
    last = conn.execute(
        "SELECT v FROM cleveland_nowcast_meta WHERE k='last_attempt'").fetchone()
    if last is not None and (now - datetime.fromisoformat(last[0])
                             ).total_seconds() < min_gap_min * 60:
        return None
    conn.execute("INSERT OR REPLACE INTO cleveland_nowcast_meta VALUES('last_attempt',?)",
                 (now.isoformat(),))
    conn.commit()
    return refresh(conn, kinds=("year",))


def latest(conn, measure: str, freq: str, target: str,
           asof: datetime) -> tuple[str, float] | None:
    """PIT accessor: newest nowcast for `target` known at `asof`."""
    r = conn.execute(
        "SELECT nowcast_date, value FROM cleveland_nowcast WHERE measure=? AND"
        " freq=? AND target=? AND knowledge_time<=? ORDER BY nowcast_date DESC"
        " LIMIT 1", (measure, freq, target, asof.isoformat())).fetchone()
    return None if r is None else (r["nowcast_date"], r["value"])


if __name__ == "__main__":
    from pathlib import Path
    from prediction_market_macro.ingest.store import connect
    db = Path(__file__).resolve().parent.parent / "data" / "macro.db"
    print(refresh(connect(db)))
