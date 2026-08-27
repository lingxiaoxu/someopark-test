"""Club-fixture schedule viewer across the desk's working clocks. Fixed, reusable tool.

Fixtures are stored with UTC kickoff timestamps (API-Football). This renders each one in
**US Eastern (America/New_York)** — the DESK clock, because Kalshi and Polymarket US quote
and settle on ET — beside the clock the match is actually played in:

  * **CET (Europe/Paris)** for the eight European competitions (EPL/La Liga/Serie A/
    Bundesliga/Ligue 1/UCL/UEL/UECL). The WC copy of this file showed US Pacific, which
    is meaningless here: nothing in the club calendar is played on the US west coast, and
    an evening European kickoff lands in the small hours PT where it reads as the wrong day.
  * **SA (America/Sao_Paulo, UTC-3 all year)** for the four South American ones
    (Libertadores / Sudamericana / Brasileirão / Argentina).

Which clock a fixture gets is decided by the League Registry (config/leagues.py), never by
a round-name or team-name guess.

IMPORTANT: a match at 01:00 UTC on calendar day D+1 is the evening of day D in ET
(e.g. 2026-09-16 01:00 UTC = Mon 2026-09-15 21:00 ET / Tue 03:00 CET). Date filtering is
therefore done on the **ET date**, not the UTC date — one desk day, one screen.

Usage:
    # today's (ET) not-yet-played matches, from stored data (0 API calls)
    conda run -n someopark_run python -m prediction_market_soccer.ops.schedule --upcoming
    # one competition only
    conda run -n someopark_run python -m prediction_market_soccer.ops.schedule --league epl --days 7
    # a specific desk date, refreshing statuses first (1 API call PER competition)
    conda run -n someopark_run python -m prediction_market_soccer.ops.schedule --date 2026-09-15 --upcoming --refresh
    # next N days
    conda run -n someopark_run python -m prediction_market_soccer.ops.schedule --days 3
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")     # desk clock: Kalshi / Polymarket US quote and settle here
CET = ZoneInfo("Europe/Paris")        # kickoff clock for the eight European competitions
SA = ZoneInfo("America/Sao_Paulo")    # CONMEBOL + Brasileirão + Argentina (UTC-3 year-round)

# Competitions played on South American time; everything else in the registry is European.
SOUTH_AMERICAN = frozenset({"libertadores", "sudamericana", "brasileirao", "argentina"})

# Statuses that mean "not yet played" (API-Football).
NOT_STARTED = ("NS", "TBD", "PST")


@dataclass(frozen=True)
class Fixture:
    kickoff_utc: datetime
    home: str
    away: str
    round: str
    status: str
    comp: str = ""          # registry key ("epl", "ucl", …); "" when the league is not registered

    @property
    def et(self) -> datetime:
        return self.kickoff_utc.astimezone(ET)

    @property
    def local(self) -> tuple[str, datetime]:
        """(label, kickoff) in the clock the match is actually played in."""
        if self.comp in SOUTH_AMERICAN:
            return "SA", self.kickoff_utc.astimezone(SA)
        return "CET", self.kickoff_utc.astimezone(CET)


def load_fixtures(conn=None, *, et_date: str | None = None, upcoming: bool = False,
                  days: int = 0, league: str | None = None) -> list[Fixture]:
    """Stored fixtures, filtered by ET date / upcoming / next-N-days / competition, by kickoff."""
    from prediction_market_soccer.config.leagues import by_api_id
    from prediction_market_soccer.ingest import store

    conn = conn or store.init_db()
    rows = conn.execute(
        "SELECT f.kickoff_ts, f.round, f.status_short, f.league_id, th.name h, ta.name a "
        "FROM fixture f JOIN team th ON f.home_api_id=th.api_id "
        "JOIN team ta ON f.away_api_id=ta.api_id WHERE f.kickoff_ts IS NOT NULL "
        "ORDER BY f.kickoff_ts").fetchall()
    now_et = datetime.now(timezone.utc).astimezone(ET).date()
    comp_of: dict[int, str] = {}
    out: list[Fixture] = []
    for r in rows:
        try:
            ku = datetime.fromisoformat(r["kickoff_ts"]).astimezone(timezone.utc)
        except ValueError:
            continue
        lid = r["league_id"]
        if lid not in comp_of:
            c = by_api_id(lid) if lid is not None else None
            comp_of[lid] = c.key if c else ""
        fx = Fixture(ku, r["h"], r["a"], r["round"] or "", r["status_short"] or "", comp_of[lid])
        if league and fx.comp != league:
            continue
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


def refresh_fixtures(league: str | None = None) -> None:
    """Re-pull fixture lists to update kickoff times + statuses.

    One API call PER competition (the WC tournament was a single call) — so this is capped
    to the requested competition when --league is given, and costs 12 calls otherwise.
    """
    from prediction_market_soccer.config.leagues import active, get
    from prediction_market_soccer.ingest import store
    from prediction_market_soccer.ingest.api_football import ApiFootball
    from prediction_market_soccer.ingest.soccer_ingest import sync_fixtures

    conn = store.init_db()
    api = ApiFootball(conn)
    comps = [get(league)] if league else active()
    for comp in comps:
        sync_fixtures(api, conn, comp, force=True)


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
        lbl, loc = f.local
        stage = " · ".join(x for x in (f.comp, f.round) if x)
        # Club names run long ("Inter Club d'Escaldes"); clip to the column so the
        # competition/round column stays readable down the page.
        h, a = f.home[:20], f.away[:20]
        lines.append(
            f"  ET {f.et:%H:%M}  {lbl:>3} {loc:%H:%M}   {h:<20} vs {a:<20}"
            f"  · {stage}{tag}")
    return "\n".join(lines)


def main() -> None:
    from prediction_market_soccer.config.leagues import active

    keys = [c.key for c in active()]
    ap = argparse.ArgumentParser(
        description="Club schedule (12 competitions) in desk ET + local kickoff time (CET / SA)")
    ap.add_argument("--date", help="desk ET date YYYY-MM-DD (e.g. 2026-09-15)")
    ap.add_argument("--league", choices=keys, help=f"one competition only ({', '.join(keys)})")
    ap.add_argument("--upcoming", action="store_true", help="only not-yet-played matches")
    ap.add_argument("--days", type=int, default=0, help="show the next N days (from today ET)")
    ap.add_argument("--refresh", action="store_true",
                    help="re-pull fixture statuses first (1 API call per competition)")
    args = ap.parse_args()

    if args.refresh:
        refresh_fixtures(args.league)
    fixtures = load_fixtures(et_date=args.date, upcoming=args.upcoming, days=args.days,
                             league=args.league)
    scope = args.date or (f"next {args.days} days" if args.days else "all dates")
    where = args.league or "all 12 competitions"
    print(f"Club schedule — {where} · {scope}"
          f"{' · upcoming only' if args.upcoming else ''} (desk ET / local CET · SA)")
    print(render(fixtures))


if __name__ == "__main__":
    main()
