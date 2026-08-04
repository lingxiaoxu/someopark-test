"""FeatureStore.fed_target_upper / fomc_meeting_moves (§29.1, §29.2) — offline.

These two doors were opened so the Fed model could see policy history before 2008 and
could count meetings that HELD. Both are easy to get subtly wrong in ways no aggregate
statistic reveals, so each trap gets a test:

  1. the splice must cut on the date, not trust DFEDTAR to stop where FRED says it does;
  2. the meeting calendar must come from statements — a rate series records only
     CHANGES, so from rates alone "held" and "no meeting" are the same picture;
  3. a meeting whose outcome has not printed yet must DROP OUT, not resolve to a hold
     (that would manufacture an H0 observation out of missing data);
  4. an empty leg must return empty, not raise — every fixture db and every fresh
     install has no DFEDTAR rows, and pandas compares an empty object Index to a
     Timestamp by raising.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from prediction_market_macro.ingest import fed_text
from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.model.features import FeatureStore, move_cat

ASOF = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def conn(tmp_path):
    return init_db(tmp_path / "t.db")


def _rate(conn, sid, day: str, value: float, kt: str | None = None):
    """One daily target observation. Default knowledge_time is 20:00Z the same day."""
    kt = kt or f"{day}T20:00:00+00:00"
    conn.execute("INSERT OR IGNORE INTO fred_obs VALUES(?,?,?,?,?,?)",
                 (sid, day, value, day, kt, kt))


def _stmt(conn, day: str):
    kt, src = fed_text._release_ts(day, "For release at 2:00 p.m. EDT ... Committee")
    conn.execute(
        "INSERT OR REPLACE INTO fed_statements(period, release_date, url, text,"
        " knowledge_time, time_source, fetched_ts) VALUES(?,?,?,?,?,?,?)",
        (day[:7], day, f"http://x/{day}", "body", kt, src, "2026-08-04"))
    return kt


# ── the splice ──────────────────────────────────────────────────────────────
def test_splice_joins_the_two_regimes_with_no_gap_and_no_overlap(conn):
    for d, v in (("2008-12-14", 1.0), ("2008-12-15", 1.0)):
        _rate(conn, "DFEDTAR", d, v)
    for d, v in (("2008-12-16", 0.25), ("2008-12-17", 0.25)):
        _rate(conn, "DFEDTARU", d, v)
    conn.commit()
    s, _ = FeatureStore(conn).fed_target_upper(ASOF)
    assert list(s.index.strftime("%Y-%m-%d")) == [
        "2008-12-14", "2008-12-15", "2008-12-16", "2008-12-17"]
    assert [float(x) for x in s] == [1.0, 1.0, 0.25, 0.25]


def test_splice_cuts_on_the_date_rather_than_trusting_the_source_to_stop(conn):
    """FRED's DFEDTAR is discontinued at 2008-12-15, but the cut is ours to enforce:
    if a stray later row ever appears it must not shadow the range's upper bound."""
    _rate(conn, "DFEDTAR", "2008-12-15", 1.0)
    _rate(conn, "DFEDTAR", "2009-01-05", 1.0)          # must be dropped
    _rate(conn, "DFEDTARU", "2009-01-05", 0.25)
    conn.commit()
    s, _ = FeatureStore(conn).fed_target_upper(ASOF)
    assert float(s.loc["2009-01-05"]) == 0.25


def test_splice_is_pit_on_both_legs(conn):
    _rate(conn, "DFEDTAR", "2008-12-15", 1.0)
    _rate(conn, "DFEDTARU", "2026-08-01", 3.75, kt="2026-08-01T20:00:00+00:00")
    conn.commit()
    s, h = FeatureStore(conn).fed_target_upper(
        datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc))       # before that 20:00Z stamp
    assert list(s.index.strftime("%Y-%m-%d")) == ["2008-12-15"]
    assert h == "2008-12-15T20:00:00+00:00"


def test_empty_pre2008_leg_returns_empty_rather_than_raising(conn):
    """The regression that broke 7 tests: `Series([], dtype=float).index` is an empty
    OBJECT index, and `idx < Timestamp` raises instead of yielding an empty mask."""
    _rate(conn, "DFEDTARU", "2026-08-01", 3.75)
    conn.commit()
    s, _ = FeatureStore(conn).fed_target_upper(ASOF)
    assert [float(x) for x in s] == [3.75]
    assert FeatureStore(init_db(":memory:")).fed_target_upper(ASOF)[0].empty


# ── the meeting panel ───────────────────────────────────────────────────────
def _seed_meetings(conn):
    """Daily target from 2026-01 to 2026-08 with ONE hike, plus three statements.

    2026-03-18 hikes to 4.00; 2026-06-17 and 2026-07-29 hold.
    """
    day = datetime(2026, 1, 2)
    while day <= datetime(2026, 8, 1):
        v = 3.75 if day < datetime(2026, 3, 19) else 4.00
        _rate(conn, "DFEDTARU", day.date().isoformat(), v)
        day += timedelta(days=1)
    for d in ("2026-03-18", "2026-06-17", "2026-07-29"):
        _stmt(conn, d)
    conn.commit()


def test_holds_are_counted_which_is_the_whole_point_of_the_statement_calendar(conn):
    """From the rate series alone only ONE of these three meetings is visible."""
    _seed_meetings(conn)
    got, _ = FeatureStore(conn).fomc_meeting_moves(ASOF)
    assert [(g["date"].strftime("%Y-%m-%d"), g["move"], g["cat"]) for g in got] == [
        ("2026-03-18", 0.25, "H25"),
        ("2026-06-17", 0.0, "H0"),
        ("2026-07-29", 0.0, "H0")]


def test_panel_is_pit_on_the_statement_leg(conn):
    _seed_meetings(conn)
    got, _ = FeatureStore(conn).fomc_meeting_moves(
        datetime(2026, 7, 1, tzinfo=timezone.utc))
    assert [g["date"].strftime("%Y-%m-%d") for g in got] == ["2026-03-18", "2026-06-17"]


def test_meeting_whose_outcome_has_not_printed_drops_out_instead_of_becoming_a_hold(conn):
    """A statement is out at 18:00Z on decision day; the next target observation is
    stamped 20:00Z the following day. In between, the meeting must not exist in the
    panel — resolving it to 0.0 would invent a hold that we cannot yet see."""
    _seed_meetings(conn)
    fs = FeatureStore(conn)
    just_after = datetime(2026, 7, 29, 19, 0, tzinfo=timezone.utc)
    assert len(fs.statements_asof(just_after, limit=99)) == 3      # statement IS visible
    got, _ = fs.fomc_meeting_moves(just_after)
    assert [g["date"].strftime("%Y-%m-%d") for g in got] == ["2026-03-18", "2026-06-17"]


def test_panel_is_empty_not_broken_when_a_leg_is_missing(conn):
    _stmt(conn, "2026-07-29")
    conn.commit()
    assert FeatureStore(conn).fomc_meeting_moves(ASOF) == ([], None)


# ── the category cuts ───────────────────────────────────────────────────────
def test_a_50bp_cut_cannot_land_in_the_25bp_bucket():
    assert move_cat(-0.50) == "C26"
    assert move_cat(-0.25) == "C25"
    assert move_cat(0.0) == "H0"
    assert move_cat(0.25) == "H25"
    assert move_cat(0.50) == "H26"


def test_category_cuts_are_symmetric_about_zero():
    """The cut side was written second and is the one that can silently drift."""
    assert [move_cat(-m) for m in (0.05, 0.20, 0.30, 0.60)] == ["H0", "C25", "C25", "C26"]
    assert [move_cat(m) for m in (0.05, 0.20, 0.30, 0.60)] == ["H0", "H25", "H25", "H26"]
    assert (move_cat(-0.124), move_cat(0.124)) == ("H0", "H0")
    assert (move_cat(-0.126), move_cat(0.126)) == ("C25", "H25")
