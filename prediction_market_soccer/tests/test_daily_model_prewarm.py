"""Which competitions need today's UTC daily model built — including ones only in play."""
from datetime import datetime, timedelta, timezone

from prediction_market_soccer.config.leagues import active
from prediction_market_soccer.ingest import store
from prediction_market_soccer.ops import daily_model_prewarm as dmp

NOW = datetime(2026, 10, 3, 0, 5, tzinfo=timezone.utc)


def _db(tmp_path, monkeypatch, fixtures):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "soccer.db")
    conn = store.init_db()
    for fid, (comp, status, kickoff) in enumerate(fixtures, 1):
        conn.execute("INSERT INTO fixture (api_id, league_id, season, kickoff_ts, status_short) VALUES (?,?,?,?,?)",
                     (fid, comp.api_football_id, comp.season, kickoff.isoformat(), status))
    conn.commit()
    return conn


def test_in_play_and_upcoming_competitions_are_due(tmp_path, monkeypatch):
    comps = active()[:4]
    conn = _db(tmp_path, monkeypatch, [
        (comps[0], "NS", NOW + timedelta(hours=5)),     # upcoming today
        (comps[1], "2H", NOW - timedelta(minutes=70)),  # in play across midnight
        (comps[2], "FT", NOW - timedelta(hours=3)),     # finished — nothing to decide
        (comps[3], "NS", NOW + timedelta(days=3)),      # beyond the 24h horizon
    ])
    assert sorted(dmp.due_competitions(conn, NOW)) == sorted([comps[0].key, comps[1].key])


def test_only_competitions_without_todays_model_are_missing(tmp_path, monkeypatch):
    comps = active()[:2]
    conn = _db(tmp_path, monkeypatch, [(c, "NS", NOW + timedelta(hours=2)) for c in comps])
    built = {comps[0].key}
    seen_days = set()

    def read_versions(conn, source, *, cutoff=None, entity_key=None):
        assert source == "derived:daily_strength"
        seen_days.add(entity_key["day"])
        return [{"payload": "x"}] if entity_key["comp"] in built else []
    monkeypatch.setattr("prediction_market_soccer.util.source_history._read_versions", read_versions)
    assert dmp.missing(conn, NOW) == [comps[1].key]
    assert seen_days == {"2026-10-03"}                  # keyed by the UTC day of `now`
