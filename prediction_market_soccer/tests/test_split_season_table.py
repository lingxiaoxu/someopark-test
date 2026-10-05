"""Live prior tables match the point-in-time reconstruction when the official feed is half a season."""
from datetime import datetime, timedelta, timezone

from prediction_market_soccer.config.leagues import active
from prediction_market_soccer.ingest import club_prior, store

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")
TOMORROW = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")


def _comp(key):
    return next(c for c in active() if c.key == key)


def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "soccer.db")
    return store.init_db()


def _fixtures(conn, comp, rounds, start_id=1):
    """Team 1 beats team 2 in every listed round, all finished in the past."""
    kickoff = datetime.now(timezone.utc) - timedelta(days=60)
    for i, rnd in enumerate(rounds):
        conn.execute("INSERT INTO fixture (api_id, league_id, season, round, kickoff_ts, status_short, "
                     "home_api_id, away_api_id, home_goals, away_goals) VALUES (?,?,?,?,?, 'FT', 1, 2, 1, 0)",
                     (start_id + i, comp.api_football_id, comp.season, rnd, (kickoff + timedelta(days=i)).isoformat()))


def _standing(conn, comp, played, points=None):
    for tid, pts in ((1, 3 * played if points is None else points), (2, 0)):
        conn.execute("INSERT INTO standing (league_id, season, group_code, team_api_id, rank, points, played) "
                     "VALUES (?,?,?,?,?,?,?)", (comp.api_football_id, comp.season, "G", tid, tid, pts, played))


def test_half_season_feed_is_replaced_by_the_point_in_time_reconstruction(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    comp = _comp("argentina")
    _fixtures(conn, comp, [f"Apertura - {i}" for i in range(1, 9)] + ["Clausura - 1", "Clausura - 2"])
    _standing(conn, comp, played=2)                       # the feed lists only the Clausura
    live = club_prior._current_table(conn, comp, None, TODAY)
    pit = club_prior._current_table(conn, comp, None, TOMORROW, point_in_time=True)
    assert live == pit
    assert live[1]["played"] == 10 and live[1]["points"] == 30


def test_full_season_feed_is_kept_with_its_deduction(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    comp = _comp("epl")
    _fixtures(conn, comp, [f"Regular Season - {i}" for i in range(1, 11)])
    _standing(conn, comp, played=10, points=27)           # 30 earned, 3 deducted
    live = club_prior._current_table(conn, comp, None, TODAY)
    assert live[1]["points"] == 27 and live[1]["played"] == 10


def test_a_feed_one_round_behind_is_not_mistaken_for_half_a_season(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    comp = _comp("epl")
    _fixtures(conn, comp, [f"Regular Season - {i}" for i in range(1, 11)])
    _standing(conn, comp, played=9)
    assert club_prior._current_table(conn, comp, None, TODAY)[1]["played"] == 9


def test_playoff_rounds_are_not_league_games(tmp_path, monkeypatch):
    conn = _db(tmp_path, monkeypatch)
    comp = _comp("argentina")
    _fixtures(conn, comp, [f"Apertura - {i}" for i in range(1, 9)] + ["Apertura - Round of 16", "Clausura - 1"])
    _standing(conn, comp, played=1)
    assert club_prior._current_table(conn, comp, None, TODAY)[1]["played"] == 9
