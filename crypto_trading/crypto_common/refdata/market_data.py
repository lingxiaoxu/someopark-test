"""Exchange spot reference data (Plan 00 §2 `refdata/market_data.py`).

Keyless public REST drivers for the three composite legs (Plan 08 §3.2):
Coinbase Exchange, Kraken, Bitstamp. Each driver exposes the same contract:

    candles_1m(asset, start_ts, end_ts) -> DataFrame[ts, open, high, low, close, volume]
    ticker(asset) -> {"price": float, "volume_24h": float, "ts": float}

Depth of history differs by venue (documented per driver): Coinbase and
Bitstamp page arbitrarily far back; Kraken's OHLC endpoint only serves the
most recent ~720 bars, so Kraken contributes to the LIVE composite and the
recent tape, not deep backfill. Assets a venue doesn't list are skipped
gracefully (probed once, cached).
"""
from __future__ import annotations

import logging
import time

import pandas as pd
import requests

logger = logging.getLogger(__name__)

# asset (Kalshi underlying) -> per-exchange product codes; None = not listed.
PAIRS: dict[str, dict[str, str | None]] = {
    "BTC":  {"coinbase": "BTC-USD",  "kraken": "XBTUSD",  "bitstamp": "btcusd"},
    "ETH":  {"coinbase": "ETH-USD",  "kraken": "ETHUSD",  "bitstamp": "ethusd"},
    "SOL":  {"coinbase": "SOL-USD",  "kraken": "SOLUSD",  "bitstamp": "solusd"},
    "XRP":  {"coinbase": "XRP-USD",  "kraken": "XRPUSD",  "bitstamp": "xrpusd"},
    "DOGE": {"coinbase": "DOGE-USD", "kraken": "DOGEUSD", "bitstamp": "dogeusd"},
    "SHIB": {"coinbase": "SHIB-USD", "kraken": "SHIBUSD", "bitstamp": "shibusd"},
    "BCH":  {"coinbase": "BCH-USD",  "kraken": "BCHUSD",  "bitstamp": "bchusd"},
    "LTC":  {"coinbase": "LTC-USD",  "kraken": "LTCUSD",  "bitstamp": "ltcusd"},
    "LINK": {"coinbase": "LINK-USD", "kraken": "LINKUSD", "bitstamp": "linkusd"},
    "NEAR": {"coinbase": "NEAR-USD", "kraken": "NEARUSD", "bitstamp": "nearusd"},
    "SUI":  {"coinbase": "SUI-USD",  "kraken": "SUIUSD",  "bitstamp": "suiusd"},
    "HYPE": {"coinbase": None,        "kraken": None,      "bitstamp": None},  # not on US spot venues
    "ZEC":  {"coinbase": "ZEC-USD",  "kraken": "ZECUSD",  "bitstamp": None},
}

_COLS = ["ts", "open", "high", "low", "close", "volume"]


class _Driver:
    name = "?"
    min_interval = 0.35          # polite pacing on public endpoints

    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout
        self._s = requests.Session()
        self._s.headers["User-Agent"] = "someopark-crypto/0.1"
        self._last = 0.0
        self._unlisted: set[str] = set()

    def _get(self, url: str, params: dict | None = None):
        wait = self._last + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        r = self._s.get(url, params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def pair(self, asset: str) -> str | None:
        return PAIRS.get(asset, {}).get(self.name)

    def candles_1m(self, asset: str, start_ts: int, end_ts: int) -> pd.DataFrame:
        raise NotImplementedError

    def ticker(self, asset: str) -> dict | None:
        raise NotImplementedError


class Coinbase(_Driver):
    """Coinbase Exchange public API. 300 bars/request; full history paging."""
    name = "coinbase"
    BASE = "https://api.exchange.coinbase.com"

    def candles_1m(self, asset, start_ts, end_ts):
        pid = self.pair(asset)
        if not pid:
            return pd.DataFrame(columns=_COLS)
        rows = []
        cur = start_ts
        while cur < end_ts:
            chunk_end = min(cur + 300 * 60, end_ts)
            data = self._get(f"{self.BASE}/products/{pid}/candles", {
                "granularity": 60,
                "start": pd.Timestamp(cur, unit="s", tz="UTC").isoformat(),
                "end": pd.Timestamp(chunk_end, unit="s", tz="UTC").isoformat(),
            })
            #  [ time, low, high, open, close, volume ] newest-first
            rows.extend({"ts": int(c[0]), "open": float(c[3]), "high": float(c[2]),
                         "low": float(c[1]), "close": float(c[4]), "volume": float(c[5])}
                        for c in data)
            cur = chunk_end
        return pd.DataFrame(rows, columns=_COLS).sort_values("ts").drop_duplicates("ts")

    def ticker(self, asset):
        pid = self.pair(asset)
        if not pid:
            return None
        t = self._get(f"{self.BASE}/products/{pid}/ticker")
        s = self._get(f"{self.BASE}/products/{pid}/stats")
        return {"price": float(t["price"]), "volume_24h": float(s.get("volume", 0.0)),
                "ts": time.time()}


class Kraken(_Driver):
    """Kraken public API. OHLC serves only the most recent ~720 bars."""
    name = "kraken"
    BASE = "https://api.kraken.com/0/public"
    HISTORY_BARS = 720

    def candles_1m(self, asset, start_ts, end_ts):
        pid = self.pair(asset)
        if not pid:
            return pd.DataFrame(columns=_COLS)
        data = self._get(f"{self.BASE}/OHLC", {"pair": pid, "interval": 1,
                                               "since": start_ts})
        result = data.get("result", {})
        key = next((k for k in result if k != "last"), None)
        rows = [{"ts": int(c[0]), "open": float(c[1]), "high": float(c[2]),
                 "low": float(c[3]), "close": float(c[4]), "volume": float(c[6])}
                for c in (result.get(key) or [])]
        df = pd.DataFrame(rows, columns=_COLS)
        return df[(df.ts >= start_ts) & (df.ts < end_ts)].sort_values("ts")

    def ticker(self, asset):
        pid = self.pair(asset)
        if not pid:
            return None
        data = self._get(f"{self.BASE}/Ticker", {"pair": pid}).get("result", {})
        key = next(iter(data), None)
        if not key:
            return None
        t = data[key]
        return {"price": float(t["c"][0]), "volume_24h": float(t["v"][1]),
                "ts": time.time()}


class Bitstamp(_Driver):
    """Bitstamp public API. 1000 bars/request with explicit start paging."""
    name = "bitstamp"
    BASE = "https://www.bitstamp.net/api/v2"

    def candles_1m(self, asset, start_ts, end_ts):
        pid = self.pair(asset)
        if not pid:
            return pd.DataFrame(columns=_COLS)
        rows = []
        cur = start_ts
        while cur < end_ts:
            data = self._get(f"{self.BASE}/ohlc/{pid}/", {
                "step": 60, "limit": 1000, "start": cur})
            batch = (data.get("data") or {}).get("ohlc") or []
            if not batch:
                break
            rows.extend({"ts": int(c["timestamp"]), "open": float(c["open"]),
                         "high": float(c["high"]), "low": float(c["low"]),
                         "close": float(c["close"]), "volume": float(c["volume"])}
                        for c in batch)
            last = int(batch[-1]["timestamp"])
            if last <= cur:
                break
            cur = last + 60
        df = pd.DataFrame(rows, columns=_COLS)
        return df[df.ts < end_ts].sort_values("ts").drop_duplicates("ts")

    def ticker(self, asset):
        pid = self.pair(asset)
        if not pid:
            return None
        t = self._get(f"{self.BASE}/ticker/{pid}/")
        return {"price": float(t["last"]), "volume_24h": float(t.get("volume", 0.0)),
                "ts": time.time()}


DRIVERS: dict[str, type[_Driver]] = {"coinbase": Coinbase, "kraken": Kraken,
                                     "bitstamp": Bitstamp}


def make_drivers(names: list[str] | None = None) -> list[_Driver]:
    return [DRIVERS[n]() for n in (names or list(DRIVERS))]
