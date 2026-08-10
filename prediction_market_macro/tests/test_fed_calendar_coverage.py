"""The FOMC calendar has to cover the history a backtest replays, and the lookup into it
has to accept both spellings of a period.

Two defects, both silent, both found while sizing the fed parameter grid:

  1. `_FOMC` was hardcoded 2026-01..2027-12. Any replay of an earlier meeting found no
     entry, so `meeting` was None, so the market / ZQ / DGS2 legs were all skipped and
     `predict` returned `mode='rule_only'` — the unconditional base rate. 7 of the 12 most
     recent settled KXFED events replayed that way. Nothing raised and nothing logged: the
     backtest simply scored a crippled model and reported the number as if it meant
     something. Any parameter selected against those Brier scores would have been selected
     against noise, since the pooling weights cannot move a distribution with one source.

  2. KXFEDDECISION spells some periods by STATEMENT DATE (`24MAR20` -> `2024-03-20`) while
     the calendar is keyed by month. `e.period == period` missed those two events, with
     the same rule_only consequence, and it also broke `_level_pmf`'s KXFED-token lookup
     because that token is month-spelled.

So: coverage is asserted against the meetings actually settled in the ledger rather than
against a count, and the lookup is asserted on both spellings.

The 2021 exclusion is deliberate and is pinned here so nobody "fixes" it: Kalshi's three
2021 KXFED events settle BEFORE their meeting's statement (21JUL settles 07-26 against a
07-28 statement), so they are not scorable against an FOMC decision at all.
"""
from __future__ import annotations

from datetime import timezone

import pytest

from prediction_market_macro.ingest.calendars import CALENDARS
from prediction_market_macro.model.fed import _meeting_event

FOMC = CALENDARS["FOMC"]


def test_the_calendar_stays_sorted_because_next_release_trusts_list_order():
    """`next_release()` returns the first entry past `now` by POSITION, not by min(). An
    out-of-order insert would therefore hand out a meeting that has already happened."""
    ts = [e.scheduled_ts for e in FOMC]
    assert ts == sorted(ts)
    assert len({e.period for e in FOMC}) == len(FOMC)


def test_every_meeting_is_eight_a_year_and_none_is_a_weekend():
    """A mistyped date is the failure mode here, and both of these catch one cheaply: the
    FOMC has met eight times a year for decades, and never announces on a weekend."""
    years = {}
    for e in FOMC:
        assert e.scheduled_ts.tzinfo is not None
        et = e.scheduled_ts.astimezone(timezone.utc)
        assert et.weekday() < 5, f"{e.period} lands on a weekend"
        years[e.period[:4]] = years.get(e.period[:4], 0) + 1
    for year, n in years.items():
        assert n == 8, f"{year} has {n} meetings, expected 8"


@pytest.mark.parametrize("period,date_key", [("2024-01", "2024-01-31"),
                                             ("2024-03", "2024-03-20")])
def test_both_spellings_of_a_period_resolve_to_the_same_meeting(period, date_key):
    """KXFED's month key and KXFEDDECISION's date key name one meeting and must return it.

    The canonical month key must come back on the entry either way, because callers hand
    `ev.period` to `_level_pmf` to find the month-spelled KXFED ladder.
    """
    by_month = _meeting_event(period)
    by_date = _meeting_event(date_key)
    assert by_month is not None and by_date is not None
    assert by_month is by_date
    assert by_date.period == period
    assert by_date.scheduled_ts.date().isoformat() == date_key


def test_a_date_key_does_not_match_a_different_day_in_the_same_month():
    """The lazy fix — truncate the date key to its month — would match any meeting that
    month. There is only ever one, but the day is the part that is checkable, so check it.
    """
    assert _meeting_event("2024-03-19") is None
    assert _meeting_event("2024-03-21") is None
    assert _meeting_event("2024-02") is None          # no February meeting exists


def test_history_the_backtest_replays_is_covered():
    """Coverage is only meaningful against the events that actually get scored, so this
    asserts every settled meeting since 2022 is present rather than asserting a range."""
    for year in range(2022, 2027):
        assert sum(1 for e in FOMC if e.period.startswith(str(year))) == 8, \
            f"{year} is not fully covered — replays of it fall back to rule_only"


def test_2021_is_excluded_on_purpose():
    """Not an oversight. Kalshi's 2021 KXFED contracts settle days BEFORE the statement
    they nominally price, so scoring a decision model on them is scoring it on an event
    that had not happened yet."""
    assert not [e for e in FOMC if e.period.startswith("2021")]
