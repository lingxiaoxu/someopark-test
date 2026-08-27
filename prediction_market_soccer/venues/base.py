"""Unified venue abstraction (plan 05 §3, 07 §3).

Three concrete venues plug into this interface:
  * KalshiVenue          — execution + market data (RSA-PSS auth)
  * PolymarketUSVenue    — execution + market data (Ed25519, NY-legal)
  * PolymarketGlobalReader — READ-ONLY reference (never places orders)

The engine works in terms of a normalised order book and TARGET NET POSITIONS
(plan 09 §5); each venue translates those into its own legal order primitives.
This module defines only the shared data types + interface; concrete REST/WS
clients live under venues/<name>/.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class BookLevel:
    price: Decimal      # 0..1 implied probability (dollars)
    size: Decimal       # contracts available


@dataclass(frozen=True)
class OrderBook:
    """Normalised two-sided book (plan 07 §3): yes/no bid + derived ask.

    In a binary market a YES bid @ X equals a NO ask @ (1-X). The best YES ASK
    (what you pay to buy YES) is therefore 1 - best NO bid (plan 01 §4.2).
    """
    venue: str
    market_key: str     # kalshi ticker / poly_us slug / poly_global token_id
    yes_bid: Decimal | None
    yes_ask: Decimal | None
    no_bid: Decimal | None
    no_ask: Decimal | None
    yes_depth: Decimal | None = None
    no_depth: Decimal | None = None

    @property
    def yes_spread(self) -> Decimal | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return self.yes_ask - self.yes_bid


@dataclass(frozen=True)
class Balance:
    venue: str
    cash: Decimal
    portfolio_value: Decimal | None = None


@dataclass(frozen=True)
class Position:
    venue: str
    market_key: str
    net_count: int          # signed: +YES / -NO
    avg_price: Decimal


class Venue(abc.ABC):
    """Common surface for market data; execution venues extend with orders."""

    name: str
    executable: bool  # True only for kalshi / poly_us (plan 05 venue_guard)

    @abc.abstractmethod
    def get_book(self, market_key: str) -> OrderBook: ...

    @abc.abstractmethod
    def list_markets(self, **filters) -> list[dict]: ...


class ExecutionVenue(Venue):
    """A venue we may place orders on. Global Polymarket must NOT implement this."""

    @abc.abstractmethod
    def get_balance(self) -> Balance: ...

    @abc.abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abc.abstractmethod
    def place_order(self, *args, **kwargs): ...

    @abc.abstractmethod
    def cancel_order(self, order_id: str): ...
