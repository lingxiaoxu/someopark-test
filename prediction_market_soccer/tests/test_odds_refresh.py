"""Bookmaker odds are re-pulled once a fixture nears kickoff with old odds; otherwise pulled once."""
from datetime import datetime, timedelta, timezone

from prediction_market_soccer.config.leagues import active
from prediction_market_soccer.ingest import soccer_ingest as si, store


class FakeApi:
    def __init__(self):
        self.asked = []

    def odds(self, fid):
        self.asked.append(fid)
        return []


def _iso(dt):
    return dt.isoformat()


def test_stale_odds_near_kickoff_are_refreshed(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "soccer.db")
    conn = store.init_db()
    comp = active()[0]
    now = datetime.now(timezone.utc)
    fixtures = {
        1: (now + timedelta(hours=10), None),                      # never pulled
        2: (now + timedelta(hours=10), now - timedelta(days=4)),   # near kickoff, odds old → refresh
        3: (now + timedelta(hours=10), now - timedelta(hours=2)),  # near kickoff, odds fresh
        4: (now + timedelta(days=5), now - timedelta(days=2)),     # far from kickoff → pulled once only
    }
    for fid, (kickoff, fetched) in fixtures.items():
        conn.execute("INSERT INTO fixture (api_id, league_id, season, kickoff_ts, status_short) VALUES (?,?,?,?, 'NS')",
                     (fid, comp.api_football_id, comp.season, _iso(kickoff)))
        if fetched:
            conn.execute("INSERT INTO match_odds (fixture_api_id, bookmaker, p_home, p_draw, p_away, overround, raw_json, fetched_at) "
                         "VALUES (?, 'Bet365', 0.4, 0.3, 0.3, 0.05, '{}', ?)", (fid, _iso(fetched)))
    conn.commit()
    api = FakeApi()
    si.sync_odds(api, conn, limit=30, include_settled=False)
    assert sorted(api.asked) == [1, 2]
