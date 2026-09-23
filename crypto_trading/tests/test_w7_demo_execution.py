"""W7 demo execution regressions. All venue I/O is mocked; no orders leave tests."""
from __future__ import annotations

from decimal import Decimal

import pytest
import requests

from crypto_trading.crypto_common import execution_events as execution
from crypto_trading.crypto_common.kalshi import rest_event


SERIES = "KXBTC15M"
TICKER = "KXBTC15M-99SEP150030-30"
CLOSE = "2099-09-15T04:30:00Z"


class Response:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


@pytest.fixture(autouse=True)
def forbid_unmocked_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Unmocked network request")

    monkeypatch.setattr(requests, "get", forbidden)
    monkeypatch.setattr(execution, "_MKT_CACHE", {})


@pytest.fixture
def orders(monkeypatch):
    calls = []

    class Client:
        def __init__(self, *, env):
            assert env == "demo"

        def create_order(self, **kwargs):
            calls.append(kwargs)
            return {"status_code": 201, "response": "{}"}

    monkeypatch.setattr(rest_event, "KalshiEventOrderClient", Client)
    return calls


def market(**overrides):
    return {"ticker": TICKER, "close_time": CLOSE, "status": "active",
            "yes_ask_dollars": "1.0000", "no_ask_dollars": "1.0000",
            **overrides}


def mock_book(monkeypatch, *, yes=(), no=(), status_code=200, payload=None):
    reads = []
    if payload is None:
        payload = {"orderbook_fp": {"yes_dollars": list(yes), "no_dollars": list(no)}}

    def get(url, **kwargs):
        assert url.endswith(f"/markets/{TICKER}/orderbook")
        assert ".demo.kalshi.co/" in url
        reads.append(url)
        return Response(payload, status_code)

    monkeypatch.setattr(requests, "get", get)
    return reads


def mirror(monkeypatch, markets=None, *, side="yes", entry_price=.90, series=(SERIES, "KXBTC")):
    rows = [market()] if markets is None else markets
    monkeypatch.setattr(execution, "_demo_markets_cached", lambda s, **kwargs: rows)
    return execution.EventExecutionRouter(strategy="w7_noisefade").mirror_demo(
        side=side, close_time=CLOSE, entry_price=entry_price, contracts=25, series=series)


@pytest.mark.parametrize("side,ask", [("yes", ".9740"), ("no", ".9130"),
                                     ("yes", ".9950"), ("no", ".0040")])
def test_executable_ask_preserves_decimal_cents_and_contract_side(monkeypatch, side, ask):
    opposite_bid = str(Decimal(1) - Decimal(ask))
    ladder = [[opposite_bid, "2.25"], [opposite_bid, "0.75"]]
    mock_book(monkeypatch, no=ladder if side == "yes" else (),
              yes=ladder if side == "no" else ())
    quote = execution.demo_executable_ask(TICKER, side)
    assert quote["status"] == "ready"
    assert quote["book_ask_dollars"] == float(ask)
    assert quote["book_top_quantity"] == 3
    assert quote["book_side_quantity"] == 3
    assert quote["book_http_status"] == 200
    assert quote["book_observed_at"]


@pytest.mark.parametrize("side", ["yes", "no"])
def test_empty_summary_does_not_hide_an_executable_market(monkeypatch, orders, side):
    ladder = [["0.0260", "10.25"], ["0.0200", "50.00"]]
    reads = mock_book(monkeypatch, no=ladder if side == "yes" else (),
                      yes=ladder if side == "no" else ())
    result = mirror(monkeypatch, side=side)
    assert result["status"] == "sent"
    assert len(reads) == len(orders) == 1
    assert orders[0] == {"ticker": TICKER, "side": side, "count": 25,
                         "price_dollars": .974, "tif": "immediate_or_cancel"}
    assert result["price_cents"] == 97.4
    assert result["summary_ask_dollars"] == 1
    assert result["demo_ask_in_main"] is True
    assert result["summary_book_delta_cents"] is None


def test_stale_summary_prices_evidence_but_does_not_price_order(monkeypatch, orders):
    mock_book(monkeypatch, no=[["0.0860", "2.00"]])
    result = mirror(monkeypatch, [market(yes_ask_dollars="0.8500")])
    assert orders[0]["price_dollars"] == .914
    assert result["summary_ask_dollars"] == .85
    assert result["summary_ask_raw"]["yes_ask_dollars"] == "0.8500"
    assert result["summary_book_delta_cents"] == 6.4


@pytest.mark.parametrize("ask", [.77, .984, .995])
def test_w7_main_guard_reports_valid_out_of_band_ask(monkeypatch, orders, ask):
    mock_book(monkeypatch, no=[[str(Decimal(1) - Decimal(str(ask))), "25.00"]])
    result = mirror(monkeypatch)
    assert result["status"] == "price_outside_main"
    assert result["book_ask_dollars"] == ask
    assert result["demo_ask_in_main"] is False
    assert not orders


@pytest.mark.parametrize("ask", [.78, .98])
def test_w7_main_bounds_are_inclusive(monkeypatch, orders, ask):
    mock_book(monkeypatch, no=[[str(Decimal(1) - Decimal(str(ask))), "25.00"]])
    assert mirror(monkeypatch)["status"] == "sent"
    assert orders[0]["price_dollars"] == ask


def test_missing_market_is_not_fallback_to_other_coin_or_close(monkeypatch, orders):
    result = mirror(monkeypatch, [
        market(ticker="KXETH15M-99SEP150030-30"),
        market(ticker="KXBTC15M-99SEP150045-45", close_time="2099-09-15T04:45:00Z"),
        market(status="closed"),
    ], series=(SERIES, "KXETH15M", "KXBTC"))
    assert result["status"] == "missing_market"
    assert result["series"] == SERIES
    assert result["side"] == "yes"
    assert result["prod_close"] == CLOSE
    assert not orders


def test_equivalent_close_timezone_is_same_window(monkeypatch, orders):
    mock_book(monkeypatch, no=[["0.0900", "25.00"]])
    result = mirror(monkeypatch, [market(close_time="2099-09-15T00:30:00-04:00")])
    assert result["status"] == "sent"
    assert len(orders) == 1


def test_empty_opposite_side_is_not_market_missing(monkeypatch, orders):
    mock_book(monkeypatch, yes=[["0.9100", "25.00"]], no=[])
    result = mirror(monkeypatch)
    assert result["status"] == "empty_side"
    assert result["demo_ticker"] == TICKER
    assert not orders


@pytest.mark.parametrize("payload,status_code", [
    ({}, 503), ({}, 429), ({}, 200),
    ({"orderbook_fp": {"no_dollars": [["NaN", "1.00"]]}}, 200),
    ({"orderbook_fp": {"no_dollars": [["0.12345", "1.00"]]}}, 200),
    ({"orderbook_fp": {"no_dollars": [["0.1234", "-1.00"]]}}, 200),
    (ValueError("invalid json"), 200),
])
def test_failed_or_malformed_books_fail_closed(monkeypatch, orders, payload, status_code):
    reads = mock_book(monkeypatch, payload=payload, status_code=status_code)
    result = mirror(monkeypatch)
    assert result["status"] == "orderbook_unavailable"
    assert result["book_error"]
    assert len(reads) == 1
    assert not orders


def test_book_timeout_does_not_retry_or_submit(monkeypatch, orders):
    reads = []

    def timeout(*args, **kwargs):
        reads.append(args)
        raise requests.Timeout("read timed out")

    monkeypatch.setattr(requests, "get", timeout)
    result = mirror(monkeypatch)
    assert result["status"] == "orderbook_unavailable"
    assert len(reads) == 1
    assert not orders


def test_market_list_failure_is_not_claimed_missing(monkeypatch, orders):
    monkeypatch.setattr(execution, "_demo_markets_cached", lambda s, **kwargs: None)
    result = execution.EventExecutionRouter(strategy="w7_noisefade").mirror_demo(
        side="yes", close_time=CLOSE, entry_price=.9, contracts=25, series=(SERIES,))
    assert result["status"] == "skipped_market_list_unavailable"
    assert result["series"] == SERIES
    assert not orders


def test_stale_list_can_discover_but_cannot_claim_a_market_missing(monkeypatch, orders):
    result = mirror(monkeypatch, execution._Stale([], 120))
    assert result["status"] == "skipped_market_list_unavailable"
    assert result["market_list_stale_s"] == 120
    assert not orders


def test_15m_discovery_keeps_unquoted_and_fractional_cent_summaries(monkeypatch):
    rows = [market(), market(ticker="KXBTC15M-99SEP150045-45", yes_ask_dollars="0.9960")]
    monkeypatch.setattr(requests, "get", lambda *a, **k: Response({"markets": rows}))
    assert execution._demo_markets_cached((SERIES,), include_unquoted=True) == rows


def test_w5_legacy_hourly_selection_and_order_are_unchanged(monkeypatch, orders):
    hourly = {"ticker": "KXBTC-HOURLY", "close_time": CLOSE, "yes_ask": 22}
    monkeypatch.setattr(execution, "_demo_markets_cached", lambda s: [hourly])
    result = execution.EventExecutionRouter(strategy="w5_knockdown").mirror_demo(
        side="yes", close_time=CLOSE, entry_price=.21, contracts=25, series=("KXBTC",))
    assert result["status"] == "sent"
    assert orders == [{"ticker": "KXBTC-HOURLY", "side": "yes", "count": 25,
                       "price_cents": 22}]


def test_unknown_post_outcome_is_not_retried_and_keeps_identity(monkeypatch):
    calls = []

    class Client:
        def __init__(self, *, env):
            assert env == "demo"

        def create_order(self, **kwargs):
            calls.append(kwargs)
            raise requests.Timeout("POST timed out")

    monkeypatch.setattr(rest_event, "KalshiEventOrderClient", Client)
    mock_book(monkeypatch, no=[["0.0900", "25.00"]])
    result = mirror(monkeypatch)
    assert result["status"] == "error"
    assert result["demo_ticker"] == TICKER
    assert result["side"] == "yes"
    assert result["price_dollars"] == .91
    assert len(calls) == 1


def test_slow_book_is_not_submitted_as_a_fresh_quote(monkeypatch, orders):
    monkeypatch.setattr(execution, 'demo_executable_ask', lambda *a, **k: dict(
        status='ready', book_ask_dollars=.91, book_read_duration_ms=2001.))
    result = mirror(monkeypatch)
    assert result['status'] == 'orderbook_stale'
    assert not orders
