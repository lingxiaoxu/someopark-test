"""Kalshi base URLs + wire constants (Plan 00 §3.2, probe-verified 2026-07-07).

Facts locked in by the live probe:
  * `/margin/*` REST market data (markets/orderbook/trades/candlesticks/funding)
    is PUBLIC — no auth. fee_tiers / balance / orders / WS handshake need auth.
  * There is NO separate margin WS path — perp channels (`ticker`, `trade`,
    `orderbook_delta`) ride the main /trade-api/ws/v2 socket.
  * Perp tickers carry the KX prefix (KXBTCPERP), prices are decimal dollars.
"""
from __future__ import annotations

from crypto_trading.crypto_common.config import kalshi_env

API_ROOT = "/trade-api/v2"
WS_ROOT = "/trade-api/ws/v2"

REST_HOSTS = {
    "demo": "https://external-api.demo.kalshi.co",
    "prod": "https://external-api.kalshi.com",
}
WS_HOSTS = {
    "demo": "wss://external-api-ws.demo.kalshi.co",
    "prod": "wss://external-api-ws.kalshi.com",
}


def rest_base(env: str | None = None) -> str:
    return REST_HOSTS[env or kalshi_env()] + API_ROOT


def ws_url(env: str | None = None) -> str:
    return WS_HOSTS[env or kalshi_env()] + WS_ROOT


# WS channels available for perps (probe: all three subscribe OK on demo).
WS_PERP_CHANNELS = ("ticker", "trade", "orderbook_delta")

# Candlestick bar sizes accepted by period_interval (minutes).
CANDLE_PERIODS = {"1m": 1, "1h": 60, "1d": 1440}

# Empty-book sentinel seen in launch-day candles: int64-max scaled by 1e-4.
# Any price ≥ this bound (or ≤ 0) is a placeholder, not a market price.
PRICE_SENTINEL_BOUND = 1e12
