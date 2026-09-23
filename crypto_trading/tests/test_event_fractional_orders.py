"""Event order quantities preserve actual partial fills without changing W7."""
from decimal import Decimal

import pytest

from crypto_trading.crypto_common.kalshi.rest_event import KalshiEventOrderClient


@pytest.mark.parametrize("count,wire", [(0.37, "0.37"), (0.01, "0.01"),
                                       (1.25, "1.25"), (25, "25.00"),
                                       (Decimal("0.3700"), "0.37")])
def test_exact_fractional_quantity(count, wire):
    body = KalshiEventOrderClient.v2_body(
        ticker="KXBTC15M-test", contract_side="no", price_dollars=.63,
        count=count, client_order_id="fixed")
    assert body["count"] == wire
    assert body["side"] == "ask" and body["price"] == "0.3700"


@pytest.mark.parametrize("count", [0, -1, float("nan"), float("inf"),
                                  float("-inf"), .001, .371, 1.001,
                                  Decimal("0.00001"), None, True])
def test_invalid_quantity_is_rejected_before_any_order_request(count):
    class NoNetwork(KalshiEventOrderClient):
        def __init__(self):
            pass

        def _authed(self, *args, **kwargs):
            pytest.fail("invalid quantity reached the account request")

    with pytest.raises(ValueError, match="count must be"):
        NoNetwork().create_order(ticker="KXBTC15M-test", side="yes",
                                 price_dollars=.50, count=count)


def test_integer_w7_wire_remains_exactly_identical():
    assert KalshiEventOrderClient.v2_body(
        ticker="KXBTC15M-test", contract_side="yes", price_dollars=.85,
        count=25, client_order_id="w7-fixed") == {
            "ticker": "KXBTC15M-test", "side": "bid", "count": "25.00",
            "price": "0.8500", "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": "w7-fixed"}


def test_create_order_forwards_fractional_exit_quantity_and_keeps_defaults():
    sent = {}

    class NoNetwork(KalshiEventOrderClient):
        def __init__(self):
            pass

        def _authed(self, method, path, body=None):
            sent.update(method=method, path=path, body=body)

            class Response:
                status_code = 201
                text = '{"order_id":"fractional-exit"}'

            return Response()

    result = NoNetwork().create_order(ticker="KXBTC15M-test", side="no",
                                      price_dollars=.37, count=.37,
                                      client_order_id="exit-fixed")
    assert result["status_code"] == 201
    assert sent["path"] == "/portfolio/events/orders"
    assert sent["body"]["count"] == "0.37"
    assert sent["body"]["side"] == "ask" and sent["body"]["price"] == "0.6300"
    assert sent["body"]["time_in_force"] == "immediate_or_cancel"
    assert "post_only" not in sent["body"] and "expiration_time" not in sent["body"]


@pytest.mark.parametrize("ticker", [None, "KXBTC15M-26SEP142345-45"])
def test_cancel_routes_to_contract_partition_without_changing_legacy_call(ticker):
    from urllib.parse import parse_qs, urlsplit

    sent = {}

    class NoNetwork(KalshiEventOrderClient):
        def __init__(self):
            pass

        def _authed(self, method, path, body=None):
            sent.update(method=method, path=path, body=body)

            class Response:
                status_code = 200
                text = '{"order_id":"own-demo-order"}'

            return Response()

    kwargs = {} if ticker is None else {"market_ticker": ticker}
    result = NoNetwork().cancel_order("own-demo-order", **kwargs)
    assert result["status_code"] == 200
    assert sent["method"] == "DELETE" and sent["body"] is None
    path = urlsplit(sent["path"])
    assert path.path == "/portfolio/events/orders/own-demo-order"
    assert parse_qs(path.query) == ({} if ticker is None else {
        "market_ticker": [ticker], "exchange_index": ["-1"]})
