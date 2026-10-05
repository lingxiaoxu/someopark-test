"""Deribit options IV recorder — DVOL index + nearest-expiry ATM IV.

Purpose (user approval 2026-09-30): archive the ONE information family the
loss-prediction research has not been able to test — option-implied
volatility — so any future regime study has point-in-time history. Deribit
does not serve historical option tickers publicly, so this stream is
UNBACKFILLABLE by construction: a lost day is gone for good (the DVOL candles
alone are re-fetchable). Recording starts now; no model consumes it yet.

Layers, each row stamped with `recv_ts` (receipt time, the causal clock):

  offshore/deribit/dvol/{CUR}/YYYY-MM-DD.jsonl
      30-day vol index close of the last minute candle (BTC/ETH only).
  offshore/deribit/atm_iv/{CUR}/YYYY-MM-DD.jsonl
      For the two nearest unexpired option expiries: the three strikes
      closest to the index, CALL ticker mark/bid/ask IV, underlying, OI.

Read-only public API, no key. Currencies without listed options (some alts)
are probed once per instruments refresh and skipped quietly.

    conda run -n someopark_run python -m crypto_trading.crypto_common.refdata.deribit \
        [--interval 60] [--currencies BTC,ETH,SOL,XRP,DOGE] [--once]

Single instance per root via an O_EXCL lock, same as the hyperliquid recorder.
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from pathlib import Path

import requests

from crypto_trading.crypto_common.config import PRICE_DATA
from crypto_trading.crypto_common.io_jsonl import DailyJsonlWriter

logger = logging.getLogger("deribit")

BASE_URL = "https://www.deribit.com/api/v2/public"
DERIBIT_DIR = PRICE_DATA / "offshore" / "deribit"
DEFAULT_CURRENCIES = ("BTC", "ETH", "SOL", "XRP", "DOGE")
DVOL_CURRENCIES = ("BTC", "ETH")           # the only published vol indices
INSTRUMENTS_TTL_S = 1800.0
EXPIRIES_PER_CUR = 2
STRIKES_PER_EXPIRY = 3
PACE_S = 0.12                              # ~8 req/s worst case, well under limits
BACKOFF_MAX_S = 60.0


def pick_atm(instruments: list[dict], index_price: float,
             now_ms: float) -> list[dict]:
    """Nearest EXPIRIES_PER_CUR expiries x STRIKES_PER_EXPIRY strikes around ATM.

    Pure selection logic (unit-tested): calls only — at ATM the mark IV of
    calls and puts coincides and one side halves the request budget.
    """
    calls = [i for i in instruments
             if i.get("option_type") == "call"
             and i.get("expiration_timestamp", 0) > now_ms
             and i.get("strike")]
    expiries = sorted({i["expiration_timestamp"] for i in calls})[:EXPIRIES_PER_CUR]
    picked = []
    for exp in expiries:
        row = sorted((i for i in calls if i["expiration_timestamp"] == exp),
                     key=lambda i: abs(i["strike"] - index_price))
        picked.extend(row[:STRIKES_PER_EXPIRY])
    return picked


class Client:
    """Paced GET with 429/error backoff. Read-only."""

    def __init__(self):
        self.session = requests.Session()
        self._next_ok = 0.0
        self._backoff = 0.0

    def get(self, path: str, **params):
        now = time.time()
        if now < self._next_ok:
            time.sleep(self._next_ok - now)
        try:
            r = self.session.get(f"{BASE_URL}/{path}", params=params, timeout=10)
        except requests.RequestException as exc:
            self._punish(f"{path}: {exc}")
            return None
        if r.status_code == 429:
            self._punish(f"{path}: 429")
            return None
        self._next_ok = time.time() + PACE_S
        if r.status_code != 200:
            logger.warning("deribit %s -> HTTP %s", path, r.status_code)
            return None
        self._backoff = 0.0
        return (r.json() or {}).get("result")

    def _punish(self, why: str) -> None:
        self._backoff = min(BACKOFF_MAX_S, max(1.0, self._backoff * 2))
        self._next_ok = time.time() + self._backoff
        logger.warning("deribit backoff %.1fs (%s)", self._backoff, why)


class Recorder:
    def __init__(self, currencies=DEFAULT_CURRENCIES, root: Path = DERIBIT_DIR):
        self.currencies = list(currencies)
        self.root = root
        self.client = Client()
        self.writer = DailyJsonlWriter(root)
        self._instruments: dict[str, tuple[float, list[dict]]] = {}
        self._no_options: set[str] = set()
        self._lock: Path | None = None
        self.stats = {"dvol": 0, "atm_iv": 0, "errors": 0}

    # -- single instance ------------------------------------------------------
    def acquire(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        lock = self.root / ".recorder.lock"
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            other = int(lock.read_text().split()[0])
            try:
                os.kill(other, 0)
                raise SystemExit(f"deribit recorder already running (pid {other})")
            except ProcessLookupError:
                lock.unlink(missing_ok=True)       # stale lock from a dead pid
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{os.getpid()} {time.time():.0f}".encode())
        os.close(fd)
        self._lock = lock

    def release(self) -> None:
        if self._lock:
            self._lock.unlink(missing_ok=True)

    # -- layers ---------------------------------------------------------------
    def dvol(self) -> None:
        end = int(time.time() * 1000)
        for cur in DVOL_CURRENCIES:
            if cur not in self.currencies:
                continue
            res = self.client.get("get_volatility_index_data", currency=cur,
                                  start_timestamp=end - 180_000,
                                  end_timestamp=end, resolution="60")
            recv = time.time()
            candles = (res or {}).get("data") or []
            if not candles:
                self.stats["errors"] += 1
                continue
            ts, _o, _h, _l, close = candles[-1][:5]
            self.writer.write(f"dvol/{cur}", dict(
                recv_ts=recv, coin=cur, dvol=close, candle_ts=ts / 1000.0))
            self.stats["dvol"] += 1

    def _index(self, cur: str) -> float | None:
        for name in (f"{cur.lower()}_usd", f"{cur.lower()}_usdc"):
            res = self.client.get("get_index_price", index_name=name)
            if res and res.get("index_price"):
                return float(res["index_price"])
        return None

    def _instruments_for(self, cur: str) -> list[dict]:
        cached = self._instruments.get(cur)
        if cached and time.time() - cached[0] < INSTRUMENTS_TTL_S:
            return cached[1]
        res = self.client.get("get_instruments", currency=cur, kind="option",
                              expired="false")
        rows = res if isinstance(res, list) else []
        if not rows and cur not in self._no_options:
            self._no_options.add(cur)
            logger.info("deribit: no listed options for %s; skipping layer", cur)
        self._instruments[cur] = (time.time(), rows)
        return rows

    def atm_iv(self) -> None:
        for cur in self.currencies:
            instruments = self._instruments_for(cur)
            if not instruments:
                continue
            index_price = self._index(cur)
            if not index_price:
                self.stats["errors"] += 1
                continue
            for inst in pick_atm(instruments, index_price, time.time() * 1000):
                tick = self.client.get("ticker",
                                       instrument_name=inst["instrument_name"])
                recv = time.time()
                if not tick:
                    self.stats["errors"] += 1
                    continue
                self.writer.write(f"atm_iv/{cur}", dict(
                    recv_ts=recv, coin=cur,
                    instrument=inst["instrument_name"],
                    expiry_ts=inst["expiration_timestamp"] / 1000.0,
                    dte_h=round((inst["expiration_timestamp"] / 1000.0 - recv)
                                / 3600.0, 2),
                    strike=inst["strike"],
                    mark_iv=tick.get("mark_iv"), bid_iv=tick.get("bid_iv"),
                    ask_iv=tick.get("ask_iv"),
                    underlying=tick.get("underlying_price") or index_price,
                    oi=tick.get("open_interest")))
                self.stats["atm_iv"] += 1

    # -- loop -----------------------------------------------------------------
    def cycle(self) -> None:
        self.dvol()
        self.atm_iv()

    def run(self, interval: float, once: bool = False) -> None:
        self.acquire()
        try:
            n = 0
            while True:
                started = time.time()
                try:
                    self.cycle()
                except Exception:                                 # noqa: BLE001
                    self.stats["errors"] += 1
                    logger.exception("deribit cycle failed; continuing")
                n += 1
                if once:
                    break
                if n % 10 == 0:
                    logger.info("deribit heartbeat: %s", self.stats)
                time.sleep(max(1.0, interval - (time.time() - started)))
        except KeyboardInterrupt:
            pass
        finally:
            self.release()
            logger.info("deribit recorder stopped: %s", self.stats)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--currencies", default=",".join(DEFAULT_CURRENCIES))
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args(argv)
    Recorder(currencies=[c.strip().upper() for c in
                         args.currencies.split(",") if c.strip()]).run(
        args.interval, once=args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
