"""A finished match must not fall between the two sources the season sim reads.

The table comes from the API's `standing` feed, the fixtures to simulate come from our
own `fixture` rows. A just-finished match is already FT in `fixture` (so it is not
simulated) while the standings refresh hours later (so its points are not in the table
either) — the result vanishes from the season entirely.

The top-up that fixes this has a trap of its own: Argentina runs Apertura and Clausura
as separate tables under ONE league id, both tagged Stage.LEAGUE, so a season-wide
top-up folds all of finished Apertura into the Clausura table.
"""
from __future__ import annotations

import sqlite3

import pytest

from prediction_market_soccer.config.leagues import get
from prediction_market_soccer.model.league_season import _topup_unrecorded

_FETCHED = "2026-08-26T05:11:05+00:00"


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE standing (league_id INT, season INT, team_api_id INT, "
              "played INT, updated_at TEXT)")
    c.execute("CREATE TABLE fixture (league_id INT, season INT, round TEXT, "
              "home_api_id INT, away_api_id INT, home_goals INT, away_goals INT, "
              "status_short TEXT, kickoff_ts TEXT)")
    return c


def _seed_standing(c, comp, teams, played, fetched=_FETCHED):
    for t in teams:
        c.execute("INSERT INTO standing VALUES (?,?,?,?,?)",
                  (comp.api_football_id, comp.season, t, played, fetched))


def _fx(c, comp, rnd, h, a, hg, ag, ko):
    c.execute("INSERT INTO fixture VALUES (?,?,?,?,?,?,?,?,?)",
              (comp.api_football_id, comp.season, rnd, h, a, hg, ag, "FT", ko))


def test_result_after_the_standings_were_fetched_is_applied():
    comp = get("laliga")
    c = _conn()
    _seed_standing(c, comp, [1, 2], played=1)
    # Round 2 kicked off BEFORE the standings were read and is already in them…
    _fx(c, comp, "Regular Season - 2", 1, 2, 2, 1, "2026-08-23T17:30:00+00:00")
    # …round 1 kicked off 14 hours AFTER, so it cannot be.
    _fx(c, comp, "Regular Season - 1", 1, 2, 4, 1, "2026-08-26T19:00:00+00:00")
    table = {"clubA": {"pts": 3, "gd": 1, "gf": 2, "played": 1},
             "clubB": {"pts": 0, "gd": -1, "gf": 1, "played": 1}}
    _topup_unrecorded(c, comp, table, {1: "clubA", 2: "clubB"})
    assert table["clubA"] == {"pts": 6, "gd": 4, "gf": 6, "played": 2}   # 3-0 win 4-1
    assert table["clubB"] == {"pts": 0, "gd": -4, "gf": 2, "played": 2}


def test_result_the_standings_already_contain_is_not_double_counted():
    """Same match, but the standings were fetched after it — `played` already covers it."""
    comp = get("laliga")
    c = _conn()
    _seed_standing(c, comp, [1, 2], played=2, fetched="2026-08-27T09:00:00+00:00")
    _fx(c, comp, "Regular Season - 1", 1, 2, 4, 1, "2026-08-26T19:00:00+00:00")
    table = {"clubA": {"pts": 6, "gd": 4, "gf": 6, "played": 2}}
    before = dict(table["clubA"])
    _topup_unrecorded(c, comp, table, {1: "clubA", 2: "clubB"})
    assert table["clubA"] == before


def test_split_season_does_not_fold_the_finished_half_into_the_running_one():
    """Argentina: Apertura is over and has its own table; Clausura is the live one."""
    comp = get("argentina")
    c = _conn()
    _seed_standing(c, comp, [1, 2], played=6)                 # six Clausura rounds
    for i in range(16):                                        # a whole finished Apertura
        _fx(c, comp, f"Apertura - {i + 1}", 1, 2, 2, 0, f"2026-0{2 + i // 8}-0{1 + i % 8}T19:00:00+00:00")
    table = {"clubA": {"pts": 12, "gd": 5, "gf": 9, "played": 6}}
    _topup_unrecorded(c, comp, table, {1: "clubA", 2: "clubB"})
    assert table["clubA"]["played"] == 6, "finished Apertura leaked into the Clausura table"


def test_no_standings_is_a_noop_not_a_crash():
    comp = get("epl")
    c = _conn()
    _fx(c, comp, "Regular Season - 1", 1, 2, 1, 0, "2026-08-26T19:00:00+00:00")
    table = {"clubA": {"pts": 0, "gd": 0, "gf": 0, "played": 0}}
    _topup_unrecorded(c, comp, table, {1: "clubA"})
    assert table["clubA"]["played"] == 0


@pytest.mark.parametrize("hg,ag,pts,gd", [(2, 0, 3, 2), (1, 1, 1, 0), (0, 3, 0, -3)])
def test_points_and_goal_difference_follow_the_result(hg, ag, pts, gd):
    comp = get("epl")
    c = _conn()
    _seed_standing(c, comp, [1, 2], played=0)
    _fx(c, comp, "Regular Season - 1", 1, 2, hg, ag, "2026-08-26T19:00:00+00:00")
    table = {"clubA": {"pts": 0, "gd": 0, "gf": 0, "played": 0}}
    _topup_unrecorded(c, comp, table, {1: "clubA", 2: "clubB"})
    assert (table["clubA"]["pts"], table["clubA"]["gd"]) == (pts, gd)
