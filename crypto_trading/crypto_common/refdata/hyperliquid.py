"""Hyperliquid public-state recorder - the alternative-data half of the pair.

This is the second of two matched sets. The first is the composite spot index
(`refdata/index.py`, venues coinbase/kraken/bitstamp); this one records, for the
SAME coin universe, the venue-side state that index cannot see: perp mark and
oracle prices, open interest, funding, premium, the L2 book, every public print
with BOTH counterparty addresses, and - for a causally-built address pool - the
positions and TWAP slice fills behind that flow.

WHY THESE FIELDS. The decision they are meant to inform is W8's: at T-11..T-7
a favourite sits at ~0.73 and the question is whether that lead survives to the
close. Two of the hypotheses in the research brief map onto this directly:

  PlannedFlow  - an in-progress TWAP is future order flow that has not yet
                 printed. `twapStates` is a WEBSOCKET subscription, not an info
                 request (all three REST shapes return 422), so what is
                 recordable here is `userTwapSliceFills`: a running TWAP emits a
                 slice every ~30s, so slices seen in the last few minutes are
                 causal evidence that one is live, with its side and pace. The
                 remaining-quantity refinement needs the WS channel and is not
                 pretended at here.
  LongAtRisk   - exposure that would be FORCED to trade if price moves a little
                 further. Note `liquidationPx` is often null for cross-margin
                 positions because cross liquidation is an ACCOUNT-level event:
                 the honest measure is account value against maintenance margin,
                 then attributed to coins. So this module records the raw
                 marginSummary and every position verbatim and computes nothing
                 - a model baked into a tape can never be revised.

POINT-IN-TIME. Every row carries `recv_ts` (when we received it) alongside the
venue's own timestamp where one exists. Nothing is backfilled: Hyperliquid
serves current state only, which is exactly why this has to start recording
before it can ever be studied. The address pool is accumulated forward from
observed prints - at time t it contains only addresses that had already traded
publicly by t - so it cannot become the "pick today's winners and backtest them"
trap the brief warns about.

Writes under price_data/hyperliquid/ using the same DailyJsonlWriter (daily
rotation, gzip on rollover) as the price recorders, so the two sets line up
file-for-file and coin-for-coin. It never reads or writes any existing
recorder's directory.
"""
import argparse
import json
import logging
import os
import time
from collections import OrderedDict, deque
from pathlib import Path

import requests

from crypto_trading.crypto_common.config import PRICE_DATA
from crypto_trading.crypto_common.io_jsonl import DailyJsonlWriter

logger = logging.getLogger(__name__)

HL_DIR = PRICE_DATA / "hyperliquid"
INFO_URL = "https://api.hyperliquid.xyz/info"
# The same universe as the composite index set, so the two correspond 1:1.
DEFAULT_COINS = ("BTC", "ETH", "SOL", "DOGE", "XRP")
BOOK_DEPTH = 20           # levels per side retained from l2Book
TRADE_MEMORY = 4000       # per-coin seen trade ids (recentTrades is a rolling window)
TWAP_MEMORY = 20000       # seen twap slice ids (userTwapSliceFills returns FULL history)
POOL_MAX = 4000           # addresses retained, most-recently-seen first
ACCOUNTS_EVERY = 6        # cycles between account-layer polls
ACCOUNTS_PER_POLL = 3     # addresses polled per account-layer poll
MIN_REQUEST_INTERVAL = 0.06
BACKOFF_MAX_S = 60.0


class Info:
    """POST /info with pacing and 429 backoff. Read-only: no key, no orders."""

    def __init__(self, url: str = INFO_URL):
        self.url = url
        self.session = requests.Session()
        self._next_ok = 0.0
        self._backoff = 0.0
        self.counts = {"ok": 0, "rate_limited": 0, "error": 0}

    def post(self, body: dict, timeout: float = 10.0):
        now = time.time()
        if now < self._next_ok:
            time.sleep(self._next_ok - now)
        try:
            r = self.session.post(self.url, json=body, timeout=timeout)
        except Exception as e:                      # network flap, never fatal
            self.counts["error"] += 1
            logger.debug("info %s failed: %s", body.get("type"), str(e)[:120])
            self._next_ok = time.time() + MIN_REQUEST_INTERVAL
            return None
        if r.status_code == 429:
            self.counts["rate_limited"] += 1
            self._backoff = min(BACKOFF_MAX_S, max(1.0, self._backoff * 2))
            self._next_ok = time.time() + self._backoff
            logger.warning("hyperliquid 429; backing off %.1fs", self._backoff)
            return None
        self._next_ok = time.time() + MIN_REQUEST_INTERVAL
        if not r.ok:
            self.counts["error"] += 1
            logger.debug("info %s -> %s", body.get("type"), r.status_code)
            return None
        self._backoff = 0.0
        self.counts["ok"] += 1
        try:
            return r.json()
        except ValueError:
            self.counts["error"] += 1
            return None


class AddressPool:
    """Addresses seen trading publicly, in the order we first saw them.

    Causal by construction: `add` is only ever called with counterparties of a
    print we have already received, so the pool as of any recorded moment holds
    exactly what was observable then. Rotation is plain round-robin - polling
    order must not be a function of how well an address has done, or the tape
    stops being usable for the very question it is being built to answer.
    """

    def __init__(self, maxlen: int = POOL_MAX):
        self.maxlen = maxlen
        self.seen: OrderedDict[str, dict] = OrderedDict()
        self._cursor = 0

    def add(self, address: str, ts: float, coin: str) -> bool:
        rec = self.seen.get(address)
        if rec is None:
            if len(self.seen) >= self.maxlen:
                self.seen.popitem(last=False)
            self.seen[address] = dict(address=address, first_seen_ts=ts,
                                      last_seen_ts=ts, prints=1, coins=[coin])
            return True
        rec["last_seen_ts"] = ts
        rec["prints"] += 1
        if coin not in rec["coins"]:
            rec["coins"].append(coin)
        return False

    def next_batch(self, n: int) -> list[str]:
        keys = list(self.seen)
        if not keys:
            return []
        out = []
        for i in range(min(n, len(keys))):
            out.append(keys[(self._cursor + i) % len(keys)])
        self._cursor = (self._cursor + len(out)) % len(keys)
        return out


class Recorder:
    def __init__(self, coins=DEFAULT_COINS, root: Path = HL_DIR):
        self.coins = list(coins)
        self.root = Path(root)
        self.info = Info()
        self.pool = AddressPool()
        self._seen_tids: dict[str, deque] = {c: deque(maxlen=TRADE_MEMORY) for c in self.coins}
        self._seen_sets: dict[str, set] = {c: set() for c in self.coins}
        self._seen_twap: deque = deque(maxlen=TWAP_MEMORY)
        self._seen_twap_set: set = set()
        self._locks: list[Path] = []
        self.stats = {"context": 0, "book": 0, "trades": 0, "accounts": 0,
                      "twap_fills": 0, "new_addresses": 0}

    # -- single-instance guard, same idiom as the strips recorder -------------
    def acquire(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        lock = self.root / ".recorder.lock"
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                other = int(lock.read_text().split()[0])
                os.kill(other, 0)                  # signal 0 = liveness probe
            except (ValueError, IndexError, ProcessLookupError, OSError):
                lock.unlink(missing_ok=True)       # stale lock from a dead pid
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            else:
                raise SystemExit(f"hyperliquid recorder already running (pid {other})")
        with os.fdopen(fd, "w") as fh:
            fh.write(f"{os.getpid()} {time.time():.0f}\n")
        self._locks.append(lock)

    def release(self) -> None:
        for lock in self._locks:
            lock.unlink(missing_ok=True)
        self._locks.clear()

    # -- layers ---------------------------------------------------------------
    def context(self, writer: DailyJsonlWriter) -> None:
        """One request covers every coin: mark/oracle/mid, OI, funding, premium."""
        data = self.info.post({"type": "metaAndAssetCtxs"})
        recv = time.time()
        if not data or len(data) != 2:
            return
        meta, ctxs = data
        names = [u["name"] for u in meta.get("universe", [])]
        for coin in self.coins:
            if coin not in names:
                continue
            c = ctxs[names.index(coin)]
            writer.write(f"context/{coin}", dict(
                recv_ts=recv, coin=coin, mark_px=c.get("markPx"),
                oracle_px=c.get("oraclePx"), mid_px=c.get("midPx"),
                prev_day_px=c.get("prevDayPx"), open_interest=c.get("openInterest"),
                funding=c.get("funding"), premium=c.get("premium"),
                day_ntl_vlm=c.get("dayNtlVlm"),
                impact_pxs=c.get("impactPxs")))
            self.stats["context"] += 1

    def book(self, writer: DailyJsonlWriter, coin: str) -> None:
        data = self.info.post({"type": "l2Book", "coin": coin})
        recv = time.time()
        if not data or "levels" not in data:
            return
        bids, asks = (data["levels"] + [[], []])[:2]
        def side(rows):
            # px, size, and n = resting orders at the level. The order count is
            # what a queue-position model needs and a size-only book cannot give.
            return [[r.get("px"), r.get("sz"), r.get("n")] for r in rows[:BOOK_DEPTH]]
        writer.write(f"book/{coin}", dict(recv_ts=recv, coin=coin,
                                          venue_ts=data.get("time"),
                                          bids=side(bids), asks=side(asks)))
        self.stats["book"] += 1

    def trades(self, writer: DailyJsonlWriter, pool_writer: DailyJsonlWriter,
               coin: str) -> None:
        """Public prints, deduplicated by tid, and the address pool they feed."""
        data = self.info.post({"type": "recentTrades", "coin": coin})
        recv = time.time()
        if not isinstance(data, list):
            return
        seen, memo = self._seen_sets[coin], self._seen_tids[coin]
        fresh = []
        for t in data:
            tid = t.get("tid") or t.get("hash")
            if tid is None or tid in seen:
                continue
            if len(memo) == memo.maxlen:
                seen.discard(memo[0])
            memo.append(tid)
            seen.add(tid)
            fresh.append(t)
        for t in sorted(fresh, key=lambda x: x.get("time") or 0):
            users = t.get("users") or []
            writer.write(f"trades/{coin}", dict(
                recv_ts=recv, coin=coin, tid=t.get("tid"), venue_ts=t.get("time"),
                px=t.get("px"), sz=t.get("sz"),
                # "A" = the aggressor sold into the bid. Kept verbatim rather
                # than remapped, so a later reader can check our convention.
                side=t.get("side"), hash=t.get("hash"), users=users))
            self.stats["trades"] += 1
            for u in users:
                if self.pool.add(u, recv, coin):
                    pool_writer.write("address_pool", dict(
                        recv_ts=recv, address=u, first_seen_coin=coin,
                        first_seen_tid=t.get("tid"), venue_ts=t.get("time")))
                    self.stats["new_addresses"] += 1

    def accounts(self, writer: DailyJsonlWriter, twap_writer: DailyJsonlWriter,
                 n: int = ACCOUNTS_PER_POLL) -> None:
        for address in self.pool.next_batch(n):
            state = self.info.post({"type": "clearinghouseState", "user": address})
            recv = time.time()
            if isinstance(state, dict):
                positions = []
                for p in state.get("assetPositions") or []:
                    pos = p.get("position") or {}
                    if pos.get("coin") in self.coins:
                        positions.append(dict(
                            coin=pos.get("coin"), szi=pos.get("szi"),
                            entry_px=pos.get("entryPx"),
                            liquidation_px=pos.get("liquidationPx"),
                            position_value=pos.get("positionValue"),
                            margin_used=pos.get("marginUsed"),
                            unrealized_pnl=pos.get("unrealizedPnl"),
                            leverage=pos.get("leverage"),
                            max_leverage=pos.get("maxLeverage")))
                if positions:
                    writer.write("accounts", dict(
                        recv_ts=recv, address=address,
                        # Cross liquidation is an ACCOUNT event; keep the summary
                        # so distance-to-maintenance can be computed later.
                        margin_summary=state.get("marginSummary"),
                        cross_margin_summary=state.get("crossMarginSummary"),
                        cross_maintenance_margin_used=state.get("crossMaintenanceMarginUsed"),
                        withdrawable=state.get("withdrawable"),
                        positions=positions))
                    self.stats["accounts"] += 1
            slices = self.info.post({"type": "userTwapSliceFills", "user": address})
            recv = time.time()
            if isinstance(slices, list) and slices:
                # This endpoint returns the address's WHOLE slice history, not a
                # recent window, so it must be deduplicated by fill tid - writing
                # the full list on every poll would be almost entirely repeats.
                # What identifies a LIVE twap is slices with recent venue_ts, and
                # that judgement belongs to the reader, not to the tape.
                keep = []
                for s in slices:
                    fill = s.get("fill") or {}
                    if fill.get("coin") not in self.coins:
                        continue
                    tid = fill.get("tid") or fill.get("hash")
                    if tid is None or tid in self._seen_twap_set:
                        continue
                    if len(self._seen_twap) == self._seen_twap.maxlen:
                        self._seen_twap_set.discard(self._seen_twap[0])
                    self._seen_twap.append(tid)
                    self._seen_twap_set.add(tid)
                    keep.append(s)
                if keep:
                    twap_writer.write("twap_fills", dict(
                        recv_ts=recv, address=address, fills=keep))
                    self.stats["twap_fills"] += len(keep)

    # -- loop -----------------------------------------------------------------
    def record(self, interval: float = 5.0, cycles: int = 0) -> dict:
        writer = DailyJsonlWriter(self.root)
        n = 0
        try:
            while True:
                started = time.time()
                try:
                    self.context(writer)
                    for coin in self.coins:
                        self.book(writer, coin)
                        self.trades(writer, writer, coin)
                    if n % ACCOUNTS_EVERY == 0:
                        self.accounts(writer, writer)
                except Exception:                  # a bad cycle must not end the tape
                    logger.exception("hyperliquid cycle failed")
                n += 1
                if n % 120 == 0:
                    logger.info("hyperliquid recorder: %s | http %s | pool %d",
                                self.stats, self.info.counts, len(self.pool.seen))
                if cycles and n >= cycles:
                    break
                time.sleep(max(0.0, interval - (time.time() - started)))
        finally:
            writer.close()
            logger.info("hyperliquid recorder stopped after %d cycles: %s",
                        n, self.stats)
        return dict(cycles=n, **self.stats)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", choices=["record"])
    ap.add_argument("--coins", default=",".join(DEFAULT_COINS))
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--cycles", type=int, default=0)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    coins = [c.strip().upper() for c in args.coins.split(",") if c.strip()]
    rec = Recorder(coins)
    rec.acquire()
    try:
        rec.record(interval=args.interval, cycles=args.cycles)
    finally:
        rec.release()
    return 0


if __name__ == "__main__":                          # pragma: no cover
    raise SystemExit(main())
