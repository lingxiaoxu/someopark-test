"""Kalshi EVENT-contract REST client (Plan 00 §3.1, Plan 02 data leg).

Adapted COPY of prediction_market/venues/kalshi/market_data.py (read-only
template) — same public no-auth endpoints, kept series/event/market discovery
and added the strike-strip helpers Plan 02 needs. Prices are 0–1 dollars
(binary contracts); we persist raw payloads and parse at load time.
"""
from __future__ import annotations

import logging
import time

import requests

from crypto_trading.crypto_common.kalshi.enums import rest_base
from crypto_trading.crypto_common.kalshi.ratelimit import KalshiRateLimiter, backoff_delays

logger = logging.getLogger(__name__)


class KalshiEventClient:
    """Read-only public REST client for the event-contract namespace."""

    def __init__(self, *, env: str | None = None, timeout: float = 15.0,
                 limiter: KalshiRateLimiter | None = None):
        self.base = rest_base(env)
        self.timeout = timeout
        # Separate limiter instance from the margin client (Plan 00 §3.7).
        self.limiter = limiter or KalshiRateLimiter()
        self._s = requests.Session()
        self._s.headers["User-Agent"] = "someopark-crypto/0.1"

    def _get(self, path: str, params: dict | None = None) -> dict:
        self.limiter.acquire_read()
        for i, delay in enumerate([0.0] + backoff_delays()):
            if delay:
                time.sleep(delay)
            r = self._s.get(self.base + path, params=params, timeout=self.timeout)
            if r.status_code == 429:
                logger.warning("429 on %s (attempt %d) — backing off", path, i + 1)
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"rate-limited out of retries on GET {path}")

    # ── discovery (mirrors PM discovery/market_data contract) ─────────────
    def list_series(self, category: str) -> list[dict]:
        return self._get("/series", {"category": category}).get("series", [])

    def list_events(self, series_ticker: str, status: str = "open") -> list[dict]:
        params = {"series_ticker": series_ticker, "status": status,
                  "with_nested_markets": "true"}
        return self._get("/events", params).get("events", [])

    def list_markets(self, **filters) -> list[dict]:
        """Paginated market listing (cursor-followed) — PM pattern verbatim."""
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

    def orderbook_raw(self, ticker: str) -> dict:
        """Raw orderbook payload (yes_dollars/no_dollars) — recorder persists as-is."""
        return self._get(f"/markets/{ticker}/orderbook")


# ── order flow (added 2026-08-25, V2 endpoint) ──────────────────────────────
# Events orders moved to V2: POST /portfolio/events/orders with the SAME wire
# shape as perps (single-book bid/ask on the YES leg, fixed-point dollar
# prices, 2-dp count). The legacy /portfolio/orders returns 410 (probed
# 2026-08-25). Semantics: bid = buy YES; ask = sell YES ≡ buy NO at (1−p).

EVENTS_ORDERS_V2 = "/portfolio/events/orders"


class KalshiEventOrderClient:
    """Minimal authed V2 order client for event contracts.

    Env-aware; the borrowed demo key is acceptable for env="demo", prod
    ordering requires the dedicated key upstream (Plan 00 §3.8 applies at the
    caller — this class only refuses cross-env confusion by binding the base
    URL at construction).
    """

    def __init__(self, *, env: str | None = None, timeout: float = 15.0):
        from crypto_trading.crypto_common.config import kalshi_key
        from crypto_trading.crypto_common.kalshi.auth import load_private_key
        from crypto_trading.crypto_common.kalshi.enums import rest_base
        self.env = env or "demo"
        self.base = rest_base(self.env)
        self.timeout = timeout
        self._key = kalshi_key("margin", borrowed_ok=(self.env != "prod"))
        self._pk = load_private_key(self._key.expanded_path())
        import requests
        self._s = requests.Session()
        self._s.headers["User-Agent"] = "someopark-crypto/0.1"

    def _authed(self, method: str, path: str, body: dict | None = None):
        from crypto_trading.crypto_common.kalshi.auth import auth_headers
        from crypto_trading.crypto_common.kalshi.enums import API_ROOT
        h = auth_headers(self._pk, self._key.key_id, method, f"{API_ROOT}{path}")
        fn = {"GET": self._s.get, "POST": self._s.post,
              "DELETE": self._s.delete}[method]
        kw = {"headers": h, "timeout": self.timeout}
        if body is not None:
            kw["json"] = body
        return fn(f"{self.base}{path}", **kw)

    @staticmethod
    def v2_body(*, ticker: str, contract_side: str, price_dollars: float,
                count: int, client_order_id: str | None = None,
                tif: str = "immediate_or_cancel",
                post_only: bool = False) -> dict:
        """Translate contract-side semantics (yes/no) to the V2 single book.

        Buying YES at p  → bid @ p.  Buying NO at p → ask @ (1 − p): selling
        the YES leg at 1−p is экономически identical to owning NO at p.
        """
        import uuid
        assert contract_side in ("yes", "no")
        assert 0.0 < price_dollars < 1.0
        side = "bid" if contract_side == "yes" else "ask"
        px = price_dollars if contract_side == "yes" else round(1.0 - price_dollars, 4)
        b = {"ticker": ticker, "side": side, "count": f"{int(count)}.00",
             "price": f"{px:.4f}", "time_in_force": tif,
             "self_trade_prevention_type": "taker_at_cross",
             "client_order_id": client_order_id or str(uuid.uuid4())}
        if post_only:
            b["post_only"] = True
        return b

    def create_order(self, *, ticker: str, side: str, action: str = "buy",
                     count: int = 1, price_cents: int | None = None,
                     price_dollars: float | None = None,
                     client_order_id: str | None = None,
                     tif: str = "immediate_or_cancel") -> dict:
        """side: yes|no (contract semantics; translated to V2 bid/ask)."""
        assert action == "buy", "sell = buy the other side; keep semantics simple"
        px = price_dollars if price_dollars is not None else price_cents / 100.0
        body = self.v2_body(ticker=ticker, contract_side=side,
                            price_dollars=px, count=count,
                            client_order_id=client_order_id, tif=tif)
        r = self._authed("POST", EVENTS_ORDERS_V2, body)
        return {"status_code": r.status_code, "response": r.text[:400],
                "body_sent": body}

    def cancel_order(self, order_id: str) -> dict:
        r = self._authed("DELETE", f"{EVENTS_ORDERS_V2}/{order_id}")
        return {"status_code": r.status_code, "response": r.text[:200]}

    def get_orders(self, **params) -> dict:
        import urllib.parse
        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        r = self._authed("GET", f"{EVENTS_ORDERS_V2}{q}")
        return r.json() if r.status_code == 200 else {"status_code": r.status_code}
