"""Kalshi MARGIN (perps) REST client (Plan 00 §3.4, probe-verified 2026-07-07).

Two access levels:
  * PUBLIC (no auth): markets, market detail, orderbook, trades, candlesticks,
    funding estimate + historical.  These power backfill + the prod poller and
    need NO API key.
  * AUTHED: fee_tiers, enabled, balance, positions, funding_history (ledger),
    orders.  Reads may use the borrowed PM demo key; ORDER PATHS MUST NOT
    (config.kalshi_key(borrowed_ok=False)) — and additionally require
    KALSHI_ENV=prod + ALLOW_LIVE_ORDERS=1 + /margin/enabled (Plan 00 §3.8).

Order placement is deliberately NOT implemented yet — data phase only. The
demo-first gate lands together with execution.py (Plan 00 build order step 4).
"""
from __future__ import annotations

import logging
import time

import requests

from crypto_trading.crypto_common.config import KalshiKey, kalshi_key
from crypto_trading.crypto_common.kalshi.auth import auth_headers, load_private_key
from crypto_trading.crypto_common.kalshi.enums import API_ROOT, rest_base
from crypto_trading.crypto_common.kalshi.ratelimit import KalshiRateLimiter, backoff_delays

logger = logging.getLogger(__name__)


class KalshiMarginClient:
    """REST client for /margin/*. Public reads work with no key at all."""

    def __init__(self, *, env: str | None = None, key: KalshiKey | None = None,
                 timeout: float = 15.0, limiter: KalshiRateLimiter | None = None,
                 min_interval: float = 0.0):
        self.base = rest_base(env)
        self.timeout = timeout
        self.limiter = limiter or KalshiRateLimiter()
        # Public (unauth) endpoints throttle harder than the authed basic tier
        # (429s observed at full bucket speed) — bulk callers set min_interval.
        self.min_interval = float(min_interval)
        self._last_req = 0.0
        self._s = requests.Session()
        self._s.headers["User-Agent"] = "someopark-crypto/0.1"
        self._key = key
        self._pk = None

    # ── plumbing ──────────────────────────────────────────────────────────
    def _ensure_key(self) -> KalshiKey:
        if self._key is None:
            self._key = kalshi_key("margin", borrowed_ok=True)
        if self._pk is None:
            self._pk = load_private_key(self._key.expanded_path())
        return self._key

    def _get(self, path: str, params: dict | None = None, *, authed: bool = False) -> dict:
        headers = {}
        if authed:
            key = self._ensure_key()
            headers = auth_headers(self._pk, key.key_id, "GET", f"{API_ROOT}{path}")
        self.limiter.acquire_read()
        if self.min_interval > 0:
            wait = self._last_req + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
        for i, delay in enumerate([0.0] + backoff_delays()):
            if delay:
                time.sleep(delay)
            self._last_req = time.monotonic()
            r = self._s.get(self.base + path, params=params, headers=headers,
                            timeout=self.timeout)
            if r.status_code == 429:
                logger.warning("429 on %s (attempt %d) — backing off", path, i + 1)
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"rate-limited out of retries on GET {path}")

    # ── PUBLIC market data (keyless) ──────────────────────────────────────
    def markets(self) -> list[dict]:
        return self._get("/margin/markets").get("markets", [])

    def market(self, ticker: str) -> dict:
        return self._get(f"/margin/markets/{ticker}").get("market", {})

    def orderbook(self, ticker: str) -> dict:
        """{"orderbook": {"asks": [[px, sz], …], "bids": …}} — decimal-dollar strings."""
        return self._get(f"/margin/markets/{ticker}/orderbook")

    def trades(self, ticker: str, *, limit: int = 100, cursor: str | None = None) -> dict:
        params = {"ticker": ticker, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self._get("/margin/trades", params)

    def candlesticks(self, ticker: str, start_ts: int, end_ts: int,
                     period_interval: int) -> list[dict]:
        """One raw request — callers chunk (server caps bars per response)."""
        payload = self._get(f"/margin/markets/{ticker}/candlesticks",
                            {"start_ts": int(start_ts), "end_ts": int(end_ts),
                             "period_interval": int(period_interval)})
        return payload.get("candlesticks", [])

    def funding_rate_estimate(self, ticker: str) -> dict:
        return self._get("/margin/funding_rates/estimate", {"ticker": ticker})

    def funding_rates_historical(self, ticker: str, *, limit: int = 500,
                                 cursor: str | None = None) -> dict:
        params = {"ticker": ticker, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self._get("/margin/funding_rates/historical", params)

    # ── AUTHED account reads (borrowed key OK — read-only) ────────────────
    def enabled(self) -> bool:
        return bool(self._get("/margin/enabled", authed=True).get("enabled", False))

    def fee_tiers(self) -> dict:
        return self._get("/margin/fee_tiers", authed=True)

    def balance(self) -> dict:
        return self._get("/margin/balance", authed=True)
