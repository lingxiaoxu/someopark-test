"""Kalshi PUBLIC market data (plan 01 §3-§4). No auth required.

Two layers:
  * ``best_prices`` — PURE parser turning a raw orderbook payload into the
    derived two-sided best bid/ask (binary identity: YES ask = 1 - best NO bid).
    Unit-tested without any network.
  * ``KalshiMarketData`` — thin REST client (markets / orderbook discovery).
    Hits the live (demo or prod) public endpoints; used by the ingest layer.

Prices use Decimal throughout (plan 01 §4.3 — never float for money).
"""
from __future__ import annotations

from decimal import Decimal

import requests

from prediction_market.config import CONFIG
from prediction_market.venues.base import OrderBook


def best_prices(orderbook_payload: dict, *, venue: str = "kalshi", market_key: str = "") -> OrderBook:
    """Parse a Kalshi orderbook payload into a normalised OrderBook.

    Expects ``{"orderbook_fp": {"yes_dollars": [[price, size], ...],
    "no_dollars": [...]}}`` with levels ascending by price (best bid last,
    plan 01 §4.2). Missing sides yield None.
    """
    ob = orderbook_payload.get("orderbook_fp", orderbook_payload)
    yes = ob.get("yes_dollars") or []
    no = ob.get("no_dollars") or []

    yes_bid = Decimal(str(yes[-1][0])) if yes else None
    no_bid = Decimal(str(no[-1][0])) if no else None
    yes_depth = Decimal(str(yes[-1][1])) if yes else None
    no_depth = Decimal(str(no[-1][1])) if no else None

    one = Decimal("1")
    yes_ask = (one - no_bid) if no_bid is not None else None  # buy-YES fill price
    no_ask = (one - yes_bid) if yes_bid is not None else None

    return OrderBook(
        venue=venue, market_key=market_key,
        yes_bid=yes_bid, yes_ask=yes_ask, no_bid=no_bid, no_ask=no_ask,
        yes_depth=yes_depth, no_depth=no_depth,
    )


class KalshiMarketData:
    """Read-only public REST client (no auth). Demo or prod per KALSHI_ENV."""

    def __init__(self, base_url: str | None = None, *, timeout: float = 10.0):
        self.base = base_url or CONFIG.venue.kalshi_rest
        self.timeout = timeout
        self._session = requests.Session()

    def _get(self, path: str, params: dict | None = None) -> dict:
        resp = self._session.get(f"{self.base}{path}", params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def list_series(self, category: str = "Sports") -> list[dict]:
        return self._get("/series", {"category": category}).get("series", [])

    def list_events(self, series_ticker: str, status: str = "open") -> list[dict]:
        params = {"series_ticker": series_ticker, "status": status, "with_nested_markets": "true"}
        return self._get("/events", params).get("events", [])

    def list_markets(self, **filters) -> list[dict]:
        """Paginated market listing (cursor-followed)."""
        out: list[dict] = []
        cursor = None
        while True:
            params = dict(filters, limit=1000)
            if cursor:
                params["cursor"] = cursor
            page = self._get("/markets", params)
            out.extend(page.get("markets", []))
            cursor = page.get("cursor")
            if not cursor:
                return out

    def get_orderbook(self, ticker: str) -> OrderBook:
        payload = self._get(f"/markets/{ticker}/orderbook")
        return best_prices(payload, market_key=ticker)
