"""The collection budget must fund the work its own scope asks for.

2026-09-10 → 09-22 full_refresh sat `degraded` on price_ticks for twelve days. Nothing
was broken: one discovery sweep costs a fixed ~44 requests (12 competitions x 14 tag
slugs, paginated — measured 2026-09-22: a cap of 45 and a cap of 400 both return the same
207 events in 44 HTTP calls), and it is charged to the SAME budget as the per-fixture
price histories. With the default 48 that left 4 requests for the 36 histories a
12-fixture scope needs, so every run ended `partial`, 278 targets recorded
`request_budget_exhausted`, and one was retried 33 times without ever being serviced.
"""
import sqlite3

from prediction_market_soccer.util import collection_state as cs


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE fixture(api_id INTEGER PRIMARY KEY, league_id INTEGER,
        kickoff_ts TEXT, status_short TEXT, home_goals INTEGER)""")
    cs.ensure(conn)
    return conn


def _seed(conn, n, league_id, kickoff="2026-09-20T12:00:00+00:00"):
    for i in range(n):
        conn.execute("INSERT INTO fixture VALUES(?,?,?,?,?)",
                     (9000 + i, league_id, kickoff, "FT", 1))
    conn.commit()


def _one_active_league_id():
    from prediction_market_soccer.config.leagues import active
    return active()[0].api_football_id


def test_the_budget_covers_discovery_plus_every_fixtures_histories():
    conn = _db()
    lid = _one_active_league_id()
    _seed(conn, 12, lid)
    sc = cs.daily_scope(conn, now="2026-09-22T00:00:00+00:00", limit=12, collector="ticks")
    need = 44 + cs.REQUESTS_PER_FIXTURE * len(sc.fixture_ids)   # measured discovery cost
    assert len(sc.fixture_ids) == 12
    assert sc.max_requests >= need, (
        f"budget {sc.max_requests} cannot fund {need} requests — the starvation that kept "
        "full_refresh degraded for twelve days")
    assert sc.max_requests == cs.DISCOVERY_ALLOWANCE + cs.REQUESTS_PER_FIXTURE * 12


def test_the_budget_scales_with_the_fixtures_actually_selected():
    """A short scope must not carry a twelve-fixture budget: the cap is a safety bound on
    API calls, so it tracks the work rather than the limit that was asked for."""
    conn = _db()
    lid = _one_active_league_id()
    _seed(conn, 3, lid)
    sc = cs.daily_scope(conn, now="2026-09-22T00:00:00+00:00", limit=12, collector="ticks")
    assert len(sc.fixture_ids) == 3
    assert sc.max_requests == cs.DISCOVERY_ALLOWANCE + cs.REQUESTS_PER_FIXTURE * 3


def test_an_empty_scope_still_funds_discovery_and_stays_valid():
    conn = _db()
    sc = cs.daily_scope(conn, now="2026-09-22T00:00:00+00:00", limit=12, collector="ticks")
    assert sc.fixture_ids == ()
    assert sc.max_requests == cs.DISCOVERY_ALLOWANCE      # construction must not raise


def test_milestones_shares_the_same_funding_rule():
    """Milestones pays the same discovery overhead and the same 3 histories per fixture
    (the receipt loop is per SIDE, not per milestone), and it exhausted its budget too —
    1,356 targets on 2026-09-20."""
    conn = _db()
    _seed(conn, 12, _one_active_league_id())
    sc = cs.daily_scope(conn, now="2026-09-22T00:00:00+00:00", limit=12, collector="milestones")
    assert sc.max_requests == cs.DISCOVERY_ALLOWANCE + cs.REQUESTS_PER_FIXTURE * len(sc.fixture_ids)
