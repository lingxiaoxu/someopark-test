"""Shared club-context fixtures for the soccer test-suite (TRANSFORM_PLAN §2.2 (b)).

The WC suite could seed any two nation ids under ``league_id=1`` and any 2026-06
kickoff. The club module cannot: it is registry-gated end to end, so a seeded row
only reaches production code when three things agree —

  * ``fixture.league_id`` is an ENABLED competition's API-Football id
    (``performance_report._settled`` and ``upcoming_export.build`` both filter
    ``league_id IN (enabled comps)``, so a foreign id yields an empty sample);
  * ``fixture.season`` is that competition's season;
  * the canonical club ids exist in that competition's prior — a per-league
    strength model has ratings for its own clubs and nobody else, so a national
    team id raises ``KeyError`` inside ``pair_lambdas``.

Kickoffs are computed from the clock rather than hard-coded because the settled
paths look back a fixed 60 days: a literal date silently ages out of the window
and turns a real assertion into ``0 == 1`` months after it was written.

Club ids/api ids below come from ``data/priors/clubs_<comp>.json`` — the api ids
are the genuine API-Football team ids so a test DB can carry standings, squads or
player rows without inventing a second mapping.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from prediction_market_soccer.config.leagues import get
from prediction_market_soccer.ingest import store

EPL = get("epl")            # domestic league: single 3-way, draw terminal, no advance
UCL = get("ucl")            # UEFA: qualifying/KO are two-legged, ET before pens

# Round names that resolve through the registry's stage rules (leagues.stage_of).
LEAGUE_ROUND = "Regular Season - 3"     # Stage.LEAGUE
UEFA_LEAGUE_ROUND = "League Phase - 3"  # Stage.LEAGUE inside a swiss competition
KO_ROUND = "Round of 16"                # Stage.CUP_TWO_LEG
KO_FINAL = "Final"                      # Stage.CUP_SINGLE (neutral, ET then pens)

# (api_id, club_id) — EPL spans the whole strength range: Arsenal is the anchor
# favourite, Ipswich a promoted side, Brighton/Brentford a genuinely even pair.
ARSENAL = (42, "arsenal")
MAN_CITY = (50, "manchester_city")
CHELSEA = (49, "chelsea")
BRIGHTON = (51, "brighton")
BRENTFORD = (55, "brentford")
IPSWICH = (57, "ipswich")

# UCL qualifying-phase clubs (the UCL prior is the pre-draw 52-club superset).
LYON = (80, "lyon")
CELTIC = (247, "celtic")
TRE_FIORI = (2260, "tre_fiori")


def ts_ago(days: float, hour: int = 19) -> str:
    """ISO kickoff ``days`` before now, on a whole hour (stable within a test run)."""
    d = datetime.now(timezone.utc) - timedelta(days=days)
    return d.replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()


def mem_db() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    store.init_db(c)
    return c


def seed_teams(conn, *pairs) -> None:
    """team_meta rows for (api_id, club_id) pairs — the api_id → club_id map every
    export joins through."""
    for api_id, club_id in pairs:
        store.upsert(conn, "team_meta",
                     {"api_id": api_id, "canonical_team_id": club_id,
                      "updated_at": store.utcnow()}, pk=["api_id"])


def seed_fixture(conn, api_id, home, away, *, comp=EPL, round_name=LEAGUE_ROUND,
                 status="FT", hg=None, ag=None, days_ago: float = 3.0,
                 kickoff_ts: str | None = None, elapsed=None, raw_json=None) -> str:
    """One fixture wired to a real competition. Returns the kickoff timestamp so a
    caller can re-price the same match point-in-time."""
    ts = kickoff_ts or ts_ago(days_ago)
    store.upsert(conn, "fixture", {
        "api_id": api_id, "league_id": comp.api_football_id, "season": comp.season,
        "round": round_name, "status_short": status, "kickoff_ts": ts,
        "home_api_id": home[0], "away_api_id": away[0],
        "home_goals": hg, "away_goals": ag, "elapsed": elapsed,
        "raw_json": raw_json, "updated_at": store.utcnow()}, pk=["api_id"])
    return ts


# The reverse-fit is deterministic and the model is only READ by tests (every
# update/blend helper returns a new StrengthModel), so one fit per competition per
# session is enough — refitting the 399-club merged model in each of a dozen tests
# was the bulk of the suite's runtime.
@lru_cache(maxsize=None)
def strength_for(league: str | None):
    from prediction_market_soccer.ingest.club_prior import load_prior
    from prediction_market_soccer.model.strength import build_strength
    return build_strength(load_prior(league), league=league)


def comp_clubs(comp_key: str) -> list[tuple[int, str]]:
    """(api_team_id, club_id) for every club in one competition's prior."""
    from prediction_market_soccer.ingest.club_prior import load_prior
    return [(t.api_team_id, t.club_id) for t in load_prior(comp_key).teams]


def epl_season_db(*, played: int = 0):
    """A pre-season EPL store: 20 standings rows + the full 380-fixture calendar.

    The season Monte-Carlo starts from the standings table and plays out the unplayed
    LEAGUE-stage fixtures, so both halves have to be present — a DB with only one of
    them makes ``simulate_season`` return its empty cup-state result instead.
    """
    from prediction_market_soccer.ingest import store
    c = mem_db()
    clubs = comp_clubs("epl")
    seed_teams(c, *clubs)
    for rank, (api_id, _cid) in enumerate(clubs, start=1):
        store.upsert(c, "standing", {
            "league_id": EPL.api_football_id, "season": EPL.season, "team_api_id": api_id,
            "rank": rank, "points": 0, "goals_diff": 0, "played": played,
            "raw_json": '{"all":{"goals":{"for":0}}}', "updated_at": store.utcnow()},
            pk=["league_id", "season", "team_api_id"])
    fid = 1
    for i, home in enumerate(clubs):
        for j, away in enumerate(clubs):
            if i == j:
                continue
            seed_fixture(c, fid, home, away, round_name=f"Regular Season - {fid % 38 + 1}",
                         status="NS", days_ago=-30.0)
            fid += 1
    return c


def epl_strength():
    """The per-league EPL model — what every EPL export prices with (C2)."""
    return strength_for("epl")


def ucl_strength():
    return strength_for("ucl")


def all_comps_strength():
    """The merged cross-league model (the one the global/backtest paths use)."""
    return strength_for(None)
