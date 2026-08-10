"""The walk-forward window must not bleed into the live ledger.

`research/walkforward.run()` used to count back from wall-clock now(), so any run
long enough to reach June also re-traded everything the live paper ledger already
holds from 2026-07-31 on. Those two are displayed side by side as history and live
(TRACK_CUTOVER, ops/frontend_export.py) — an overlap double-counts the same events
in the same headline, and it is invisible in the output because both segments look
internally consistent.

`end` closes it on BOTH sides, and the second side is the one that is easy to miss:
bounding only the entry day still admits a trade opened 2026-07-29 that settles
2026-08-02, i.e. a position the live ledger is simultaneously carrying. So an event
qualifies only when it settles by `end` too.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.research.walkforward import _open_settled_events

END = datetime(2026, 7, 31, 23, 59, 59, tzinfo=timezone.utc)


@pytest.fixture()
def conn(tmp_path):
    c = init_db(tmp_path / "wf.db")
    # KXWTIW is in SERIES_DISPATCH; three events around the boundary
    for tok, close, result in (("26JUL24", "2026-07-24T20:00:00Z", "yes"),   # inside
                               ("26JUL31", "2026-07-31T20:00:00Z", "yes"),   # on the edge
                               ("26AUG07", "2026-08-07T20:00:00Z", "no")):   # past `end`
        tk = f"KXWTIW-{tok}-T70"
        c.execute("INSERT OR REPLACE INTO contracts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (tk, "KXWTIW", f"KXWTIW-{tok}", tok, None, "greater", 70.0, None,
                   close, "settled", "2026-06-01T00:00:00Z"))
        c.execute("INSERT OR REPLACE INTO settlements VALUES(?,?,?,?,?,?)",
                  (tk, "KXWTIW", tok, result, close, "2026-06-01T00:00:00Z"))
    c.commit()
    return c


def _toks(rows):
    return sorted(r["tok"] for r in rows)


def test_an_event_settling_after_end_is_not_a_candidate(conn):
    """The straddler is the whole point: opened inside the window, settles outside,
    and the live ledger is already carrying it."""
    day = datetime(2026, 7, 29, 16, tzinfo=timezone.utc)
    assert _toks(_open_settled_events(conn, day, END)) == ["26JUL31"]
    # unbounded (the old behaviour) would have taken the August event too
    later = datetime(2026, 8, 10, tzinfo=timezone.utc)
    assert _toks(_open_settled_events(conn, day, later)) == ["26AUG07", "26JUL31"]


def test_an_event_already_closed_at_the_simulated_day_is_not_a_candidate(conn):
    """asof < close_ts is strict — you cannot enter a market that has closed."""
    day = datetime(2026, 7, 28, 16, tzinfo=timezone.utc)
    assert "26JUL24" not in _toks(_open_settled_events(conn, day, END))
    day_early = datetime(2026, 7, 20, 16, tzinfo=timezone.utc)
    assert "26JUL24" in _toks(_open_settled_events(conn, day_early, END))


def test_the_edge_event_is_included_not_dropped(conn):
    """`end` is inclusive: an event settling 2026-07-31T20:00 belongs to a window
    that ends 2026-07-31. Off-by-one here silently shrinks the track record."""
    day = datetime(2026, 7, 30, 16, tzinfo=timezone.utc)
    assert _toks(_open_settled_events(conn, day, END)) == ["26JUL31"]


def test_window_fields_describe_the_simulated_span_not_the_wall_clock(conn):
    """window_start/window_end are what a reader uses to check the segment lines up
    with TRACK_CUTOVER, so they must follow `end`, never datetime.now()."""
    from prediction_market_macro.research import walkforward as wf
    out = wf.run(conn, days=3, end=END)
    assert out["window_end"] == "2026-07-31"
    assert out["window_start"] == "2026-07-28"
    assert out["bounded"] is True
    # and an unbounded run still reports today, i.e. the default did not change
    out2 = wf.run(conn, days=3)
    assert out2["window_end"] == datetime.now(timezone.utc).date().isoformat()
    assert out2["bounded"] is False


def test_experiments_window_column_keeps_the_days_prefix(conn):
    """frontend_export filters the daily headline rows with window LIKE '30d%'.
    Putting the date range in that column instead would blank the dashboard while
    every number underneath stayed correct — a silent break."""
    from prediction_market_macro.research import walkforward as wf
    wf.run(conn, days=30, end=END)
    row = conn.execute("SELECT window FROM experiments WHERE name='daily_walkforward'"
                       " ORDER BY created_ts DESC LIMIT 1").fetchone()
    assert row["window"].startswith("30d")
