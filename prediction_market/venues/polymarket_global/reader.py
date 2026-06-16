"""Polymarket Global — READ-ONLY reference reader (plan 07 §2, 08).

Polymarket Global (`clob.polymarket.com` + Gamma + Data) is the deeper, often
price-leading pool. From the US it is **readable but NOT tradable** (order
geoblock). We use it purely as a third reference price for cross-venue value
(plan 08): when Polymarket US diverges from Global's de-vigged price, that is a
signal the thinner US pool may be mispriced.

NO credentials required — all endpoints are public. This class deliberately does
NOT implement ExecutionVenue; `venue_guard` additionally blocks any order routed
to `poly_global`.

Three public APIs (plan 07 §2):
  * Gamma  (`gamma-api.polymarket.com`)  — market/event discovery + metadata
  * CLOB   (`clob.polymarket.com`)       — order book / price / midpoint reads
  * Data   (`data-api.polymarket.com`)   — public trades / positions / OI

Identifiers: condition_id (market) / token_id (a YES or NO outcome). Prices are
0–1 and read directly as implied probabilities.
"""
from __future__ import annotations

from decimal import Decimal

import requests

from prediction_market.config import CONFIG
from prediction_market.venues.base import OrderBook

_VENUE = "poly_global"


def parse_clob_book(payload: dict, token_id: str) -> OrderBook:
    """Normalise a CLOB `/book` response into our two-sided OrderBook.

    Unlike Kalshi (bids only), Polymarket returns both `bids` and `asks` for a
    token directly. For the YES token: yes_bid = best (max) bid, yes_ask = best
    (min) ask; the NO side is the binary complement (1 − yes price).
    """
    bids = payload.get("bids") or []
    asks = payload.get("asks") or []
    # Polymarket returns levels as {"price": "0.42", "size": "100"}; best bid is
    # the highest price, best ask the lowest.
    best_bid = max((Decimal(str(b["price"])) for b in bids), default=None)
    best_ask = min((Decimal(str(a["price"])) for a in asks), default=None)
    bid_depth = next((Decimal(str(b["size"])) for b in bids
                      if Decimal(str(b["price"])) == best_bid), None) if best_bid is not None else None
    ask_depth = next((Decimal(str(a["size"])) for a in asks
                      if Decimal(str(a["price"])) == best_ask), None) if best_ask is not None else None

    one = Decimal("1")
    no_bid = (one - best_ask) if best_ask is not None else None
    no_ask = (one - best_bid) if best_bid is not None else None
    return OrderBook(
        venue=_VENUE, market_key=token_id,
        yes_bid=best_bid, yes_ask=best_ask, no_bid=no_bid, no_ask=no_ask,
        yes_depth=bid_depth, no_depth=ask_depth,
    )


class PolymarketGlobalReader:
    """Read-only client for Polymarket Global. Never places orders."""

    name = _VENUE
    executable = False

    def __init__(self, *, timeout: float = 15.0):
        self.cfg = CONFIG.venue
        self.timeout = timeout
        self._s = requests.Session()
        self._s.headers.update({"User-Agent": "someopark-prediction-market/1.0"})

    def _get(self, base: str, path: str, params: dict | None = None):
        r = self._s.get(f"{base}{path}", params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    # ── Gamma: discovery ─────────────────────────────────────────────────────
    def list_events(self, **params) -> list[dict]:
        return self._get(self.cfg.pmglobal_gamma, "/events", params)

    def list_markets(self, **params) -> list[dict]:
        return self._get(self.cfg.pmglobal_gamma, "/markets", params)

    def search_events(self, query: str, *, limit: int = 200) -> list[dict]:
        """Client-side keyword filter over Gamma events (slug/title contains query)."""
        q = query.lower()
        events = self.list_events(limit=limit, closed="false")
        return [e for e in events
                if q in (e.get("slug", "") + " " + e.get("title", "")).lower()]

    # ── CLOB: order book / price ─────────────────────────────────────────────
    def get_book(self, token_id: str) -> OrderBook:
        payload = self._get(self.cfg.pmglobal_clob, "/book", {"token_id": token_id})
        return parse_clob_book(payload, token_id)

    def get_price(self, token_id: str, side: str = "buy") -> Decimal | None:
        payload = self._get(self.cfg.pmglobal_clob, "/price", {"token_id": token_id, "side": side})
        p = payload.get("price")
        return Decimal(str(p)) if p is not None else None

    def get_midpoint(self, token_id: str) -> Decimal | None:
        payload = self._get(self.cfg.pmglobal_clob, "/midpoint", {"token_id": token_id})
        m = payload.get("mid")
        return Decimal(str(m)) if m is not None else None

    # ── Data: public trades ──────────────────────────────────────────────────
    def recent_trades(self, **params) -> list[dict]:
        return self._get(self.cfg.pmglobal_data, "/trades", params)


if __name__ == "__main__":
    r = PolymarketGlobalReader()
    wc = r.search_events("world-cup", limit=300) or r.search_events("world cup", limit=300)
    print(f"Gamma: found {len(wc)} open event(s) matching 'world cup'")
    for e in wc[:5]:
        print(f"  - {e.get('slug')}  ({len(e.get('markets', []))} markets)")
    if not wc:
        print("  (none open right now — Gamma discovery still reachable)")
