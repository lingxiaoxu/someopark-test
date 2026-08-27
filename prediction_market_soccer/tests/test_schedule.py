"""Tests for the schedule viewer (timezone boundary correctness + the kickoff clock).

The desk clock is ET (Kalshi / Polymarket US quote and settle there), so date filtering
is on the ET date. The SECOND clock is the one the match is actually played in, and which
one that is comes from the League Registry: CET for the eight European competitions, São
Paulo time for the four South American ones. (The WC copy showed US Pacific — meaningless
for a club calendar, where an evening European kickoff lands in the small hours PT and
reads as the wrong day.)
"""
from __future__ import annotations

from prediction_market_soccer.ingest import store
from prediction_market_soccer.ops.schedule import load_fixtures
from prediction_market_soccer.tests import clubctx


def _store_with_fixture(kickoff_utc: str, status="NS", comp=clubctx.EPL,
                        home=clubctx.ARSENAL, away=clubctx.IPSWICH):
    c = clubctx.mem_db()
    clubctx.seed_teams(c, home, away)
    for (api, _cid), name in ((home, "Arsenal"), (away, "Ipswich")):
        store.upsert(c, "team", {"api_id": api, "name": name, "updated_at": store.utcnow()},
                     pk=["api_id"])
    clubctx.seed_fixture(c, 1, home, away, comp=comp, status=status, kickoff_ts=kickoff_utc)
    return c


def test_utc_to_et_and_date_boundary():
    # 01:00 UTC on 9/16 = 21:00 ET on Tue 9/15 (the US desk date is the day before).
    c = _store_with_fixture("2026-09-16T01:00:00+00:00")
    fx = load_fixtures(conn=c)[0]
    assert fx.et.strftime("%Y-%m-%d %H:%M") == "2026-09-15 21:00"
    # Filtering by ET date 9/15 includes it; 9/16 does not.
    assert len(load_fixtures(conn=c, et_date="2026-09-15")) == 1
    assert len(load_fixtures(conn=c, et_date="2026-09-16")) == 0


def test_second_clock_is_the_one_the_match_is_played_in():
    """European fixture → CET; South American fixture → São Paulo. Decided by the
    registry, never by a team- or round-name guess."""
    from prediction_market_soccer.config.leagues import get
    c = _store_with_fixture("2026-09-16T01:00:00+00:00")
    label, kick = load_fixtures(conn=c)[0].local
    assert label == "CET"
    assert kick.strftime("%Y-%m-%d %H:%M") == "2026-09-16 03:00"

    lib = get("libertadores")
    sa_clubs = clubctx.comp_clubs("libertadores")[:2]
    c2 = _store_with_fixture("2026-09-16T01:00:00+00:00", comp=lib,
                             home=sa_clubs[0], away=sa_clubs[1])
    label2, kick2 = load_fixtures(conn=c2)[0].local
    assert label2 == "SA"
    assert kick2.strftime("%Y-%m-%d %H:%M") == "2026-09-15 22:00"   # UTC-3, year-round


def test_league_filter_and_unregistered_competition():
    c = _store_with_fixture("2026-09-16T01:00:00+00:00")
    assert len(load_fixtures(conn=c, league="epl")) == 1
    assert load_fixtures(conn=c, league="ucl") == []
    # A fixture from a league we do not trade carries an empty comp and defaults to CET
    # rather than crashing — the viewer stays usable on any stored row.
    c.execute("UPDATE fixture SET league_id=9999 WHERE api_id=1")
    fx = load_fixtures(conn=c)[0]
    assert fx.comp == "" and fx.local[0] == "CET"


def test_upcoming_filter():
    c = _store_with_fixture("2026-09-16T01:00:00+00:00", status="FT")
    assert load_fixtures(conn=c, upcoming=True) == []     # finished excluded
    assert len(load_fixtures(conn=c, upcoming=False)) == 1
