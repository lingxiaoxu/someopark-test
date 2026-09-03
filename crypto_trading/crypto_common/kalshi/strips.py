"""Event strike-strip recorder (Plan 02 milestone 1 — "start now").

Captures the raw material for the implied-distribution work: for each
configured threshold series (probe-verified: KXBTCD / KXBTC / KXETHD / KXETH,
strike_type='greater', floor_strike=$level), snapshot the open markets at the
nearest horizons and the orderbooks of strikes near ATM.

ATM reference = perp mark / contract_size (margin API, public). All public —
no key needed. Raw payloads persisted as-is (parse at load time).

Layout:
    price_data/kalshi/event_strips/<env>/<SERIES>/markets/<date>.jsonl
    price_data/kalshi/event_strips/<env>/<SERIES>/orderbook/<date>.jsonl
    price_data/kalshi/event_strips/<env>/heartbeat.json

CLI:
    conda run -n someopark_run python -m crypto_trading.crypto_common.kalshi.strips \
        [--env prod] [--interval 60] [--horizons 2] [--atm-window 0.15] [--cycles 0]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from collections import defaultdict

from crypto_trading.crypto_common.config import PRICE_DATA
from crypto_trading.crypto_common.io_jsonl import DailyJsonlWriter
from crypto_trading.crypto_common.kalshi.rest_event import KalshiEventClient
from crypto_trading.crypto_common.kalshi.rest_margin import KalshiMarginClient

logger = logging.getLogger(__name__)

# series -> perp ticker used for the ATM reference
DEFAULT_SERIES = {
    "KXBTCD": "KXBTCPERP", "KXBTC": "KXBTCPERP",
    "KXETHD": "KXETHPERP", "KXETH": "KXETHPERP",
    # added 2026-08-26 for W7 sample expansion (add-only: existing series and
    # their files are untouched — the standing rule is never lose recordings)
    "KXSOLD": "KXSOLPERP",
    # 15-MINUTE UP/DOWN (added 2026-08-27): the structural twin of the
    # Polymarket instrument the reverse-engineered account traded — one
    # contract per window, strike = the window's opening price. Kalshi's API
    # only serves ~10 days of settled history, so recording starts the clock
    # on everything beyond that (candlesticks cover the past, tape covers the
    # future — and only the tape carries L2 depth).
    "KXBTC15M": "KXBTCPERP", "KXETH15M": "KXETHPERP",
    "KXSOL15M": "KXSOLPERP", "KXDOGE15M": "KXDOGEPERP",
    "KXXRP15M": "KXXRPPERP",
}


def spot_estimate(margin: KalshiMarginClient, perp_ticker: str) -> float | None:
    """Underlying-level estimate = perp mark / contract_size (decimal dollars)."""
    m = margin.market(perp_ticker)
    try:
        price, size = float(m.get("price", 0)), float(m.get("contract_size", 0))
        return price / size if price > 0 and size > 0 else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


class StripRecorder:
    def __init__(self, *, env: str = "prod", series: dict[str, str] | None = None,
                 horizons: int = 2, atm_window: float = 0.15, interval: float = 60.0):
        self.env = env
        self.series = dict(series or DEFAULT_SERIES)
        self.horizons = horizons
        self.atm_window = atm_window
        self.interval = interval
        self.event = KalshiEventClient(env=env)
        self.margin = KalshiMarginClient(env=env, min_interval=0.15)
        self.root = PRICE_DATA / "kalshi" / "event_strips" / env
        self.writer = DailyJsonlWriter(self.root)
        self.counts = {"markets": 0, "orderbooks": 0, "errors": 0}
        self._locks: list = []
        # one heartbeat per instance: a shared file let the second recorder
        # overwrite the first's series list, which is what made the overlap
        # above invisible for six days
        self.hb_suffix = "" if set(self.series) == set(DEFAULT_SERIES) else \
            "_" + "-".join(sorted(self.series))[:40]

    def capture_series(self, series_ticker: str, perp_ticker: str) -> None:
        now = time.time()
        spot = spot_estimate(self.margin, perp_ticker)
        markets = self.event.list_markets(series_ticker=series_ticker, status="open")
        by_close: dict[str, list[dict]] = defaultdict(list)
        for m in markets:
            by_close[m.get("close_time", "")].append(m)
        horizons = sorted(by_close)[: self.horizons]

        for close_time in horizons:
            strip = by_close[close_time]
            self.writer.write(f"{series_ticker}/markets", {
                "recv_ts": now, "close_time": close_time, "spot_est": spot,
                "n_markets": len(strip), "markets": strip,
            })
            self.counts["markets"] += len(strip)
            for m in strip:
                strike = m.get("floor_strike") or m.get("cap_strike")
                if spot and strike and abs(float(strike) / spot - 1.0) > self.atm_window:
                    continue                      # only book-snapshot strikes near ATM
                ob = self.event.orderbook_raw(m["ticker"])
                self.writer.write(f"{series_ticker}/orderbook", {
                    "recv_ts": now, "ticker": m["ticker"], "close_time": close_time,
                    "strike": strike, "spot_est": spot, "ob": ob,
                })
                self.counts["orderbooks"] += 1

    def heartbeat(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / f"heartbeat{self.hb_suffix}.json").write_text(json.dumps(
            {"ts": time.time(), "env": self.env, "series": list(self.series),
             "pid": os.getpid(), "counts": self.counts}, indent=1))

    def claim_series(self) -> None:
        """Refuse to start if another live process already records these series.

        DEFAULT_SERIES grew to include the five 15M series on 2026-08-27, which
        made the no-``--series`` invocation overlap the dedicated 15M instance.
        Today they do not collide only because the older process was started
        before that edit and holds the pre-15M dict in memory: ANY restart —
        crash, reboot, or `make_launchd.sh install` — would put two writers on
        one file, double every quote W7 reads, and at 00:00 UTC hand the gzip
        rotation a file another process is still appending to. That would
        destroy a day of tape that cannot be re-fetched (Kalshi serves ~10 days
        of settled history and never L2 depth), so this fails loudly instead.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        held = []
        for st in self.series:
            lock = self.root / st / ".recorder.lock"
            lock.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    other = int(lock.read_text().split()[0])
                    os.kill(other, 0)              # signal 0 = liveness probe
                except (ValueError, IndexError, ProcessLookupError, OSError):
                    lock.unlink(missing_ok=True)   # stale lock from a dead pid
                    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                else:
                    held.append(f"{st} (pid {other})")
                    continue
            with os.fdopen(fd, "w") as fh:
                fh.write(f"{os.getpid()} {time.time():.0f}\n")
            self._locks.append(lock)
        if held:
            self.release_series()
            raise SystemExit(
                "strips: these series are already being recorded by a live "
                f"process: {', '.join(held)}. Two writers on one tape file "
                "corrupt it at the UTC-midnight rotation. Pass --series to "
                "split the work, or stop the other instance first.")

    def release_series(self) -> None:
        for lock in self._locks:
            lock.unlink(missing_ok=True)
        self._locks = []

    def run(self, cycles: int = 0) -> None:
        n = 0
        self.claim_series()
        try:
            while True:
                started = time.time()
                for st, perp in self.series.items():
                    try:
                        self.capture_series(st, perp)
                    except Exception:
                        self.counts["errors"] += 1
                        logger.exception("strip capture failed for %s — continuing", st)
                self.heartbeat()
                n += 1
                if cycles and n >= cycles:
                    break
                time.sleep(max(0.0, self.interval - (time.time() - started)))
        finally:
            self.release_series()
            self.writer.close()
            logger.info("strip recorder stopped after %d cycles: %s", n, self.counts)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--env", default="prod", choices=["prod", "demo"])
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--horizons", type=int, default=2)
    ap.add_argument("--atm-window", type=float, default=0.15)
    ap.add_argument("--cycles", type=int, default=0, help="0 = run until killed")
    ap.add_argument("--series", default=None,
                    help="comma-separated series subset (default: all configured). "
                         "Lets a second recorder cover newly-added series without "
                         "restarting the long-running one — recordings are never "
                         "interrupted, and each series owns its own directory so "
                         "two instances cannot collide.")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    series = None
    if args.series:
        keys = [x.strip().upper() for x in args.series.split(",") if x.strip()]
        missing = [k for k in keys if k not in DEFAULT_SERIES]
        if missing:
            raise SystemExit(f"unknown series: {missing}; known: {sorted(DEFAULT_SERIES)}")
        series = {k: DEFAULT_SERIES[k] for k in keys}
    StripRecorder(env=args.env, horizons=args.horizons, atm_window=args.atm_window,
                  interval=args.interval, series=series).run(args.cycles)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
