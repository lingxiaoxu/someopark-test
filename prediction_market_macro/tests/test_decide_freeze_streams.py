"""The release freeze must guard arb and argmax as well as ordinary decisions."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from prediction_market_macro.config.registry import REGISTRY
from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.ops import decide_all as da
from prediction_market_macro.ops import trading_kalshi
from prediction_market_macro.strategy import arb
from prediction_market_macro.strategy.decision import Decision, GATES

NOW = datetime(2026, 9, 11, 12, 25, tzinfo=timezone.utc)
SPEC = REGISTRY["KXCPI"]


@pytest.fixture()
def book(tmp_path, monkeypatch):
    conn = init_db(tmp_path / "freeze.db")

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(da, "datetime", Clock)
    monkeypatch.setattr(da, "REGISTRY", {SPEC.ticker: SPEC})
    monkeypatch.setattr(da, "_warn_unevaluated_series_gate", lambda _: None)
    monkeypatch.setattr(trading_kalshi, "on_fill", lambda *args: None)
    for strike, bid, ask in ((0.1, 0.60, 0.62), (0.2, 0.70, 0.74)):
        ticker = f"KXCPI-26AUG-T{strike}"
        conn.execute(
            "INSERT INTO contracts(ticker,series,event_ticker,period,strike_type,"
            "floor_strike,close_time,status,first_seen_ts) VALUES(?,?,?,?,?,?,?,?,?)",
            (ticker, SPEC.ticker, "KXCPI-26AUG", "26AUG", "greater", strike,
             (NOW + timedelta(days=1)).isoformat(), "active", NOW.isoformat()))
        conn.execute("INSERT INTO quotes VALUES(?,?,?,?,?,?)",
                     (NOW.isoformat(), ticker, bid, ask, 200.0, 200.0))
    conn.commit()
    return conn


def release(conn, seconds_before):
    conn.execute("INSERT INTO releases(cal,period,scheduled_ts) VALUES(?,?,?)",
                 (SPEC.calendar, "2026-08", (NOW + timedelta(seconds=seconds_before)).isoformat()))
    conn.commit()


@pytest.mark.parametrize("seconds_before, frozen", [(601, False), (600, True),
                                                     (300, True), (0, True), (-1, False)])
def test_real_arb_obeys_freeze_without_any_prediction(book, seconds_before, frozen):
    release(book, seconds_before)
    # The fixture's monotone violation is executable, so outside-window success
    # proves the frozen case was blocked by the clock, not missing model data.
    n = da.run(book, SimpleNamespace())
    count = book.execute("SELECT COUNT(*) FROM decisions WHERE kind='arb'").fetchone()[0]
    assert count == (0 if frozen else 1)
    assert n == count
    if frozen:
        assert book.execute("SELECT state FROM coverage").fetchone()[0] == "frozen"


@pytest.mark.parametrize("seconds_before, expected_calls", [(300, 0), (601, 1)])
def test_argmax_fallback_cannot_follow_a_frozen_pass(book, monkeypatch, seconds_before,
                                                  expected_calls):
    release(book, seconds_before)
    book.execute(
        "INSERT INTO preds(series,period,asof,model_version,dist_json,ladder_json,"
        "data_horizon,created_ts) VALUES(?,?,?,?,?,?,?,?)",
        (SPEC.ticker, "2026-08", NOW.isoformat(), SPEC.model + "/test", "{}",
         '{"0.2": 1.0}', NOW.isoformat(), NOW.isoformat()))
    book.commit()
    calls = []
    monkeypatch.setattr(arb, "execute", lambda *args, **kwargs: 0)
    monkeypatch.setattr(da, "enumerate_structs", lambda *args, **kwargs: [])
    monkeypatch.setattr(da, "decide", lambda *args, **kwargs:
                        Decision("pass", None, 0.0, 0, ("no_edge",), dict(GATES)))
    monkeypatch.setattr(da, "_place_argmax", lambda *args, **kwargs: calls.append("argmax"))
    assert da.run(book, SimpleNamespace(bankroll_usd=1000)) == 1
    assert len(calls) == expected_calls
    row = book.execute("SELECT kind,note FROM decisions").fetchone()
    assert row["kind"] == "pass"
    assert row["note"] == ("freeze_window" if not expected_calls else "no_edge")


def test_clock_is_refreshed_between_books_when_previous_work_crosses_freeze(book, monkeypatch):
    other = REGISTRY["KXCPICORE"]
    release(book, 610)                  # just outside freeze for the first book
    book.execute(
        "INSERT INTO contracts(ticker,series,event_ticker,period,status,first_seen_ts)"
        " VALUES(?,?,?,?,?,?)",
        ("CORE", other.ticker, "CORE-EVENT", "26AUG", "active", NOW.isoformat()))
    book.commit()
    monkeypatch.setattr(da, "REGISTRY", {SPEC.ticker: SPEC, other.ticker: other})
    current = [NOW]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0]

    monkeypatch.setattr(da, "datetime", Clock)
    priced = []

    def slow_first_book(conn, series, period):
        priced.append(series)
        current[0] += timedelta(seconds=20)
        return []

    monkeypatch.setattr(da, "_legs_meta", slow_first_book)
    da.run(book, SimpleNamespace())
    assert priced == [SPEC.ticker]
    assert book.execute("SELECT state FROM coverage WHERE series=?",
                        (other.ticker,)).fetchone()[0] == "frozen"
