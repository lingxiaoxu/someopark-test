"""Kalshi order placement (plan 01 §5, 12 §5). V2 event-market endpoint.

THREE independent safety layers guard every order — all must pass:
  1. ``assert_trading_enabled('kalshi')`` — prod requires KALSHI_TRADING_ENABLED;
  2. **HARD $1 cap** — notional cost (price × count) may NEVER exceed
     ``CONFIG.risk.max_test_order_usd`` (default $1.00). Enforced here in code,
     before signing, with no override path. This is the strict test limit.
  3. ``self_trade_prevention_type`` on the order itself (plan 09 §1.4).

Endpoint (plan 12 §5): ``POST /portfolio/events/orders`` with fixed-point dollar
``price`` and string ``count``; ``side`` is bid(=yes)/ask(=no); idempotent
``client_order_id``. Cancel: ``DELETE /portfolio/events/orders/{id}``.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import requests

from prediction_market_soccer.config import CONFIG
from prediction_market_soccer.venues.base import Balance
from prediction_market_soccer.venues.guard import assert_trading_enabled
from prediction_market_soccer.venues.kalshi.auth import auth_headers, load_private_key

_API_ROOT = "/trade-api/v2"


class OrderCapExceeded(RuntimeError):
    """Raised when an order's notional cost would exceed the hard $1 test cap."""


def enforce_order_cap(price_dollars: float, count: int) -> float:
    """Refuse any order whose notional cost exceeds the hard test cap. Returns notional.

    The single chokepoint for the strict ≤$1 test limit (CONFIG.risk.max_test_order_usd).
    """
    notional = float(price_dollars) * int(count)
    cap = CONFIG.risk.max_test_order_usd
    if notional > cap + 1e-9:
        raise OrderCapExceeded(
            f"order notional ${notional:.4f} (price {price_dollars} × {count}) exceeds "
            f"the hard ${cap:.2f} test cap — refused before signing")
    return notional


class KalshiOrders:
    """Authenticated Kalshi order client (read balance + place/cancel orders)."""

    def __init__(self, api_key_id: str, private_key_path: str, *,
                 base_url: str | None = None, timeout: float = 20.0):
        self.kid = api_key_id
        self.pk = load_private_key(private_key_path)
        self.base = base_url or CONFIG.venue.kalshi_rest
        self.timeout = timeout
        self._s = requests.Session()

    def _headers(self, method: str, path: str) -> dict:
        return auth_headers(self.pk, self.kid, method, f"{_API_ROOT}{path}")

    def get_balance(self) -> Balance:
        r = self._s.get(self.base + "/portfolio/balance",
                        headers=self._headers("GET", "/portfolio/balance"), timeout=self.timeout)
        r.raise_for_status()
        j = r.json()
        return Balance(venue="kalshi", cash=Decimal(str(j["balance"])) / 100,
                       portfolio_value=Decimal(str(j.get("portfolio_value", 0))) / 100)

    def create_order(self, ticker: str, side: str, count: int, price_dollars: float, *,
                     tif: str = "good_till_canceled", client_order_id: str | None = None,
                     stp: str = "taker_at_cross") -> dict:
        """Place a limit order. ``side`` = 'bid' (yes) or 'ask' (no).

        Refuses unless trading is enabled AND notional ≤ the hard $1 cap.
        """
        if side not in ("bid", "ask"):
            raise ValueError("side must be 'bid' (yes) or 'ask' (no)")
        assert_trading_enabled("kalshi")
        enforce_order_cap(price_dollars, count)
        body = {
            "ticker": ticker, "side": side, "count": str(int(count)),
            "price": f"{Decimal(str(price_dollars)):.4f}",
            "time_in_force": tif, "self_trade_prevention_type": stp,
            "client_order_id": client_order_id or str(uuid.uuid4()),
        }
        r = self._s.post(self.base + "/portfolio/events/orders", json=body,
                         headers=self._headers("POST", "/portfolio/events/orders"), timeout=self.timeout)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"create_order failed {r.status_code}: {r.text[:300]}")
        return r.json()

    def cancel_order(self, order_id: str) -> dict:
        path = f"/portfolio/events/orders/{order_id}"
        r = self._s.delete(self.base + path, headers=self._headers("DELETE", path), timeout=self.timeout)
        if r.status_code not in (200, 204):
            raise RuntimeError(f"cancel_order failed {r.status_code}: {r.text[:300]}")
        return r.json() if r.text else {"order_id": order_id, "status": "canceled"}

    def get_order(self, order_id: str) -> dict:
        path = f"/portfolio/events/orders/{order_id}"
        r = self._s.get(self.base + path, headers=self._headers("GET", path), timeout=self.timeout)
        r.raise_for_status()
        return r.json()
