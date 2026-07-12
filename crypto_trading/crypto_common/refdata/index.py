"""Settlement-index proxy — volume-weighted spot composite (Plan 00 §5, Plan 08 §3.2).

The BRTI itself is a licensed CF Benchmarks feed; the doable default is a
VWAP-style composite over Coinbase/Kraken/Bitstamp (the probe-verified free
path). Two products:

  * ``backfill_composite(asset, …)`` — per-exchange 1m candles → parquet, plus
    the merged composite parquet:
        price_data/index_proxy/raw/<exchange>_<ASSET>_1m.parquet
        price_data/index_proxy/<ASSET>_composite_1m.parquet
    Composite bar = per-minute volume-weighted close across available venues
    (Kraken only contributes its recent ~720-bar window — documented).
  * ``LiveComposite`` — polls venue tickers, 24h-volume-weighted last price,
    staleness flags; the Plan 01 index anchor. ``record`` mode persists the
    live composite at a fixed cadence:
        price_data/index_proxy/live/<ASSET>/<date>.jsonl

Tracking error vs Kalshi's own mark is a first-class health metric — computed
downstream in the loader, never hidden here.

CLI:
    … -m crypto_trading.crypto_common.refdata.index backfill [--assets BTC,ETH] [--days 730]
    … -m crypto_trading.crypto_common.refdata.index record   [--assets BTC,ETH] [--interval 5]
"""
from __future__ import annotations

import argparse
import logging
import time

import pandas as pd

from crypto_trading.crypto_common.config import PRICE_DATA
from crypto_trading.crypto_common.io_jsonl import DailyJsonlWriter
from crypto_trading.crypto_common.refdata.market_data import make_drivers
from crypto_trading.crypto_common.timeutils import utcnow

logger = logging.getLogger(__name__)

INDEX_DIR = PRICE_DATA / "index_proxy"
STALE_AFTER_S = 30.0


def backfill_composite(asset: str, *, days: int = 730, drivers=None,
                       end_ts: int | None = None) -> pd.DataFrame:
    """Fetch per-exchange 1m candles, persist raw + composite parquet."""
    drivers = drivers or make_drivers()
    end_ts = end_ts or int(utcnow().timestamp())
    start_ts = end_ts - days * 86400
    legs: dict[str, pd.DataFrame] = {}

    raw_dir = INDEX_DIR / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    for drv in drivers:
        out = raw_dir / f"{drv.name}_{asset}_1m.parquet"
        existing = None
        # ranges to fetch: backward head extension + forward top-up (resume-safe
        # in BOTH directions — a shallow first run must not clamp a deep rerun)
        ranges = [(start_ts, end_ts)]
        if out.exists():
            existing = pd.read_parquet(out)
            if len(existing):
                lo, hi = int(existing.ts.min()), int(existing.ts.max())
                ranges = []
                if start_ts < lo - 60:
                    ranges.append((start_ts, lo))
                if end_ts > hi - 60:
                    ranges.append((hi - 60, end_ts))
        try:
            parts = [drv.candles_1m(asset, s, e) for s, e in ranges]
            df = (pd.concat(parts, ignore_index=True)
                  if parts else pd.DataFrame())
        except Exception as e:
            logger.warning("%s %s candles failed: %s: %s", drv.name, asset,
                           type(e).__name__, str(e)[:150])
            df = None
        if df is not None and len(df):
            if existing is not None and len(existing):
                df = pd.concat([existing, df], ignore_index=True)
            df = df.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
            df.to_parquet(out, index=False)
            legs[drv.name] = df
            logger.info("%s %s: %d bars (%s → %s)", drv.name, asset, len(df),
                        pd.Timestamp(df.ts.min(), unit="s", tz="UTC"),
                        pd.Timestamp(df.ts.max(), unit="s", tz="UTC"))
        elif existing is not None:
            legs[drv.name] = existing

    if not legs:
        raise RuntimeError(f"no exchange leg available for {asset}")

    # per-minute volume-weighted close across venues (venues missing a minute
    # simply don't contribute to that bar)
    frames = []
    for name, df in legs.items():
        f = df[["ts", "close", "volume"]].copy()
        f["src"] = name
        frames.append(f)
    allbars = pd.concat(frames, ignore_index=True)
    allbars["wpx"] = allbars.close * allbars.volume
    grp = allbars.groupby("ts", sort=True)
    comp = pd.DataFrame({
        "vw_close": grp.wpx.sum() / grp.volume.sum().replace(0, pd.NA),
        "mean_close": grp.close.mean(),
        "volume": grp.volume.sum(),
        "n_venues": grp.src.nunique(),
    }).reset_index()
    comp["vw_close"] = comp.vw_close.fillna(comp.mean_close)
    out = INDEX_DIR / f"{asset}_composite_1m.parquet"
    comp.to_parquet(out, index=False)
    logger.info("%s composite: %d bars → %s", asset, len(comp), out)
    return comp


class LiveComposite:
    """Live index proxy: 24h-volume-weighted venue last-prices + staleness."""

    def __init__(self, assets: list[str], drivers=None):
        self.assets = list(assets)
        self.drivers = drivers or make_drivers()
        self._last: dict[tuple[str, str], dict] = {}   # (asset, venue) -> ticker

    def sample(self, asset: str) -> dict:
        quotes = []
        now = time.time()
        for drv in self.drivers:
            try:
                t = drv.ticker(asset)
            except Exception as e:
                logger.debug("%s ticker %s failed: %s", drv.name, asset, str(e)[:100])
                t = self._last.get((asset, drv.name))     # reuse if fresh enough
            if t:
                self._last[(asset, drv.name)] = t
                if now - t["ts"] <= STALE_AFTER_S:
                    quotes.append((drv.name, t))
        if not quotes:
            return {"asset": asset, "ts": now, "index": None, "n_venues": 0,
                    "stale": True, "venues": {}}
        wsum = sum(q["price"] * max(q["volume_24h"], 1e-9) for _, q in quotes)
        vsum = sum(max(q["volume_24h"], 1e-9) for _, q in quotes)
        return {"asset": asset, "ts": now, "index": wsum / vsum,
                "n_venues": len(quotes), "stale": False,
                "venues": {n: q["price"] for n, q in quotes}}

    def record(self, interval: float = 5.0, cycles: int = 0) -> None:
        writer = DailyJsonlWriter(INDEX_DIR / "live")
        n = 0
        try:
            while True:
                started = time.time()
                for a in self.assets:
                    writer.write(a, self.sample(a))
                n += 1
                if cycles and n >= cycles:
                    break
                time.sleep(max(0.0, interval - (time.time() - started)))
        finally:
            writer.close()
            logger.info("live composite recorder stopped after %d cycles", n)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", choices=["backfill", "record"])
    ap.add_argument("--assets", default="BTC,ETH")
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--cycles", type=int, default=0)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    assets = [a.strip().upper() for a in args.assets.split(",") if a.strip()]
    if args.mode == "backfill":
        for a in assets:
            backfill_composite(a, days=args.days)
    else:
        LiveComposite(assets).record(interval=args.interval, cycles=args.cycles)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
