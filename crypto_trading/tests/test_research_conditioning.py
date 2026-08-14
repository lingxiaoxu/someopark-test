"""Conditioning-bucket helpers — pure-function tests, no network."""
import pandas as pd

from crypto_trading.crypto_strategies.research_conditioning import (bucket_table,
                                                                    is_weekend,
                                                                    near_funding,
                                                                    okx_bucket,
                                                                    okx_count,
                                                                    overlap_fraction,
                                                                    session_bucket,
                                                                    vol_bucket_at)


def ts(s):
    return pd.Timestamp(s, tz="UTC")


def test_session_buckets():
    assert session_bucket(ts("2026-07-20 03:00")) == "asia_00_08"
    assert session_bucket(ts("2026-07-20 08:00")) == "eu_08_16"
    assert session_bucket(ts("2026-07-20 15:59")) == "eu_08_16"
    assert session_bucket(ts("2026-07-20 23:00")) == "us_16_24"


def test_near_funding_windows():
    assert near_funding(ts("2026-07-20 03:30"))          # 30min before 04:00
    assert near_funding(ts("2026-07-20 12:59"))          # 59min after 12:00
    assert not near_funding(ts("2026-07-20 09:30"))      # mid-window
    # 23:30 is 3.5h after 20:00 and 4.5h before next-day 04:00 → NOT near
    assert not near_funding(ts("2026-07-20 23:30"))
    # cross-day: 03:30 uses same-day 04:00 anchor; 20:50 within +1h of 20:00
    assert near_funding(ts("2026-07-20 20:50"))


def test_weekend():
    assert is_weekend(ts("2026-07-25 12:00"))            # Saturday
    assert not is_weekend(ts("2026-07-24 12:00"))        # Friday


def test_okx_count_and_bucket():
    liq = pd.DatetimeIndex([ts("2026-07-20 10:00"), ts("2026-07-20 10:01"),
                            ts("2026-07-20 10:30")])
    # PIT: past window only — at 10:00:30 the 10:01 print is FUTURE, not counted
    assert okx_count(ts("2026-07-20 10:00:30"), liq) == 1
    assert okx_count(ts("2026-07-20 10:02"), liq) == 2         # both now in the past 2min
    assert okx_count(ts("2026-07-20 09:59"), liq) == 0         # nothing printed yet
    assert okx_count(ts("2026-07-20 11:00"), liq) == 0
    assert okx_bucket(0) == "0" and okx_bucket(1) == "1" and okx_bucket(5) == "2+"


def test_vol_bucket_lookup():
    idx = pd.date_range("2026-07-20", periods=10, freq="1min", tz="UTC")
    regime = pd.Series([False] * 5 + [True] * 5, index=idx)
    assert vol_bucket_at(idx[2] + pd.Timedelta(seconds=30), regime) == "low_vol"
    assert vol_bucket_at(idx[7], regime) == "high_vol"
    assert vol_bucket_at(idx[0] - pd.Timedelta(hours=1), regime) == "unknown"


def test_bucket_table_and_overlap():
    df = pd.DataFrame({"net": [1.0, -1.0, 2.0, 3.0],
                       "session": ["a", "a", "b", "b"]})
    t = bucket_table(df, "session")
    assert t.loc["a", "n"] == 2 and t.loc["a", "hit"] == 0.5
    assert t.loc["b", "mean_net"] == 2.5
    a = pd.Series([ts("2026-07-20 10:00"), ts("2026-07-20 12:00")])
    b = pd.Series([ts("2026-07-20 10:10")])
    assert overlap_fraction(a, b, window_min=30) == 0.5
    assert overlap_fraction(b, a, window_min=30) == 1.0
    assert overlap_fraction(a, pd.Series(dtype=object)) == 0.0
