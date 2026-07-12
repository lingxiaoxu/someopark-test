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
