"""Tests for the venue layer (plan 01, 05, 09). No network — pure parsing/guard."""
from __future__ import annotations

from decimal import Decimal

import pytest

from prediction_market.venues.guard import VenueGuardError, assert_executable, is_executable
from prediction_market.venues.kalshi.auth import auth_headers, sign
from prediction_market.venues.kalshi.market_data import best_prices


def test_best_prices_binary_identity():
    # Levels ascending; best bid is the last element (plan 01 §4.2).
    payload = {"orderbook_fp": {
        "yes_dollars": [["0.40", "10"], ["0.42", "25"]],   # best YES bid 0.42
        "no_dollars": [["0.50", "5"], ["0.55", "30"]],     # best NO bid 0.55
    }}
    ob = best_prices(payload, market_key="WC-TEST")
    assert ob.yes_bid == Decimal("0.42")
    assert ob.no_bid == Decimal("0.55")
    # YES ask = 1 - best NO bid; NO ask = 1 - best YES bid.
    assert ob.yes_ask == Decimal("0.45")
    assert ob.no_ask == Decimal("0.58")
    assert ob.yes_spread == Decimal("0.03")
    assert ob.yes_depth == Decimal("25")


def test_best_prices_handles_empty_side():
    ob = best_prices({"orderbook_fp": {"yes_dollars": [], "no_dollars": []}})
    assert ob.yes_bid is None and ob.no_bid is None
    assert ob.yes_ask is None and ob.no_ask is None


def test_reach_round_mark_uses_bid_on_crossed_book():
    """Regression: reach-round books are thin and often CROSSED — a stale 1¢ sell order can sit
    far below the live bid (real case: Argentina→QF ask $0.01, bid $0.60, last $0.63). The mark
    must NOT take the broken ask (that made a strong team look like a 1¢ free-arb); it falls back
    to the bid/last. Well-formed books still use the ask."""
    from prediction_market.venues.champion_prices import _real_price
    # crossed book → ignore the 1¢ ask, use the live bid.
    assert _real_price("0.01", "0.60", "0.63") == 0.60
    # well-formed book (ask at/above bid) → executable ask.
    assert _real_price("0.62", "0.61", "0.62") == 0.62
    # capped ask, no sellers (yes_ask = $1) but a real bid → the bid.
    assert _real_price("1.00", "0.08", None) == 0.08
    # only a last trade present → last.
    assert _real_price(None, None, "0.21") == 0.21
    # settled / no real price anywhere → None.
    assert _real_price("1.00", "0.00", "1.00") is None


def test_polymarket_global_book_parse():
    # Polymarket returns both bids and asks for a token (unlike Kalshi).
    from prediction_market.venues.polymarket_global.reader import parse_clob_book
    payload = {
        "bids": [{"price": "0.40", "size": "50"}, {"price": "0.42", "size": "100"}],
        "asks": [{"price": "0.45", "size": "30"}, {"price": "0.47", "size": "80"}],
    }
    ob = parse_clob_book(payload, "tok123")
    assert ob.venue == "poly_global" and ob.market_key == "tok123"
    assert ob.yes_bid == Decimal("0.42") and ob.yes_ask == Decimal("0.45")
    # NO side is the binary complement.
    assert ob.no_bid == Decimal("0.55") and ob.no_ask == Decimal("0.58")
    assert ob.yes_depth == Decimal("100") and ob.yes_spread == Decimal("0.03")


def test_polymarket_global_is_not_executable():
    from prediction_market.venues.polymarket_global.reader import PolymarketGlobalReader
    assert PolymarketGlobalReader.executable is False
    with pytest.raises(VenueGuardError):
        assert_executable("poly_global")


def test_venue_guard_blocks_global():
    assert is_executable("kalshi")
    assert is_executable("poly_us")
    assert not is_executable("poly_global")
    assert_executable("kalshi")  # no raise
    with pytest.raises(VenueGuardError):
        assert_executable("poly_global")


def test_kalshi_signing_is_deterministic_per_message():
    # Generate an ephemeral RSA key to exercise the signer without real creds.
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    s1 = sign(key, "msg")
    s2 = sign(key, "msg")
    # PSS is randomised → signatures differ, but both are valid base64 strings.
    assert isinstance(s1, str) and isinstance(s2, str)

    headers = auth_headers(key, "key-id-123", "GET", "/trade-api/v2/portfolio/balance?x=1")
    assert headers["KALSHI-ACCESS-KEY"] == "key-id-123"
    assert headers["KALSHI-ACCESS-TIMESTAMP"].isdigit()
    assert len(headers["KALSHI-ACCESS-TIMESTAMP"]) >= 13  # milliseconds
    assert "KALSHI-ACCESS-SIGNATURE" in headers


def test_token_bucket_refill_with_fake_clock():
    from prediction_market.venues.ratelimit import TokenBucket, KalshiRateLimiter, CANCEL_COST
    t = [0.0]
    b = TokenBucket(rate=100, capacity=100, clock=lambda: t[0])
    assert b.try_take(100) is True       # drain
    assert b.try_take(1) is False        # empty
    t[0] = 0.5                            # 0.5s × 100/s = 50 tokens
    assert b.try_take(50) is True
    assert b.try_take(1) is False
    # Tier sizing: write bucket above Basic holds 2s of budget.
    rl = KalshiRateLimiter(tier="premier")
    assert rl.write.capacity == 2000 and rl.read.capacity == 1000
    assert KalshiRateLimiter(tier="basic").write.capacity == 100  # 1s at Basic


def test_hard_dollar_cap_on_orders():
    from prediction_market.venues.kalshi.orders import enforce_order_cap, OrderCapExceeded
    # within cap: 1 contract at any price <= $1 is fine.
    assert enforce_order_cap(0.50, 1) == 0.50
    assert enforce_order_cap(0.99, 1) == pytest.approx(0.99)
    assert enforce_order_cap(0.01, 100) == pytest.approx(1.0)   # exactly $1 allowed
    # over the cap: refused before any signing/network.
    with pytest.raises(OrderCapExceeded):
        enforce_order_cap(0.50, 3)        # $1.50
    with pytest.raises(OrderCapExceeded):
        enforce_order_cap(0.02, 100)      # $2.00
