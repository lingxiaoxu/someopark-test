"""WS tape recorder (Plan 08 §3.1 / §4-5). Persists every message, per ticker
per channel, into daily jsonl files; yesterday's file is gzipped at UTC-midnight
rotation. Raw tape is immutable — cleaning happens at load time, never here.

Layout:
    price_data/kalshi/perps/ws/<env>/<channel>/<TICKER>/<YYYY-MM-DD>.jsonl[.gz]
    price_data/kalshi/perps/ws/<env>/_system/<YYYY-MM-DD>.jsonl        (acks/errors)
    price_data/kalshi/perps/ws/<env>/heartbeat.json                    (data health)

Each line: {"recv_ts": <epoch float>, "seq": <int|null>, "msg": <raw message>}.
Sequence gaps per market are tracked via BookMirror and surface in heartbeat.

CLI (demo works TODAY with the borrowed PM key — read-only):
    conda run -n someopark_run python -m crypto_trading.crypto_common.kalshi.recorder \
        [--env demo] [--tickers KXBTCPERP,…] [--duration 0]
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import logging
import shutil
import time
from pathlib import Path

from crypto_trading.crypto_common.config import ACTIVE_PERPS_SNAPSHOT, PRICE_DATA, kalshi_env
from crypto_trading.crypto_common.kalshi.book import BookMirror
from crypto_trading.crypto_common.kalshi.enums import WS_PERP_CHANNELS
from crypto_trading.crypto_common.kalshi.ws import KalshiWS
from crypto_trading.crypto_common.timeutils import utc_day

logger = logging.getLogger(__name__)

HEARTBEAT_EVERY_S = 30.0


class Recorder:
    def __init__(self, *, env: str | None = None, tickers: list[str] | None = None,
                 channels: list[str] | None = None):
        self.env = env or kalshi_env()
        self.tickers = tickers or list(ACTIVE_PERPS_SNAPSHOT)
        self.channels = channels or list(WS_PERP_CHANNELS)
        self.root = PRICE_DATA / "kalshi" / "perps" / "ws" / self.env
        self._files: dict[tuple[str, str], tuple[str, object]] = {}  # (chan,tick) -> (day, fh)
        self.books: dict[str, BookMirror] = {t: BookMirror(t) for t in self.tickers}
        self._last_msg_ts: dict[str, float] = {}
        self._counts: dict[str, int] = {}
        self._last_heartbeat = 0.0

    # ── file plumbing ──────────────────────────────────────────────────────
    def _fh(self, channel: str, ticker: str):
        key = (channel, ticker)
        day = utc_day()
        cur = self._files.get(key)
        if cur and cur[0] == day:
            return cur[1]
        if cur:  # UTC midnight passed — close + gzip the finished day
            cur[1].close()
            old = self.root / channel / ticker / f"{cur[0]}.jsonl"
            self._gzip(old)
        d = self.root / channel / ticker
        d.mkdir(parents=True, exist_ok=True)
        fh = open(d / f"{day}.jsonl", "a", buffering=1)  # line-buffered: crash-safe-ish
        self._files[key] = (day, fh)
        return fh

    @staticmethod
    def _gzip(path: Path) -> None:
        if not path.exists():
            return
        with open(path, "rb") as src, gzip.open(str(path) + ".gz", "wb") as dst:
            shutil.copyfileobj(src, dst)
        path.unlink()

    # ── message handling ───────────────────────────────────────────────────
    async def on_message(self, message: dict) -> None:
        mtype = message.get("type", "")
        msg = message.get("msg") or {}
        ticker = msg.get("market_ticker") or ""
        seq = message.get("seq")
        now = time.time()

        if mtype in ("orderbook_snapshot", "orderbook_delta") and ticker in self.books:
            book = self.books[ticker]
            if mtype == "orderbook_snapshot":
                book.apply_snapshot(msg, seq)
            else:
                book.apply_delta(msg, seq)

        channel = {"orderbook_snapshot": "orderbook_delta"}.get(mtype, mtype)
        if ticker and channel in self.channels:
            fh = self._fh(channel, ticker)
        else:
            fh = self._fh("_system", "")
        fh.write(json.dumps({"recv_ts": now, "seq": seq, "msg": message},
                            separators=(",", ":")) + "\n")
        self._counts[channel or "_system"] = self._counts.get(channel or "_system", 0) + 1
        if ticker:
            self._last_msg_ts[ticker] = now
        if now - self._last_heartbeat > HEARTBEAT_EVERY_S:
            self.write_heartbeat()

    def write_heartbeat(self) -> None:
        self._last_heartbeat = time.time()
        self.root.mkdir(parents=True, exist_ok=True)
        hb = {
            "ts": self._last_heartbeat,
            "env": self.env,
            "counts": self._counts,
            "last_msg_ts": self._last_msg_ts,
            "book_gaps": {t: b.gaps for t, b in self.books.items() if b.gaps},
            "books_synced": {t: b.synced for t, b in self.books.items()},
        }
        (self.root / "heartbeat.json").write_text(json.dumps(hb, indent=1))

    def close(self) -> None:
        self.write_heartbeat()
        for day, fh in self._files.values():
            fh.close()


async def record(env: str, tickers: list[str], duration: float) -> Recorder:
    rec = Recorder(env=env, tickers=tickers)
    ws = KalshiWS(rec.channels, rec.tickers, env=env, on_message=rec.on_message)
    task = asyncio.create_task(ws.run())
    try:
        if duration > 0:
            await asyncio.sleep(duration)
            ws.stop()
            task.cancel()          # recv() may be mid-await — cancel promptly
            try:
                await task
            except asyncio.CancelledError:
                pass
        else:
            await task
    finally:
        rec.close()
        logger.info("recorder stopped: %s msgs=%s reconnects=%d",
                    env, rec._counts, ws.stats["reconnects"])
    return rec


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--env", default=None, choices=[None, "demo", "prod"])
    ap.add_argument("--tickers", default="")
    ap.add_argument("--duration", type=float, default=0.0,
                    help="seconds to run; 0 = until killed (daemon mode)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()] or None
    asyncio.run(record(args.env or kalshi_env(), tickers or list(ACTIVE_PERPS_SNAPSHOT),
                       args.duration))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
