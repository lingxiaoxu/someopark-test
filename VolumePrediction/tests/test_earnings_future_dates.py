"""Future earnings calendar accepts both deployed Mongo date representations.

Small synthetic cursors deliberately return out-of-query records as well: the
loader must enforce the requested symbols and inclusive calendar window locally.
No database or network connection is created by these tests.
"""
from datetime import date, datetime, timezone

import pytest
import pymongo

from VolumePrediction.data import earnings_loader as el


TODAY = date(2026, 9, 20)


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return TODAY

    monkeypatch.setattr(el, "date", FixedDate)
    monkeypatch.setenv("MONGO_URI", "mongodb://synthetic.invalid/calendar")


@pytest.fixture
def mongo_stub(monkeypatch):
    """Patch the public constructor used by the function's local import."""
    def install(documents=(), *, fail_at=None):
        secret = "credential-must-not-appear"
        failure = ValueError(f"mongodb://user:{secret}@private.invalid/calendar")
        state = {"opened": 0, "closed": 0, "finds": [], "lookups": []}

        class Collection:
            def find(self, query, projection):
                state["finds"].append((query, projection))
                if fail_at == "find":
                    raise failure

                def cursor():
                    yield from documents
                    if fail_at == "cursor":
                        raise failure

                return cursor()

        class Database:
            def __getitem__(self, name):
                state["lookups"].append(name)
                return Collection()

        class Client:
            def __getitem__(self, name):
                state["lookups"].append(name)
                if fail_at == "database":
                    raise failure
                return Database()

            def close(self):
                state["closed"] += 1

        def constructor(*args, **kwargs):
            state["opened"] += 1
            return Client()

        monkeypatch.setattr(pymongo, "MongoClient", constructor)
        return state, secret

    return install


def test_mixed_storage_types_window_filter_sort_and_deduplicate(mongo_stub):
    state, _ = mongo_stub([
        {"symbol": "AAA", "date": "2026-09-22"},
        {"symbol": "AAA", "date": datetime(2026, 9, 20)},
        {"symbol": "AAA", "date": "2026-09-20"},
        {"symbol": "AAA", "date": "2026-09-21T12:30:00Z"},
        {"symbol": "AAA", "date": "2026-09-21 13:30:00"},
        {"symbol": "BBB", "date": datetime(2026, 9, 22, 23, 59, 59)},
        {"symbol": "BBB", "date": "2026-09-22T23:59:59-12:00"},
        {"symbol": "BBB", "date": datetime(2026, 9, 20, tzinfo=timezone.utc)},
        {"symbol": "AAA", "date": "2026-09-19T23:59:59Z"},
        {"symbol": "AAA", "date": datetime(2026, 9, 19, 23, 59, 59)},
        {"symbol": "AAA", "date": "2026-09-23"},
        {"symbol": "AAA", "date": datetime(2026, 9, 23)},
        {"symbol": "UNKNOWN", "date": "2026-09-20"},
        {"date": "2026-09-20"},
        {"symbol": "AAA"},
        {"symbol": "AAA", "date": None},
        {"symbol": "AAA", "date": 12345},
        {"symbol": "AAA", "date": "bad-date"},
        {"symbol": "AAA", "date": "2026-09-31"},
        {"symbol": "AAA", "date": "2026-09-20junk"},
    ])

    actual = el.future_dates(["BBB", "AAA", "EMPTY"], horizon_days=2)

    assert actual == {
        "AAA": [date(2026, 9, 20), date(2026, 9, 21), date(2026, 9, 22)],
        "BBB": [date(2026, 9, 20), date(2026, 9, 22)],
        "EMPTY": [],
    }
    assert state["opened"] == state["closed"] == 1
    assert state["lookups"] == ["someopark", "fmp_historical_earning_calendar"]
    assert len(state["finds"]) == 1
    query, projection = state["finds"][0]
    assert set(query["symbol"]["$in"]) == {"AAA", "BBB", "EMPTY"}
    assert set(query) == {"symbol", "$or"}
    assert len(query["$or"]) == 2
    date_ranges = [branch["date"] for branch in query["$or"]]
    bson_range = next(r for r in date_ranges if isinstance(r["$gte"], datetime))
    iso_range = next(r for r in date_ranges if isinstance(r["$gte"], str))
    assert bson_range["$gte"] == datetime(2026, 9, 20)
    assert bson_range["$lt"] == datetime(2026, 9, 23)
    assert iso_range["$gte"] == "2026-09-20"
    assert iso_range["$lt"] == "2026-09-23"
    assert projection["symbol"] == projection["date"] == 1
    assert set(projection) <= {"symbol", "date", "_id"}


def test_zero_horizon_includes_whole_today(mongo_stub):
    state, _ = mongo_stub([
        {"symbol": "AAA", "date": datetime(2026, 9, 20, 23, 59, 59)},
        {"symbol": "AAA", "date": "2026-09-20T23:59:59.999Z"},
        {"symbol": "AAA", "date": "2026-09-21"},
    ])

    assert el.future_dates(["AAA"], horizon_days=0) == {"AAA": [TODAY]}
    ranges = state["finds"][0][0]["$or"]
    assert {str(branch["date"]["$lt"]) for branch in ranges} == {
        "2026-09-21", "2026-09-21 00:00:00",
    }
    assert state["closed"] == 1


@pytest.mark.parametrize("fail_at", ["database", "find", "cursor"])
def test_client_closed_and_exception_redacted_on_query_failure(mongo_stub, fail_at):
    state, secret = mongo_stub(
        [{"symbol": "AAA", "date": "2026-09-20"}], fail_at=fail_at
    )

    with pytest.raises(RuntimeError) as caught:
        el.future_dates(["AAA"], horizon_days=2)

    assert "future earnings calendar query failed" in str(caught.value)
    assert "ValueError" in str(caught.value)
    assert secret not in str(caught.value)
    assert "mongodb://" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__
    assert state["opened"] == state["closed"] == 1


def test_empty_symbols_do_not_require_mongo(monkeypatch, mongo_stub):
    state, _ = mongo_stub()
    monkeypatch.delenv("MONGO_URI", raising=False)
    assert el.future_dates([]) == {}
    assert state["opened"] == state["closed"] == 0


def test_negative_horizon_rejected_before_opening_mongo(mongo_stub):
    state, _ = mongo_stub()
    with pytest.raises(ValueError):
        el.future_dates(["AAA"], horizon_days=-1)
    assert state["opened"] == state["closed"] == 0
