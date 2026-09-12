"""固定分钟参考价:日历时点、完整覆盖和取数失败行为(全部隔离网络)。"""
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from reconcile import official_close as oc


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "test-secret-never-log")
    def forbidden(*a, **kw):
        raise AssertionError("unexpected network request")
    monkeypatch.setattr(oc.requests, "get", forbidden)


def response(monkeypatch, change=None):
    def get(url, *, params, timeout):
        pieces = url.split("/")
        ticker, epoch = pieces[-6], int(pieces[-2])
        assert params["adjusted"] == "false" and params["limit"] == 2
        doc = {"ticker": ticker, "results": [{"t": epoch, "c": 123.45}]}
        if change:
            change(doc)
        return SimpleNamespace(status_code=200, json=lambda: doc)
    monkeypatch.setattr(oc.requests, "get", get)


@pytest.mark.parametrize("session,expected", [
    ("2026-09-08", "2026-09-08T15:58:00-04:00"),
    ("2026-11-27", "2026-11-27T12:58:00-05:00"),
    ("2026-01-06", "2026-01-06T15:58:00-05:00"),
])
def test_fixed_exchange_close_reference_handles_half_days_and_dst(monkeypatch, session, expected):
    response(monkeypatch)
    got = oc.qc_reference_prices(session, ["AMD", "TXN", "AMD"])
    assert got["bar_start_et"] == expected
    assert got["prices"] == {"AMD": 123.45, "TXN": 123.45}
    assert datetime.fromisoformat(got["bar_start_utc"]) == datetime.fromisoformat(expected)


@pytest.mark.parametrize("change", [
    lambda d: d.update(ticker="WRONG"),
    lambda d: d.update(results=[]),
    lambda d: d["results"].append(dict(d["results"][0])),
    lambda d: d["results"][0].update(t=d["results"][0]["t"] - 60000),
    lambda d: d["results"][0].update(c=float("nan")),
    lambda d: d["results"][0].update(c=0),
])
def test_missing_exact_bar_or_wrong_identity_refuses_without_carry(monkeypatch, change):
    response(monkeypatch, change)
    with pytest.raises(oc.SourceError, match="精确"):
        oc.qc_reference_prices("2026-09-08", ["AMD"])


def test_error_does_not_include_api_key(monkeypatch):
    def fail(*a, **kw):
        raise requests.ConnectionError("https://api.example/?apiKey=test-secret-never-log")
    monkeypatch.setattr(oc.requests, "get", fail)
    with pytest.raises(oc.SourceError) as e:
        oc.qc_reference_prices("2026-09-08", ["AMD"])
    assert "ConnectionError" in str(e.value) and "test-secret" not in str(e.value)


def test_holiday_cannot_be_used_as_reference():
    with pytest.raises(oc.SourceError, match="不是 NYSE"):
        oc.qc_reference_prices("2026-09-07", ["AMD"])
