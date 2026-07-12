"""Kalshi WebSocket client (Plan 00 §3.5, probe-verified 2026-07-07).

Facts baked in:
  * ONE socket serves perp channels — no /ws/v2/margin path (404s).
  * Auth headers are required at HANDSHAKE (same RSA-PSS trio as REST).
  * Perp-verified channels: ticker, trade, orderbook_delta (subscribe cmd).
  * Messages carry ``seq`` for orderbook streams — consumers detect gaps.

Async client with reconnect + resubscribe. The recorder drives it; strategies
later reuse it via the ``on_message`` callback.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

import websockets

from crypto_trading.crypto_common.config import KalshiKey, kalshi_key
from crypto_trading.crypto_common.kalshi.auth import auth_headers, load_private_key
from crypto_trading.crypto_common.kalshi.enums import WS_ROOT, ws_url

logger = logging.getLogger(__name__)

RECONNECT_DELAYS = [1, 2, 5, 10, 30, 60]  # seconds, then stay at the last


class KalshiWS:
    """Auto-reconnecting subscription client. Read-only (never sends orders)."""

    def __init__(self, channels: list[str], market_tickers: list[str], *,
                 env: str | None = None, key: KalshiKey | None = None,
                 on_message: Callable[[dict], Awaitable[None]] | None = None,
                 on_reconnect: Callable[[], Awaitable[None]] | None = None):
        self.url = ws_url(env)
        self.channels = list(channels)
        self.market_tickers = list(market_tickers)
        self.on_message = on_message
        self.on_reconnect = on_reconnect
        self._key = key or kalshi_key("margin", borrowed_ok=True)
        self._pk = load_private_key(self._key.expanded_path())
        self._stop = asyncio.Event()
        self.stats = {"messages": 0, "reconnects": 0, "errors": 0}

    def _headers(self) -> dict[str, str]:
        return auth_headers(self._pk, self._key.key_id, "GET", WS_ROOT)

    async def _subscribe(self, ws) -> None:
        for i, ch in enumerate(self.channels, start=1):
            await ws.send(json.dumps({
                "id": i, "cmd": "subscribe",
                "params": {"channels": [ch], "market_tickers": self.market_tickers},
            }))

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        """Connect → subscribe → pump messages; reconnect forever until stop()."""
        attempt = 0
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.url, additional_headers=self._headers(),
                                              open_timeout=15, ping_interval=20,
                                              ping_timeout=20, max_queue=4096) as ws:
                    attempt = 0
                    await self._subscribe(ws)
                    if self.on_reconnect and self.stats["reconnects"]:
                        await self.on_reconnect()
                    logger.info("WS connected: %s channels=%s tickers=%d",
                                self.url, self.channels, len(self.market_tickers))
                    while not self._stop.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=90)
                        self.stats["messages"] += 1
                        if self.on_message:
                            try:
                                await self.on_message(json.loads(raw))
                            except Exception:
                                self.stats["errors"] += 1
                                logger.exception("on_message handler error")
            except asyncio.TimeoutError:
                logger.warning("WS idle >90s — reconnecting")
            except Exception as e:
                logger.warning("WS dropped: %s: %s", type(e).__name__, str(e)[:200])
            if self._stop.is_set():
                break
            delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
            attempt += 1
            self.stats["reconnects"] += 1
            await asyncio.sleep(delay)
