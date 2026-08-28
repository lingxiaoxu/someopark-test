"""The per-minute loop must never shrink the board it publishes.

`live_refresh` rebuilds a NEAR-TERM slice of upcoming.json every cycle. Writing that
slice as the whole file replaced the daily calendar with it, so a quiet evening — no
kickoff inside the 12-hour horizon — published an EMPTY upcoming.json and the two most
prominent cards (Today's Predictions, Match Pricing) went blank until the next daily
run. Observed in production: 128 matches on the backend, 0 in the served copy.
"""
from __future__ import annotations

from prediction_market_soccer.ops.live_refresh import _merge_upcoming


def _board():
    return [
        {"fixture_id": 1, "status": "NS", "src": "daily"},
        {"fixture_id": 2, "status": "NS", "src": "daily"},
        {"fixture_id": 3, "status": "1H", "src": "daily"},   # kicked off since
        {"fixture_id": 4, "status": "FT", "src": "daily"},   # finished since
        {"fixture_id": 5, "status": "NS", "src": "daily"},
    ]


def test_quiet_window_keeps_the_board():
    """The regression itself: nothing to re-price must not mean nothing to show."""
    merged = _merge_upcoming(_board(), [])
    assert [m["fixture_id"] for m in merged] == [1, 2, 5]


def test_fresh_rows_replace_their_counterparts():
    merged = _merge_upcoming(_board(), [{"fixture_id": 2, "status": "NS", "src": "live"}])
    row2 = [m for m in merged if m["fixture_id"] == 2]
    assert len(row2) == 1 and row2[0]["src"] == "live"


def test_near_term_slice_sorts_first():
    """The actionable half stays at the top of the card."""
    fresh = [{"fixture_id": 9, "status": "NS"}, {"fixture_id": 2, "status": "NS"}]
    assert [m["fixture_id"] for m in _merge_upcoming(_board(), fresh)][:2] == [9, 2]


def test_started_and_finished_matches_leave_the_board():
    """They belong to the live feed / recent_finished; a stale pre-match row for a match
    already in play is worse than omitting it."""
    ids = [m["fixture_id"] for m in _merge_upcoming(_board(), [])]
    assert 3 not in ids and 4 not in ids


def test_kickoff_is_read_from_the_database_not_the_archived_row():
    """The archived rows cannot answer "have you kicked off since?" — upcoming_export only
    ever emits not-started fixtures, so every stored row says NS forever (measured 128 of
    128 in production). Asking the row was a test whose answer was fixed before it was
    asked; the live status has to come from the fixture table."""
    import sqlite3
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE fixture (api_id INT, status_short TEXT)")
    # Every carried row still SAYS 'NS'; the database knows 2 and 5 have moved on.
    board = [{"fixture_id": i, "status": "NS"} for i in (1, 2, 5)]
    c.executemany("INSERT INTO fixture VALUES (?,?)",
                  [(1, "NS"), (2, "1H"), (5, "FT")])
    ids = [m["fixture_id"] for m in _merge_upcoming(board, [], c)]
    assert ids == [1], "a match already in play must leave the pre-match board"


def test_a_fresh_row_survives_even_with_no_prior_board():
    assert [m["fixture_id"] for m in _merge_upcoming([], [{"fixture_id": 7, "status": "NS"}])] == [7]


def test_rows_without_an_id_are_dropped_not_duplicated():
    merged = _merge_upcoming([{"status": "NS"}, {"fixture_id": 1, "status": "NS"}], [])
    assert [m["fixture_id"] for m in merged] == [1]
