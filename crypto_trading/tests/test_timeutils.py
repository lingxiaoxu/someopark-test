"""Funding-clock tests (04:00/12:00/20:00 UTC, probe-verified) — no network."""
from datetime import datetime, timezone

import pytest

from crypto_trading.crypto_common.timeutils import (next_funding_time, prev_funding_time,
                                                    to_utc, utc_day)

UTC = timezone.utc


@pytest.mark.parametrize("now,expect_hour,expect_day", [
    (datetime(2026, 7, 7, 3, 59, tzinfo=UTC), 4, 7),
    (datetime(2026, 7, 7, 4, 0, tzinfo=UTC), 12, 7),    # boundary: strictly after
    (datetime(2026, 7, 7, 11, 30, tzinfo=UTC), 12, 7),
    (datetime(2026, 7, 7, 20, 0, tzinfo=UTC), 4, 8),    # wraps to next day
    (datetime(2026, 7, 7, 23, 59, tzinfo=UTC), 4, 8),
])
def test_next_funding(now, expect_hour, expect_day):
    nxt = next_funding_time(now)
    assert (nxt.hour, nxt.day) == (expect_hour, expect_day)
    assert nxt > now


def test_prev_is_next_minus_8h():
    now = datetime(2026, 7, 7, 13, 0, tzinfo=UTC)
    assert prev_funding_time(now) == datetime(2026, 7, 7, 12, 0, tzinfo=UTC)


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        next_funding_time(datetime(2026, 7, 7, 3, 0))


def test_to_utc_ms_and_seconds():
    assert to_utc(1783390080).year == 2026
    assert to_utc(1783390080000, ms=True) == to_utc(1783390080)


def test_utc_day_format():
    assert utc_day(datetime(2026, 7, 7, 23, 59, tzinfo=UTC)) == "2026-07-07"
