"""Offshore derivatives PROXY data (Plan 00 §2 `refdata/derivs.py`, Plan 08 §3.3).

Purpose: long-history mechanism-prototyping panels (funding, klines, OI) and a
forward liquidation tape. Everything here is labeled ``source=proxy`` — it is
NEVER the validation gate (Plan 00 isolation of proxy vs Kalshi-native data).

Drivers (keyless public), tried in reachability order — probe result from this
box (US IP, 2026-07-07): Binance REST 451-blocked, Bybit REST blocked,
**OKX + Kraken Futures reachable** (Hyperliquid/dYdX also 200 — future options):
  * OKX — /api/v5 public (candles, funding-rate-history) + public WS
    ``liquidation-orders`` channel. PRIMARY fallback: one venue covers klines,
    funding, AND the Plan 04 liquidation tape.
  * KrakenFutures — charts API + historical funding rates. Secondary.
  * BinanceFutures — deepest history when reachable (non-US egress).
  * Bybit — same coverage as Binance; also geo-blocked from US.

Layout:
    price_data/offshore/<driver>/klines_1h/<SYMBOL>.parquet
    price_data/offshore/<driver>/funding/<SYMBOL>.parquet
    price_data/offshore/<driver>/liquidations/<SYMBOL>/<date>.jsonl   (record-forward)

CLI:
    … -m crypto_trading.crypto_common.refdata.derivs backfill [--symbols BTCUSDT,ETHUSDT] [--days 730]
    … -m crypto_trading.crypto_common.refdata.derivs liq-record [--symbols BTCUSDT,ETHUSDT]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time

import pandas as pd
import requests

from crypto_trading.crypto_common.config import PRICE_DATA
from crypto_trading.crypto_common.io_jsonl import DailyJsonlWriter

logger = logging.getLogger(__name__)

OFFSHORE_DIR = PRICE_DATA / "offshore"
DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT")


class BinanceFutures:
    name = "binance"
    BASE = "https://fapi.binance.com"

    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout
        self._s = requests.Session()
        self._s.headers["User-Agent"] = "someopark-crypto/0.1"

    def available(self) -> bool:
        try:
            r = self._s.get(f"{self.BASE}/fapi/v1/ping", timeout=8)
            return r.status_code == 200
        except Exception:
            return False

    def klines(self, symbol: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        rows = []
        cur = start_ms
        while cur < end_ms:
            data = self._s.get(f"{self.BASE}/fapi/v1/klines", params={
                "symbol": symbol, "interval": interval, "startTime": cur,
                "endTime": end_ms, "limit": 1500}, timeout=self.timeout).json()
            if not isinstance(data, list) or not data:
                break
            rows.extend({"ts": int(k[0] // 1000), "open": float(k[1]), "high": float(k[2]),
                         "low": float(k[3]), "close": float(k[4]), "volume": float(k[5])}
                        for k in data)
            cur = int(data[-1][0]) + 1
            time.sleep(0.25)
        return pd.DataFrame(rows).drop_duplicates("ts") if rows else pd.DataFrame()

    def funding(self, symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        rows = []
        cur = start_ms
        while cur < end_ms:
            data = self._s.get(f"{self.BASE}/fapi/v1/fundingRate", params={
                "symbol": symbol, "startTime": cur, "endTime": end_ms,
                "limit": 1000}, timeout=self.timeout).json()
            if not isinstance(data, list) or not data:
                break
            rows.extend({"funding_time": int(d["fundingTime"] // 1000),
                         "funding_rate": float(d["fundingRate"]),
                         "mark_price": float(d.get("markPrice") or 0) or None}
                        for d in data)
            cur = int(data[-1]["fundingTime"]) + 1
            time.sleep(0.25)
        return pd.DataFrame(rows).drop_duplicates("funding_time") if rows else pd.DataFrame()

    def liq_ws_url(self, symbols: list[str]) -> str:
        streams = "/".join(f"{s.lower()}@forceOrder" for s in symbols)
        return f"wss://fstream.binance.com/stream?streams={streams}"

    @staticmethod
    def parse_liq(raw: dict) -> dict | None:
        o = (raw.get("data") or raw).get("o") or {}
        if not o:
            return None
        return {"symbol": o.get("s"), "side": o.get("S"), "price": o.get("ap") or o.get("p"),
                "qty": o.get("q"), "ts_ms": o.get("T")}


class Bybit:
    name = "bybit"
    BASE = "https://api.bybit.com"
    _INTERVALS = {"1m": "1", "1h": "60", "1d": "D"}

    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout
        self._s = requests.Session()
        self._s.headers["User-Agent"] = "someopark-crypto/0.1"

    def available(self) -> bool:
        try:
            r = self._s.get(f"{self.BASE}/v5/market/time", timeout=8)
            return r.status_code == 200
        except Exception:
            return False

    def klines(self, symbol: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        iv = self._INTERVALS[interval] if interval in self._INTERVALS else interval
        rows = []
        cur = start_ms
        while cur < end_ms:
            data = self._s.get(f"{self.BASE}/v5/market/kline", params={
                "category": "linear", "symbol": symbol, "interval": iv,
                "start": cur, "end": end_ms, "limit": 1000},
                timeout=self.timeout).json()
            batch = ((data.get("result") or {}).get("list")) or []
            if not batch:
                break
            batch = sorted(batch, key=lambda k: int(k[0]))       # bybit is newest-first
            rows.extend({"ts": int(int(k[0]) // 1000), "open": float(k[1]),
                         "high": float(k[2]), "low": float(k[3]), "close": float(k[4]),
                         "volume": float(k[5])} for k in batch)
            nxt = int(batch[-1][0]) + 1
            if nxt <= cur:
                break
            cur = nxt
            time.sleep(0.25)
        return pd.DataFrame(rows).drop_duplicates("ts") if rows else pd.DataFrame()

    def funding(self, symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        rows = []
        end_cursor = end_ms
        for _ in range(400):                     # pages backward in time
            data = self._s.get(f"{self.BASE}/v5/market/funding/history", params={
                "category": "linear", "symbol": symbol, "startTime": start_ms,
                "endTime": end_cursor, "limit": 200}, timeout=self.timeout).json()
            batch = ((data.get("result") or {}).get("list")) or []
            if not batch:
                break
            rows.extend({"funding_time": int(int(d["fundingRateTimestamp"]) // 1000),
                         "funding_rate": float(d["fundingRate"]), "mark_price": None}
                        for d in batch)
            oldest = min(int(d["fundingRateTimestamp"]) for d in batch)
            if oldest <= start_ms:
                break
            end_cursor = oldest - 1
            time.sleep(0.25)
        return pd.DataFrame(rows).drop_duplicates("funding_time") if rows else pd.DataFrame()

    def liq_ws_url(self, symbols: list[str]) -> str:
        return "wss://stream.bybit.com/v5/public/linear"

    def liq_subscribe_msg(self, symbols: list[str]) -> dict:
        return {"op": "subscribe", "args": [f"allLiquidation.{s}" for s in symbols]}

    @staticmethod
    def parse_liq(raw: dict) -> list[dict]:
        out = []
        for d in raw.get("data") or []:
            out.append({"symbol": d.get("s"), "side": d.get("S"), "price": d.get("p"),
                        "qty": d.get("v"), "ts_ms": d.get("T")})
        return out


class OKX:
    """OKX v5 public data. US-reachable (probe 2026-07-07). Symbols: BTC-USDT-SWAP."""
    name = "okx"
    BASE = "https://www.okx.com"
    WS_PUBLIC = "wss://ws.okx.com:8443/ws/v5/public"
    _INTERVALS = {"1m": "1m", "1h": "1H", "1d": "1D"}

    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout
        self._s = requests.Session()
        self._s.headers["User-Agent"] = "someopark-crypto/0.1"

    @staticmethod
    def sym(symbol: str) -> str:
        """BTCUSDT → BTC-USDT-SWAP."""
        base = symbol.upper().removesuffix("USDT")
        return f"{base}-USDT-SWAP"

    def available(self) -> bool:
        try:
            return self._s.get(f"{self.BASE}/api/v5/public/time", timeout=8).status_code == 200
        except Exception:
            return False

    def klines(self, symbol: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        inst = self.sym(symbol)
        bar = self._INTERVALS.get(interval, interval)
        rows = []
        # history-candles pages backward via `after` (returns < after), 100/req
        after = end_ms
        for _ in range(4000):
            data = self._s.get(f"{self.BASE}/api/v5/market/history-candles", params={
                "instId": inst, "bar": bar, "after": after, "limit": 100},
                timeout=self.timeout).json()
            batch = data.get("data") or []
            if not batch:
                break
            rows.extend({"ts": int(int(k[0]) // 1000), "open": float(k[1]),
                         "high": float(k[2]), "low": float(k[3]), "close": float(k[4]),
                         "volume": float(k[5])} for k in batch)
            oldest = min(int(k[0]) for k in batch)
            if oldest <= start_ms:
                break
            after = oldest
            time.sleep(0.15)
        df = pd.DataFrame(rows).drop_duplicates("ts") if rows else pd.DataFrame()
        if len(df):
            df = df[(df.ts >= start_ms // 1000) & (df.ts <= end_ms // 1000)]
        return df

    def funding(self, symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        inst = self.sym(symbol)
        rows = []
        after = end_ms
        for _ in range(1000):
            data = self._s.get(f"{self.BASE}/api/v5/public/funding-rate-history", params={
                "instId": inst, "after": after, "limit": 100}, timeout=self.timeout).json()
            batch = data.get("data") or []
            if not batch:
                break
            rows.extend({"funding_time": int(int(d["fundingTime"]) // 1000),
                         "funding_rate": float(d["fundingRate"]), "mark_price": None}
                        for d in batch)
            oldest = min(int(d["fundingTime"]) for d in batch)
            if oldest <= start_ms:
                break
            after = oldest
            time.sleep(0.15)
        df = pd.DataFrame(rows).drop_duplicates("funding_time") if rows else pd.DataFrame()
        if len(df):
            df = df[(df.funding_time >= start_ms // 1000) & (df.funding_time <= end_ms // 1000)]
        return df

    def liq_ws_url(self, symbols: list[str]) -> str:
        return self.WS_PUBLIC

    def liq_subscribe_msg(self, symbols: list[str]) -> dict:
        # one channel covers all SWAP instruments; we filter client-side
        return {"op": "subscribe",
                "args": [{"channel": "liquidation-orders", "instType": "SWAP"}]}

    @staticmethod
    def parse_liq(raw: dict) -> list[dict]:
        out = []
        for d in raw.get("data") or []:
            inst = d.get("instId", "")
            for det in d.get("details") or []:
                out.append({"symbol": inst.replace("-USDT-SWAP", "USDT"),
                            "side": det.get("side"), "price": det.get("bkPx"),
                            "qty": det.get("sz"), "ts_ms": int(det.get("ts") or 0)})
        return out


class KrakenFutures:
    """Kraken Futures public data. US-reachable. Symbols: PF_XBTUSD."""
    name = "krakenfutures"
    BASE = "https://futures.kraken.com"
    _INTERVALS = {"1m": "1m", "1h": "1h", "1d": "1d"}

    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout
        self._s = requests.Session()
        self._s.headers["User-Agent"] = "someopark-crypto/0.1"

    @staticmethod
    def sym(symbol: str) -> str:
        """BTCUSDT → PF_XBTUSD (Kraken calls BTC 'XBT')."""
        base = symbol.upper().removesuffix("USDT")
        return f"PF_{'XBT' if base == 'BTC' else base}USD"

    def available(self) -> bool:
        try:
            return self._s.get(f"{self.BASE}/derivatives/api/v3/tickers",
                               timeout=8).status_code == 200
        except Exception:
            return False

    def klines(self, symbol: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        inst = self.sym(symbol)
        res = self._INTERVALS.get(interval, interval)
        rows = []
        cur = start_ms // 1000
        end_s = end_ms // 1000
        while cur < end_s:
            data = self._s.get(
                f"{self.BASE}/api/charts/v1/trade/{inst}/{res}",
                params={"from": cur, "to": end_s}, timeout=self.timeout).json()
            batch = data.get("candles") or []
            if not batch:
                break
            rows.extend({"ts": int(int(c["time"]) // 1000), "open": float(c["open"]),
                         "high": float(c["high"]), "low": float(c["low"]),
                         "close": float(c["close"]), "volume": float(c["volume"])}
                        for c in batch)
            newest = max(int(c["time"]) // 1000 for c in batch)
            if newest <= cur:
                break
            cur = newest + 1
            time.sleep(0.15)
        return pd.DataFrame(rows).drop_duplicates("ts") if rows else pd.DataFrame()

    def funding(self, symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        inst = self.sym(symbol)
        data = self._s.get(f"{self.BASE}/derivatives/api/v4/historicalfundingrates",
                           params={"symbol": inst}, timeout=self.timeout).json()
        rows = [{"funding_time": int(pd.Timestamp(d["timestamp"]).timestamp()),
                 "funding_rate": float(d.get("relativeFundingRate") or 0), "mark_price": None}
                for d in (data.get("rates") or [])]
        df = pd.DataFrame(rows).drop_duplicates("funding_time") if rows else pd.DataFrame()
        if len(df):
            df = df[(df.funding_time >= start_ms // 1000) & (df.funding_time <= end_ms // 1000)]
        return df


def pick_driver():
    """First reachable source; order favors US-reachable one-stop coverage."""
    for cls in (OKX, KrakenFutures, BinanceFutures, Bybit):
        drv = cls()
        if drv.available():
            if cls is not OKX:
                logger.info("offshore proxy: using %s", drv.name)
            return drv
    raise RuntimeError(
        "no offshore proxy source reachable (tried OKX, KrakenFutures, Binance, Bybit)")


def backfill(symbols: list[str], days: int) -> None:
    drv = pick_driver()
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86400_000
    for sym in symbols:
        for interval, sub in (("1h", "klines_1h"), ("1d", "klines_1d")):
            out = OFFSHORE_DIR / drv.name / sub / f"{sym}.parquet"
            out.parent.mkdir(parents=True, exist_ok=True)
            df = drv.klines(sym, interval, start_ms, end_ms)
            if len(df):
                df["source"] = f"proxy:{drv.name}"
                df.sort_values("ts").to_parquet(out, index=False)
            logger.info("%s %s %s: %d bars", drv.name, sym, interval, len(df))
        out = OFFSHORE_DIR / drv.name / "funding" / f"{sym}.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        fdf = drv.funding(sym, start_ms, end_ms)
        if len(fdf):
            fdf["source"] = f"proxy:{drv.name}"
            fdf.sort_values("funding_time").to_parquet(out, index=False)
        logger.info("%s %s funding: %d cycles", drv.name, sym, len(fdf))


async def _okx_keepalive(ws, interval: float = 20.0) -> None:
    """OKX closes idle sockets ~30s — send an app-level 'ping' text frame
    (OKX replies 'pong'). websockets' protocol pings alone weren't keeping the
    connection up (dropped ~every 40min), so this is the reliable keepalive."""
    while True:
        await asyncio.sleep(interval)
        try:
            await ws.send("ping")
        except Exception:
            return


def _has_subscribe(drv) -> bool:
    """Bybit AND OKX subscribe over the socket; Binance embeds streams in the URL."""
    return hasattr(drv, "liq_subscribe_msg")


async def record_liquidations(symbols: list[str]) -> None:
    """Record-forward liquidation tape (Plan 04 proxy prototyping input).

    Records ALL market-wide liquidations from the venue (useful cascade context,
    not just our symbols); parse_liq tags each with its symbol. WS-verified
    2026-07-10: OKX pushes on every liquidation once SUBSCRIBED — the earlier
    zero-capture was a missing subscribe for OKX (only Bybit was sent).
    """
    import websockets
    drv = pick_driver()
    writer = DailyJsonlWriter(OFFSHORE_DIR / drv.name / "liquidations")
    url = drv.liq_ws_url(symbols)
    while True:
        keepalive = None
        try:
            async with websockets.connect(url, open_timeout=15, ping_interval=20,
                                          ping_timeout=15) as ws:
                if _has_subscribe(drv):
                    await ws.send(json.dumps(drv.liq_subscribe_msg(symbols)))
                if isinstance(drv, OKX):
                    keepalive = asyncio.create_task(_okx_keepalive(ws))
                logger.info("liq stream connected (%s), subscribed=%s", drv.name,
                            _has_subscribe(drv))
                n = 0
                while True:
                    msg = await ws.recv()
                    if msg == "pong" or (isinstance(msg, str) and msg.startswith("pong")):
                        continue
                    raw = json.loads(msg)
                    if raw.get("event"):          # subscribe ack / error frame
                        if raw.get("event") == "error":
                            logger.warning("liq subscribe error: %s", str(raw)[:200])
                        continue
                    parsed = drv.parse_liq(raw)
                    events = parsed if isinstance(parsed, list) else ([parsed] if parsed else [])
                    for ev in events:
                        if ev and ev.get("symbol"):
                            writer.write(ev["symbol"], {"recv_ts": time.time(), **ev})
                            n += 1
                    if n and n % 50 == 0:
                        logger.info("liq events recorded: %d", n)
        except asyncio.CancelledError:
            if keepalive:
                keepalive.cancel()
            writer.close()
            raise
        except Exception as e:
            logger.warning("liq stream dropped: %s: %s — reconnecting", type(e).__name__,
                           str(e)[:150])
            if keepalive:
                keepalive.cancel()
            await asyncio.sleep(5)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", choices=["backfill", "liq-record"])
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--days", type=int, default=730)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if args.mode == "backfill":
        backfill(symbols, args.days)
    else:
        asyncio.run(record_liquidations(symbols))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
