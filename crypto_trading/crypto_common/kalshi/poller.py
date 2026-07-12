"""PROD REST poller — keyless capture of real perp market data (Plan 08 §3.1).

The demo WS tape is synthetic/quiet and prod WS needs a dedicated key (operator
item). This poller closes the gap TODAY: public REST gives us real books,
trades, and per-market stats. Once the prod key exists, the WS recorder runs
alongside and this poller drops to a slow health-check cadence.

Per cycle:
  * /margin/markets              → one line per active ticker  (stats: bid/ask,
    OI, liquidation_mark_price, leverage_estimates — Plan 04/06 inputs)
  * /margin/markets/{t}/orderbook for book_tickers → full-depth book snapshot
  * /margin/trades?ticker={t}    → NEW trades since last seen (id-deduped)

Layout (raw, immutable, daily gzip rotation):
    price_data/kalshi/perps/poll/<env>/markets/<TICKER>/<date>.jsonl
    price_data/kalshi/perps/poll/<env>/orderbook/<TICKER>/<date>.jsonl
    price_data/kalshi/perps/poll/<env>/trades/<TICKER>/<date>.jsonl
    price_data/kalshi/perps/poll/<env>/heartbeat.json

CLI:
    conda run -n someopark_run python -m crypto_trading.crypto_common.kalshi.poller \
        [--env prod] [--interval 10] [--book-tickers KXBTCPERP,KXETHPERP,...] [--cycles 0]
"""
from __future__ import annotations

import argparse
import json
import logging
import time

from crypto_trading.crypto_common.config import ACTIVE_PERPS_SNAPSHOT, PRICE_DATA
from crypto_trading.crypto_common.io_jsonl import DailyJsonlWriter
from crypto_trading.crypto_common.kalshi.rest_margin import KalshiMarginClient

logger = logging.getLogger(__name__)

DEFAULT_BOOK_TICKERS = ("KXBTCPERP", "KXETHPERP", "KXSOLPERP", "KXXRPPERP")


class Poller:
    def __init__(self, *, env: str = "prod", interval: float = 10.0,
                 book_tickers: tuple[str, ...] = DEFAULT_BOOK_TICKERS):
        self.env = env
        self.interval = interval
        self.book_tickers = tuple(book_tickers)
        self.client = KalshiMarginClient(env=env, min_interval=0.15)
        self.root = PRICE_DATA / "kalshi" / "perps" / "poll" / env
        self.writer = DailyJsonlWriter(self.root)
        self._seen_trades: dict[str, set[str]] = {}
        self._trade_watermark: dict[str, str] = {}   # ticker -> newest created_time seen
        self.counts = {"markets": 0, "orderbook": 0, "trades": 0, "errors": 0}

    def poll_markets(self) -> list[str]:
        mkts = self.client.markets()
        now = time.time()
        active = []
        for m in mkts:
            if m.get("status") != "active":
                continue
            active.append(m["ticker"])
            self.writer.write(f"markets/{m['ticker']}", {"recv_ts": now, "m": m})
            self.counts["markets"] += 1
        return active

    def poll_books(self) -> None:
        now = time.time()
        for t in self.book_tickers:
            ob = self.client.orderbook(t)
            self.writer.write(f"orderbook/{t}", {"recv_ts": now, "ob": ob.get("orderbook", ob)})
            self.counts["orderbook"] += 1

    def poll_trades(self) -> None:
        """Append only unseen trades (newest-first API; dedupe by trade_id)."""
        now = time.time()
        for t in self.book_tickers:
            page = self.client.trades(t, limit=100)
            trades = page.get("trades", [])
            seen = self._seen_trades.setdefault(t, set())
            fresh = [tr for tr in trades if tr.get("trade_id") not in seen]
            for tr in reversed(fresh):               # persist oldest-first
                self.writer.write(f"trades/{t}", {"recv_ts": now, "t": tr})
                self.counts["trades"] += 1
            seen.update(tr.get("trade_id") for tr in fresh)
            if len(seen) > 5000:                     # bound memory; keep newest ids
                self._seen_trades[t] = set(list(seen)[-2500:])
            if fresh:
                self._trade_watermark[t] = max(tr.get("created_time", "") for tr in fresh)

    def heartbeat(self) -> None:
        (self.root).mkdir(parents=True, exist_ok=True)
        (self.root / "heartbeat.json").write_text(json.dumps({
            "ts": time.time(), "env": self.env, "interval": self.interval,
            "book_tickers": self.book_tickers, "counts": self.counts,
            "trade_watermark": self._trade_watermark,
        }, indent=1))

    def run(self, cycles: int = 0) -> None:
        n = 0
        try:
            while True:
                started = time.time()
                try:
                    self.poll_markets()
                    self.poll_books()
                    self.poll_trades()
                except Exception:
                    self.counts["errors"] += 1
                    logger.exception("poll cycle failed — continuing")
                self.heartbeat()
                n += 1
                if cycles and n >= cycles:
                    break
                time.sleep(max(0.0, self.interval - (time.time() - started)))
        finally:
            self.writer.close()
            logger.info("poller stopped after %d cycles: %s", n, self.counts)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--env", default="prod", choices=["prod", "demo"])
    ap.add_argument("--interval", type=float, default=10.0)
    ap.add_argument("--book-tickers", default=",".join(DEFAULT_BOOK_TICKERS),
                    help=f"full universe = {','.join(ACTIVE_PERPS_SNAPSHOT)}")
    ap.add_argument("--cycles", type=int, default=0, help="0 = run until killed")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    tickers = tuple(t.strip() for t in args.book_tickers.split(",") if t.strip())
    Poller(env=args.env, interval=args.interval, book_tickers=tickers).run(args.cycles)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
