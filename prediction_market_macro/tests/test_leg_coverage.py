"""Every Brier in this project is computed on the QUOTED subsample. Say so, in the report.

WHY THIS FILE EXISTS
--------------------
`param_grid._release_universe`, `eval._chronos_replay` and `ts_replay.replay_series` all
score a settled leg only if `_market_leg_prob` returns a mid at `asof`, and all three
drop the rest with a bare `continue`. **The drop is correct and is not changed here**:
the market baseline defines the universe so that every variant, parameter set and lag is
scored on identical legs, and a variant cannot win by being handed easier ones.

What was wrong is that the drop was *silent*. Measured on the live book, 80.4% of settled
legs across the fourteen macro series have no two-sided quote at close−1h
(`docs/PLAN_DFM_SYNTH.md` §5e, 6718 of 8360; 99.9% of those are 404 sentinels for tickers
that never carried a quote in their lives). A report that prints a Brier and no
denominator therefore reads as a statement about the book when it is a statement about a
fifth of it — and a fifth selected jointly on liquidity and uncertainty, not at random.

So the rule these tests enforce is narrow and mechanical: **a scorer that drops legs must
emit how many it dropped.** They assert the count is present, that it is the true ratio
and not a placeholder, and — the part that actually catches a regression — that the
ratio MOVES when a leg's quote is taken away. A hardcoded 1.0 passes a presence check.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.research import param_grid

CLOSE = datetime(2026, 3, 5, 21, 0, tzinfo=timezone.utc)


def _seed(conn, quoted: int, unquoted: int):
    """One settled claims period with `quoted` legs that have a bar at close−1h and
    `unquoted` legs that have none — the R1 shape §5e counted, which is 99.9% of the
    real unquoted book."""
    # the Kalshi token, not the ISO key — `_release_universe` runs it through
    # `kalshi_period_to_key`, and seeding the ISO form would make the whole event vanish
    # before a single leg was counted.
    period = "26MAR07"
    bar_ts = int((CLOSE - timedelta(hours=2)).timestamp())
    for i in range(quoted + unquoted):
        t = f"KXJOBLESSCLAIMS-26MAR07-T{200 + i}"
        conn.execute(
            "INSERT INTO contracts(ticker, series, event_ticker, period, floor_strike,"
            " strike_type, close_time, first_seen_ts) VALUES(?,?,?,?,?,?,?,?)",
            (t, "KXJOBLESSCLAIMS", "KXJOBLESSCLAIMS-26MAR07", period,
             200000.0 + 1000 * i, "greater_or_equal",
             CLOSE.isoformat().replace("+00:00", "Z"), CLOSE.isoformat()))
        conn.execute(
            "INSERT INTO settlements(ticker, series, period, result, first_seen_ts)"
            " VALUES(?,?,?,?,?)", (t, "KXJOBLESSCLAIMS", period,
                                   "yes" if i % 2 else "no", CLOSE.isoformat()))
        if i < quoted:
            conn.execute(
                "INSERT INTO candles(ticker, end_ts, yes_bid_close, yes_ask_close,"
                " price_close, volume) VALUES(?,?,?,?,?,?)",
                (t, bar_ts, 0.40, 0.44, 0.42, 12))
        else:
            # the 404 sentinel `ingest/kalshi_md.py` writes: one all-NULL row meaning
            # "Kalshi never generated candlesticks for this ticker". It must be rejected
            # by the same rule as a genuinely absent bar, and it must be COUNTED.
            conn.execute(
                "INSERT INTO candles(ticker, end_ts, yes_bid_close, yes_ask_close,"
                " price_close, volume) VALUES(?,0,NULL,NULL,NULL,NULL)", (t,))
    conn.commit()


@pytest.fixture()
def conn(tmp_path):
    c = init_db(tmp_path / "cov.db")
    _seed(c, quoted=3, unquoted=7)
    return c


def test_release_universe_counts_the_legs_it_drops(conn):
    cov: dict = {}
    universe = param_grid._release_universe(conn, cov)
    assert cov["legs_settled"] == 10, "the denominator must be every SETTLED leg"
    assert cov["legs_scored"] == 3, "only the quoted legs are scorable"
    assert len(universe) == 1 and len(universe[0]["legs"]) == 3


def test_the_count_tracks_the_book_rather_than_being_a_constant(tmp_path):
    """The test that would fail on a hardcoded 1.0 or on a count of the wrong thing."""
    seen = []
    for q in (1, 5, 9):
        c = init_db(tmp_path / f"cov{q}.db")
        _seed(c, quoted=q, unquoted=10 - q)
        cov: dict = {}
        param_grid._release_universe(c, cov)
        seen.append((cov["legs_settled"], cov["legs_scored"]))
    assert seen == [(10, 1), (10, 5), (10, 9)]


def test_the_sentinel_row_is_rejected_and_still_counted(tmp_path):
    """A settled leg whose only candle is the all-NULL 404 sentinel is unquoted, not
    absent. If a future reader treated the sentinel as a bar, `legs_scored` would jump
    to 10 here and the coverage number would silently become 1.0."""
    c = init_db(tmp_path / "sent.db")
    _seed(c, quoted=0, unquoted=10)
    cov: dict = {}
    assert param_grid._release_universe(c, cov) == []
    assert (cov["legs_settled"], cov["legs_scored"]) == (10, 0)


def test_default_call_still_works_without_a_counter(conn):
    """`cov` is optional. Existing callers must not have to pass it — the counting is
    additive reporting, not a signature change with teeth."""
    assert len(param_grid._release_universe(conn)) == 1


def test_all_three_scorers_expose_a_coverage_key():
    """Structural: the two heavier scorers are integration-tested elsewhere, but the
    contract 'if you drop legs you report the ratio' is checked on their source so that
    deleting the field is a test failure rather than a quiet loss of scope."""
    import inspect

    from prediction_market_macro.research import eval as ev
    from prediction_market_macro.research import ts_replay

    for mod in (param_grid, ev, ts_replay):
        src = inspect.getsource(mod)
        assert "leg_coverage" in src, f"{mod.__name__} drops legs without reporting it"
