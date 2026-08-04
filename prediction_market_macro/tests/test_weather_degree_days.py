"""Degree days (§28) — the arithmetic, the aggregation, and the PIT stamp. Offline.

The headline result this source was accepted on (measured 2026-08-04, 864 weeks of EIA
gas storage 2010→2026):

    model                      LOO RMSE of weekly storage change (Bcf)
    mean only                   98.9
    week-of-year dummies        42.5
    HDD + CDD                   23.9
    week-of-year + HDD/CDD      22.8

so weather is not seasonality relabelled — it halves the error that a pure calendar
model leaves behind. What the tests below pin is the machinery that result depends on,
because the two ways to get it silently wrong are both invisible in a summary statistic:
averaging temperatures before applying the 65°F kink, and stamping the rows early.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from prediction_market_macro.ingest import weather
from prediction_market_macro.ingest.store import init_db


@pytest.fixture()
def conn(tmp_path):
    c = init_db(tmp_path / "t.db")
    weather.ensure_schema(c)
    return c


def _put(conn, region, day, tmean):
    hdd, cdd = weather._degree_days(tmean)
    conn.execute(
        "INSERT OR REPLACE INTO weather_daily(region, event_time, tmean_f, hdd, cdd,"
        " knowledge_time, first_seen_ts) VALUES(?,?,?,?,?,?,?)",
        (region, day, tmean, hdd, cdd, weather._knowledge_time(day), "t0"))
    conn.commit()


# ── the kink ────────────────────────────────────────────────────────────────
def test_degree_days_are_one_sided_around_65f():
    assert weather._degree_days(65.0) == (0.0, 0.0)
    assert weather._degree_days(45.0) == (20.0, 0.0)
    assert weather._degree_days(85.0) == (0.0, 20.0)


def test_national_index_weights_degree_days_not_temperatures(monkeypatch, conn):
    """A 25°F Minneapolis and a 105°F Phoenix on the same day is a day of heavy heating
    AND heavy cooling demand. Average the temperatures first and it reports 65°F — a
    perfectly mild day with no demand at all, which is the opposite of the truth."""
    monkeypatch.setattr(weather, "_CITY_DELAY_S", 0.0)     # no quota is used here
    monkeypatch.setattr(weather, "CITIES",
                        {"MSP": (44.98, -93.27, 1.0), "PHX": (33.45, -112.07, 1.0)})
    monkeypatch.setattr(weather, "_fetch_city",
                        lambda lat, lon, *a, **k: {"2026-01-15": 25.0 if lat > 40 else 105.0})
    weather.pull(conn, start="2026-01-15", end="2026-01-15")
    r = conn.execute("SELECT tmean_f, hdd, cdd FROM weather_daily WHERE region='US'").fetchone()
    assert r["tmean_f"] == pytest.approx(65.0)          # the trap: looks like nothing
    assert r["hdd"] == pytest.approx(20.0)              # ...but 40 HDD and 40 CDD across
    assert r["cdd"] == pytest.approx(20.0)              #    two cities, halved by weight


def test_national_index_skips_days_a_city_is_missing(monkeypatch, conn):
    """A partial day would be a constant-weight index computed on a subset — i.e. a
    warm day whenever the coldest city happens to be absent."""
    monkeypatch.setattr(weather, "_CITY_DELAY_S", 0.0)     # no quota is used here
    monkeypatch.setattr(weather, "CITIES",
                        {"MSP": (44.98, -93.27, 1.0), "PHX": (33.45, -112.07, 1.0)})

    def fake(lat, lon, *a, **k):
        return ({"2026-01-15": 25.0, "2026-01-16": 30.0} if lat > 40
                else {"2026-01-15": 105.0})
    monkeypatch.setattr(weather, "_fetch_city", fake)
    weather.pull(conn, start="2026-01-15", end="2026-01-16")
    days = [r[0] for r in conn.execute(
        "SELECT event_time FROM weather_daily WHERE region='US' ORDER BY event_time")]
    assert days == ["2026-01-15"]
    # the city row itself is still stored — only the aggregate is withheld
    assert conn.execute("SELECT COUNT(*) FROM weather_daily WHERE region='MSP'"
                        ).fetchone()[0] == 2


# ── the stamp ───────────────────────────────────────────────────────────────
def test_knowledge_time_is_the_morning_after():
    assert weather._knowledge_time("2026-01-15") == "2026-01-16T12:00:00+00:00"


def test_weather_leads_the_storage_print_it_has_to_explain():
    """The whole trade: EIA publishes the week-ending-Friday storage number the
    following Thursday 14:30Z, and the last day of that week is stamped Saturday 12:00Z
    — five days earlier. If this ordering ever inverts, the NG feature stops being a
    forecast and becomes a leak."""
    last_day_of_week = weather._knowledge_time("2026-07-24")
    eia_print = "2026-07-30T14:30:00+00:00"
    assert last_day_of_week < eia_print


# ── the PIT accessor ────────────────────────────────────────────────────────
def test_weekly_us_cannot_see_past_asof(conn):
    for day, t in [("2026-01-05", 30.0), ("2026-01-06", 30.0), ("2026-01-07", 30.0),
                   ("2026-01-08", 30.0), ("2026-01-09", 30.0), ("2026-01-10", 30.0),
                   ("2026-01-11", 30.0)]:
        _put(conn, "US", day, t)
    # week ending Fri 2026-01-09 is Sat 03 .. Fri 09; we seeded Mon 05 .. Sun 11, so
    # only the Sat-10/Sun-11 bucket-mates are short. Ask before the last day is known:
    mid = datetime(2026, 1, 8, 12, 0, tzinfo=timezone.utc)
    assert weather.weekly_us(conn, mid) == []          # no complete 7-day week yet


def test_weekly_us_returns_only_complete_weeks(conn):
    # Sat 2026-01-03 .. Fri 2026-01-09 = one full week ending Friday
    for i, day in enumerate(["2026-01-03", "2026-01-04", "2026-01-05", "2026-01-06",
                             "2026-01-07", "2026-01-08", "2026-01-09"]):
        _put(conn, "US", day, 45.0)
    _put(conn, "US", "2026-01-10", 45.0)               # starts the next week, incomplete
    got = weather.weekly_us(conn, datetime(2026, 2, 1, tzinfo=timezone.utc))
    assert [g[0] for g in got] == ["2026-01-09"]
    assert got[0][1] == pytest.approx(7 * 20.0)        # 7 days x 20 HDD
    assert got[0][2] == pytest.approx(0.0)
