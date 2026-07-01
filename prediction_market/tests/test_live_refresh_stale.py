"""Tests for the stale-minute retry guard in live_refresh (a transient blip must not freeze the
on-screen minute for a whole cycle)."""
import sqlite3
from datetime import datetime, timedelta, timezone

from prediction_market.ops.live_refresh import (
    _expected_elapsed_min, _stale_live_fixtures, _STALE_TOL_MIN,
)


def _iso(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def test_expected_elapsed_first_and_second_half():
    now = datetime.now(timezone.utc)
    ko = now - timedelta(minutes=30)
    # first half → elapsed ≈ wall clock since kickoff.
    assert abs(_expected_elapsed_min(ko.isoformat(), "1H", now) - 30) < 0.1
    # second half → subtract the ~15' halftime break.
    ko2 = now - timedelta(minutes=90)
    assert abs(_expected_elapsed_min(ko2.isoformat(), "2H", now) - 75) < 0.1


def test_expected_elapsed_uncheckable_cases():
    now = datetime.now(timezone.utc)
    assert _expected_elapsed_min(now.isoformat(), "HT", now) is None          # halftime frozen
    assert _expected_elapsed_min(None, "1H", now) is None                     # no kickoff
    assert _expected_elapsed_min("not-a-date", "1H", now) is None             # unparseable
    assert _expected_elapsed_min((now + timedelta(minutes=5)).isoformat(), "1H", now) is None  # future


def _conn_with(rows):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE fixture (api_id INT, kickoff_ts TEXT, status_short TEXT, elapsed INT)")
    c.executemany("INSERT INTO fixture VALUES (?,?,?,?)", rows)
    return c


def test_stale_detection_flags_only_lagging_fixtures():
    # 2H, kicked off 90' ago → expected ~75'. A fetched 66' lags by 9' (> tol) → stale;
    # a fetched 74' is within tolerance → fresh. An HT fixture is never checked.
    c = _conn_with([
        (1, _iso(90), "2H", 66),   # stale (9' behind)
        (2, _iso(90), "2H", 74),   # fresh (1' behind)
        (3, _iso(50), "HT", 45),   # halftime — skipped
    ])
    stale = _stale_live_fixtures(c)
    ids = {s[0] for s in stale}
    assert ids == {1}
    assert _STALE_TOL_MIN == 6.0


def test_no_false_positive_when_data_is_current():
    c = _conn_with([(1, _iso(30), "1H", 30), (2, _iso(90), "2H", 76)])
    assert _stale_live_fixtures(c) == []
