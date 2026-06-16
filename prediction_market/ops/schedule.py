"""World Cup schedule viewer in US time zones (ET + PT). Fixed, reusable tool.

Fixtures are stored with UTC kickoff timestamps (API-Football). This renders them
in **US Eastern (America/New_York)** and **US Pacific (America/Los_Angeles)**,
filterable by US date and upcoming-only.

IMPORTANT: a match at 01:00 UTC on calendar day D+1 is the evening of day D in US
time (e.g. 2026-06-17 01:00 UTC = Tue 2026-06-16 21:00 ET / 18:00 PT). So date
filtering is done on the **ET date**, not the UTC date.

Usage:
    # today's (ET) not-yet-played matches, from stored data (0 API calls)
    conda run -n someopark_run python -m prediction_market.ops.schedule --upcoming
    # a specific US date, refreshing statuses first (1 API call)
    conda run -n someopark_run python -m prediction_market.ops.schedule --date 2026-06-16 --upcoming --refresh
    # next N days
    conda run -n someopark_run python -m prediction_market.ops.schedule --days 3
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")

# Statuses that mean "not yet played" (API-Football).
NOT_STARTED = ("NS", "TBD", "PST")


@dataclass(frozen=True)
class Fixture:
    kickoff_utc: datetime
    home: str
    away: str
    round: str
    status: str

    @property
    def et(self) -> datetime:
        return self.kickoff_utc.astimezone(ET)

    @property
    def pt(self) -> datetime:
        return self.kickoff_utc.astimezone(PT)


def load_fixtures(conn=None, *, et_date: str | None = None, upcoming: bool = False,
                  days: int = 0) -> list[Fixture]:
    """Stored fixtures, filtered by ET date / upcoming / next-N-days, sorted by kickoff."""
    from prediction_market.ingest import store

    conn = conn or store.init_db()
    rows = conn.execute(
        "SELECT f.kickoff_ts, f.round, f.status_short, th.name h, ta.name a "
        "FROM fixture f JOIN team th ON f.home_api_id=th.api_id "
        "JOIN team ta ON f.away_api_id=ta.api_id WHERE f.kickoff_ts IS NOT NULL "
        "ORDER BY f.kickoff_ts").fetchall()
    now_et = datetime.now(timezone.utc).astimezone(ET).date()
    out: list[Fixture] = []
    for r in rows:
        try:
            ku = datetime.fromisoformat(r["kickoff_ts"]).astimezone(timezone.utc)
        except ValueError:
            continue
        fx = Fixture(ku, r["h"], r["a"], r["round"] or "", r["status_short"] or "")
        if upcoming and fx.status not in NOT_STARTED:
            continue
        if et_date and fx.et.date().isoformat() != et_date:
            continue
        out.append(fx)
    if days:
        from datetime import timedelta
        hi = now_et + timedelta(days=days)
        out = [f for f in out if now_et <= f.et.date() < hi]
    return out


def refresh_fixtures() -> None:
    """Re-pull the fixture list to update kickoff times + statuses (1 API call)."""
    from prediction_market.ingest import store
    from prediction_market.ingest.api_football import ApiFootball
    from prediction_market.ingest.soccer_ingest import sync_fixtures

    conn = store.init_db()
    sync_fixtures(ApiFootball(conn), conn, force=True)


def render(fixtures: list[Fixture]) -> str:
    if not fixtures:
        return "  (no matches for this filter)"
    lines = []
    last_day = None
    for f in fixtures:
        day = f.et.strftime("%a %b %d (ET)")
        if day != last_day:
            lines.append(f"\n{day}")
            last_day = day
        tag = "" if f.status in NOT_STARTED else f"  [{f.status}]"
        lines.append(
            f"  ET {f.et:%H:%M}  PT {f.pt:%H:%M}   {f.home:<16} vs {f.away:<16}"
            f"  · {f.round}{tag}")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="World Cup schedule in ET + PT")
    ap.add_argument("--date", help="US ET date YYYY-MM-DD (e.g. 2026-06-16)")
    ap.add_argument("--upcoming", action="store_true", help="only not-yet-played matches")
    ap.add_argument("--days", type=int, default=0, help="show the next N days (from today ET)")
    ap.add_argument("--refresh", action="store_true", help="re-pull fixture statuses first (1 API call)")
    args = ap.parse_args()

    if args.refresh:
        refresh_fixtures()
    fixtures = load_fixtures(et_date=args.date, upcoming=args.upcoming, days=args.days)
    scope = args.date or (f"next {args.days} days" if args.days else "all dates")
    print(f"World Cup schedule — {scope}{' · upcoming only' if args.upcoming else ''} (times in ET / PT)")
    print(render(fixtures))


if __name__ == "__main__":
    main()
