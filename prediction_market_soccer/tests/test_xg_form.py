"""xG-form alpha: PIT correctness + bounded rating blend."""
from prediction_market_soccer.ingest import store
from prediction_market_soccer.model.xg_form import xg_form_adjusted_ratings, xg_form_index


def _db():
    c = store.init_db(":memory:") if False else None
    import sqlite3
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row; store.init_db(c)
    c.execute("INSERT INTO team(api_id,name) VALUES (10,'A'),(11,'B'),(12,'C')")
    c.execute("INSERT INTO team_meta(api_id,canonical_team_id) VALUES (10,'a'),(11,'b'),(12,'c')")
    # two finished fixtures, chronological
    c.execute("INSERT INTO fixture(api_id,home_api_id,away_api_id,status_short,home_goals,away_goals,kickoff_ts) "
              "VALUES (1,10,11,'FT',1,0,'2026-06-11T19:00:00+00:00'),"
              "(2,10,12,'FT',0,0,'2026-06-15T19:00:00+00:00')")
    # A dominated game 1 on xG (2.0 vs 0.3); game 2 even (1.0 vs 1.0)
    c.execute("INSERT INTO fixture_stats(fixture_api_id,team_api_id,xg) VALUES "
              "(1,10,2.0),(1,11,0.3),(2,10,1.0),(2,12,1.0)")
    c.commit()
    return c


def test_xg_form_is_point_in_time():
    c = _db()
    # Before any match → no team has xG-form.
    assert xg_form_index(c, as_of="2026-06-11T18:00:00+00:00") == {}
    # As of game 2's kickoff → only game 1 counts (A and B), not game 2.
    idx = xg_form_index(c, as_of="2026-06-15T19:00:00+00:00")
    assert set(idx) == {"a", "b"}
    assert idx["a"].n == 1 and idx["a"].weighted_net_xg > 0   # A out-created in game 1
    assert idx["b"].weighted_net_xg < 0                       # B was out-created
    # As of now → both games count for A (net xG averaged).
    full = xg_form_index(c)
    assert full["a"].n == 2 and set(full) == {"a", "b", "c"}


def test_xg_form_blend_is_bounded_and_optional():
    from prediction_market_soccer.tests.clubctx import epl_strength
    sm = epl_strength()
    idx = xg_form_index(_db())
    # weight 0 → unchanged
    assert xg_form_adjusted_ratings(sm, idx, 0.0) is sm
    adj = xg_form_adjusted_ratings(sm, idx, 0.15)
    b = sm.cfg.rating_bound
    assert all(-b <= r <= b for r in adj.ratings.values())   # stays within rating bound
