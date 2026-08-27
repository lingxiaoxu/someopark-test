"""League/competition FORMAT rules — the club replacement for the WC group tie-break tests.

A World Cup had one format: four-team groups ranked by points → GD → GF → head-to-head
among the tied subset. Twelve club competitions have twelve, and they are REGISTRY data:
how many clubs, how many go down, how many go up into Europe, where the swiss league
phase cuts, whether the table is one league or two zones.

These tests pin the season ranking machinery (model/league_season) against those registry
facts. They use a DECIDED table with nothing left to play, so the Monte-Carlo collapses to
a pure ranking function and the assertions are exact rather than statistical.

v1 tie-break disclosure (MODEL_NOTES / R4): the sim ranks on pts > GD > GF for every
league, so La Liga's and Serie A's head-to-head criterion is APPROXIMATED by goal
difference. The registry still records each league's official rule, and
`test_registry_records_each_leagues_official_tiebreak` is what will fail loudly when the
exact in-sim pair matrix lands and someone forgets to change one of them.
"""
from __future__ import annotations

import pytest

from prediction_market_soccer.config.leagues import REGISTRY, get
from prediction_market_soccer.ingest import store
from prediction_market_soccer.model.league_season import simulate_season
from prediction_market_soccer.tests import clubctx


def _decided_table(comp_key, rows, *, zones=None):
    """A store holding only a finished table: standings rows, no unplayed fixtures.

    ``rows`` = [(pts, gd, gf)] applied to the competition's clubs in prior order.
    With nothing left to play the sim ranks the table as-is, which is exactly what a
    format test wants to assert on.
    """
    comp = get(comp_key)
    c = clubctx.mem_db()
    clubs = clubctx.comp_clubs(comp_key)[:len(rows)]
    clubctx.seed_teams(c, *clubs)
    for i, ((api, _cid), (pts, gd, gf)) in enumerate(zip(clubs, rows)):
        store.upsert(c, "standing", {
            "league_id": comp.api_football_id, "season": comp.season, "team_api_id": api,
            "rank": i + 1, "points": pts, "goals_diff": gd, "played": 38,
            "raw_json": '{"all":{"goals":{"for":%d}},"group":%s}' % (
                gf, ('"%s"' % zones[i]) if zones else "null"),
            "updated_at": store.utcnow()}, pk=["league_id", "season", "team_api_id"])
    return c, [cid for _, cid in clubs]


def _sim(c, comp_key, n_sims=200):
    return simulate_season(c, comp_key, clubctx.strength_for(comp_key),
                           n_sims=n_sims, seed=1)


# ── the ranking key ──────────────────────────────────────────────────────────
def test_rank_key_is_points_then_goal_difference_then_goals_for():
    """Three separations in one table: A beats B on points, B beats C on goal
    difference at equal points, C beats D on goals for at equal points AND GD."""
    c, clubs = _decided_table("epl", [(80, 40, 90),    # A — most points
                                      (70, 30, 70),    # B — same pts as C, better GD
                                      (70, 20, 60),    # C — same pts+GD as D, more GF
                                      (70, 20, 55)])   # D
    sim = _sim(c, "epl")
    a, b, cc, d = clubs
    assert sim.p_champion[a] == 1.0                      # decided → snapped, not 0.999
    assert [sim.e_rank[x] for x in (a, b, cc, d)] == [1.0, 2.0, 3.0, 4.0]


def test_registry_records_each_leagues_official_tiebreak():
    """The official rule per league. v1 ranks every league on pts > GD > GF, so the
    two head-to-head leagues are knowingly approximated — this test is the reminder."""
    assert get("epl").tiebreak == "pts_gd_gf"
    assert get("ligue1").tiebreak == "pts_gd_gf"
    assert get("laliga").tiebreak == "pts_h2h_gd"        # H2H first — approximated in v1
    assert get("seriea").tiebreak == "pts_h2h_gd"        # H2H first — approximated in v1
    assert get("bundesliga").tiebreak == "pts_gd_gf_h2h"
    # A cup has no table, so it declares no tie-break at all.
    assert get("libertadores").tiebreak == ""
    assert get("ucl").tiebreak == "pts_gd_gf"            # the swiss league phase does


def test_every_registered_competition_declares_a_coherent_format():
    """Registry sanity across all fourteen entries (twelve live + two extension slots):
    a competition that trades a relegation market must have somewhere to relegate to,
    and a European cut cannot exceed the field."""
    for key, comp in REGISTRY.items():
        assert comp.n_teams > 0, key
        assert comp.releg_direct + comp.releg_playoff < comp.n_teams, key
        assert comp.top_n <= comp.n_teams, key
        if comp.kind == "league":
            assert comp.tiebreak, key                    # a table needs an order
        if comp.kind == "swiss_ucl":
            assert comp.qual_direct < comp.qual_playoff <= comp.n_teams, key


# ── relegation, including the half-weighted play-off spot ────────────────────
def test_relegation_mass_matches_the_registrys_direct_drops():
    """The EPL drops three and has no play-off, so exactly three relegation slots exist."""
    n = get("epl").n_teams
    c, clubs = _decided_table("epl", [(80 - 3 * i, 40 - 4 * i, 90 - 3 * i) for i in range(n)])
    sim = _sim(c, "epl")
    assert get("epl").releg_playoff == 0
    assert abs(sum(sim.p_relegation.values()) - 3.0) < 1e-4
    assert sim.p_relegation[clubs[-1]] == 1.0 and sim.p_relegation[clubs[0]] == 0.0
    assert sim.p_last[clubs[-1]] == 1.0


def test_relegation_playoff_spot_is_half_weighted():
    """The Bundesliga drops two automatically and sends the 16th into a play-off. That
    club is not relegated and not safe, so it counts half — total mass 2.5, not 3."""
    comp = get("bundesliga")
    assert (comp.releg_direct, comp.releg_playoff) == (2, 1)
    n = comp.n_teams
    c, clubs = _decided_table("bundesliga",
                              [(80 - 3 * i, 40 - 4 * i, 90 - 3 * i) for i in range(n)])
    sim = _sim(c, "bundesliga")
    assert abs(sum(sim.p_relegation.values()) - 2.5) < 1e-4
    assert sim.p_relegation[clubs[-1]] == 1.0                 # bottom: down
    assert abs(sim.p_relegation[clubs[-3]] - 0.5) < 1e-4      # play-off place: half
    assert sim.p_relegation[clubs[-4]] == 0.0                 # safe


def test_top_n_cut_matches_the_traded_market():
    """Each league's 'top-N' market is the European cut the venue actually trades."""
    n = get("epl").n_teams
    c, clubs = _decided_table("epl", [(80 - 3 * i, 40 - 4 * i, 90 - 3 * i) for i in range(n)])
    sim = _sim(c, "epl")
    assert get("epl").top_n == 4
    assert abs(sum(sim.p_top_n.values()) - 4.0) < 1e-4
    assert all(sim.p_top_n[x] == 1.0 for x in clubs[:4])
    assert sim.p_top_n[clubs[4]] == 0.0


# ── swiss league phase (UCL/UEL/UECL) ────────────────────────────────────────
def test_swiss_phase_qualification_cuts():
    """A 36-club swiss league phase sends the top 8 straight to the Round of 16 and
    ranks 9-24 into the knockout play-off; 25-36 are out. Those cuts replace the WC
    'two per group plus best thirds' rule entirely."""
    comp = get("ucl")
    c, clubs = _decided_table("ucl", [(30 - i, 25 - i, 40 - i) for i in range(36)])
    sim = _sim(c, "ucl")
    assert (comp.qual_direct, comp.qual_playoff) == (8, 24)
    assert abs(sum(sim.p_qual_direct.values()) - 8.0) < 1e-4
    assert abs(sum(sim.p_qual_playoff.values()) - 16.0) < 1e-4   # ranks 9..24
    assert sim.p_qual_direct[clubs[7]] == 1.0 and sim.p_qual_direct[clubs[8]] == 0.0
    assert sim.p_qual_playoff[clubs[8]] == 1.0                   # 9th: play-off
    assert sim.p_qual_playoff[clubs[24]] == 0.0                  # 25th: eliminated


def test_domestic_league_has_no_swiss_cuts():
    c, _clubs = _decided_table("epl", [(80 - 3 * i, 40 - 4 * i, 90 - 3 * i)
                                       for i in range(get("epl").n_teams)])
    sim = _sim(c, "epl")
    assert sim.p_qual_direct is None and sim.p_qual_playoff is None


# ── zoned competitions ───────────────────────────────────────────────────────
def test_zoned_competition_table_is_reported_per_zone():
    """Argentina runs two 15-club zones. Merging them into one 30-club table would make
    its 'top 8' meaningless, so the published table is grouped by zone first, then by the
    usual pts > GD > GF inside each."""
    zones = ["A"] * 4 + ["B"] * 4
    c, clubs = _decided_table(
        "argentina",
        [(30, 10, 25), (20, 5, 18), (28, 9, 22), (18, 2, 15),      # zone A entries
         (26, 8, 21), (16, 1, 12), (24, 7, 20), (14, 0, 10)],
        zones=zones)
    sim = _sim(c, "argentina")
    table = sim.table_now
    assert [r["zone"] for r in table] == ["A", "A", "A", "A", "B", "B", "B", "B"]
    for half in (table[:4], table[4:]):
        pts = [r["pts"] for r in half]
        assert pts == sorted(pts, reverse=True)


# ── the cup-state guard ──────────────────────────────────────────────────────
def test_cup_state_competition_returns_an_empty_season_sim():
    """A cup with no table and no league fixtures has no season to simulate; returning
    an empty result (rather than ranking 200k copies of an all-zero table) is what stops
    the champion card reading as an alphabetical accident. run_model swaps in the KO
    tree instead."""
    c = clubctx.mem_db()
    clubs = clubctx.comp_clubs("libertadores")[:4]
    clubctx.seed_teams(c, *clubs)
    sim = _sim(c, "libertadores")
    assert sim.n_sims == 0 and sim.n_remaining == 0
    assert sim.p_champion == {} and sim.table_now == []


# ── decided-off-the-pitch fixtures ───────────────────────────────────────────
@pytest.mark.parametrize("status,remaining", [
    ("NS", 1), ("PST", 1), ("TBD", 1),          # genuinely still to play
    ("FT", 0), ("AET", 0), ("PEN", 0),          # played out
    ("AWD", 0), ("WO", 0),                      # awarded / walkover — decided off the pitch
    ("CANC", 0), ("ABD", 0),                    # cancelled / abandoned — never replayed here
])
def test_only_genuinely_unplayed_fixtures_are_simulated(status, remaining):
    """AWD/WO results are already carried by the official standings, so re-simulating
    them double-counted their points — that is what invented a phantom Libertadores
    title chance for a club that had been knocked out."""
    comp = get("epl")
    c, clubs = _decided_table("epl", [(80 - 3 * i, 40 - 4 * i, 90 - 3 * i)
                                      for i in range(comp.n_teams)])
    clubctx.seed_fixture(c, 900, clubctx.comp_clubs("epl")[0], clubctx.comp_clubs("epl")[1],
                         status=status, days_ago=-2.0)
    assert _sim(c, "epl").n_remaining == remaining
