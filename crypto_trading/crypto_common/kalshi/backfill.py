"""REST backfill: candlesticks + funding history → parquet (Plan 08 §3.1).

All endpoints are PUBLIC (probe-verified) — no key needed. Incremental: reruns
resume from the last stored bar/cycle. Layout:

    price_data/kalshi/perps/candles_{1m,1h,1d}/<TICKER>.parquet
    price_data/kalshi/funding/<TICKER>.parquet
    price_data/kalshi/refdata/margin_markets_<YYYY-MM-DD>.json

Candle rows keep raw values with empty-book sentinels nulled (launch-day bars
carry int64-max/zero placeholders — enums.PRICE_SENTINEL_BOUND) and a
``had_sentinel`` flag so loaders can drop or inspect. PIT: every row carries
``ingested_at``.

CLI (from repo root):
    conda run -n someopark_run python -m crypto_trading.crypto_common.kalshi.backfill \
        [--env prod] [--tickers KXBTCPERP,…] [--periods 1m,1h,1d] [--no-funding]
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import pandas as pd

from crypto_trading.crypto_common.config import (ACTIVE_PERPS_SNAPSHOT, LISTING_EPOCH_TS,
                                                 PRICE_DATA)
from crypto_trading.crypto_common.kalshi.enums import CANDLE_PERIODS, PRICE_SENTINEL_BOUND
from crypto_trading.crypto_common.kalshi.rest_margin import KalshiMarginClient
from crypto_trading.crypto_common.timeutils import utcnow

logger = logging.getLogger(__name__)

PERPS_DIR = PRICE_DATA / "kalshi" / "perps"
FUNDING_DIR = PRICE_DATA / "kalshi" / "funding"
REFDATA_DIR = PRICE_DATA / "kalshi" / "refdata"

# Server returns ≤ ~60 bars per request regardless of window (probe) — chunk
# conservatively and advance by what actually came back.
CHUNK_BARS = 60


def _clean_price(v) -> float | None:
    """Decimal-string → float; empty-book sentinels (int64-max / ≤0) → None."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f <= 0.0 or f >= PRICE_SENTINEL_BOUND:
        return None
    return f


def parse_candle(c: dict) -> dict | None:
    """Flatten one raw candlestick into a row dict (None on missing ts)."""
    ts = c.get("end_period_ts") or c.get("ts")
    if ts is None:
        return None
    row: dict = {"ts": int(ts)}
    had_sentinel = False
    for side in ("price", "bid", "ask"):
        node = c.get(side) or {}
        for f in ("open", "high", "low", "close"):
            raw = node.get(f)
            val = _clean_price(raw)
            if raw is not None and val is None:
                had_sentinel = True
            row[f"{side}_{f}"] = val
    for k, col in (("volume", "volume"), ("volume_fp", "volume"),
                   ("open_interest", "oi"),
                   ("open_interest_notional_value_dollars", "oi_notional")):
        if k in c and c[k] is not None and col not in row:
            try:
                row[col] = float(c[k])
            except (TypeError, ValueError):
                pass
    row["had_sentinel"] = had_sentinel
    return row


def backfill_candles(client: KalshiMarginClient, ticker: str, period_key: str,
                     *, start_ts: int = LISTING_EPOCH_TS, end_ts: int | None = None) -> int:
    """Fetch [start,end] in resume-safe chunks; merge into the ticker parquet."""
    period_min = CANDLE_PERIODS[period_key]
    period_sec = period_min * 60
    end_ts = end_ts or int(utcnow().timestamp())
    out = PERPS_DIR / f"candles_{period_key}" / f"{ticker}.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)

    existing = None
    if out.exists():
        existing = pd.read_parquet(out)
        if len(existing):
            # overlap one bar to re-pull a possibly partial final bar
            start_ts = max(start_ts, int(existing["ts"].max()) - period_sec)

    rows: list[dict] = []
    cur = start_ts
    while cur < end_ts:
        window_end = min(cur + CHUNK_BARS * period_sec, end_ts)
        bars = client.candlesticks(ticker, cur, window_end, period_min)
        parsed = [r for r in (parse_candle(b) for b in bars) if r]
        rows.extend(parsed)
        if parsed:
            last = max(r["ts"] for r in parsed)
            cur = max(last + period_sec, cur + period_sec)  # guaranteed progress
        else:
            cur = window_end
    if not rows and existing is None:
        logger.info("%s %s: nothing fetched", ticker, period_key)
        return 0

    df = pd.DataFrame(rows)
    if not df.empty:
        df["ingested_at"] = int(utcnow().timestamp())
    frames = [f for f in (existing, df) if f is not None and not f.empty]
    merged = pd.concat(frames, ignore_index=True)
    merged = (merged.sort_values(["ts", "ingested_at"])
                    .drop_duplicates("ts", keep="last")
                    .reset_index(drop=True))
    merged.to_parquet(out, index=False)
    logger.info("%s %s: +%d fetched → %d rows total", ticker, period_key, len(df), len(merged))
    return len(df)


def backfill_funding(client: KalshiMarginClient, ticker: str) -> int:
    """Pull the full funding-rate history (cursor-paginated) and merge."""
    out = FUNDING_DIR / f"{ticker}.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    cursor = None
    for _ in range(200):  # hard page cap — 8h cycles ⇒ tiny data
        page = client.funding_rates_historical(ticker, cursor=cursor)
        rows.extend(page.get("funding_rates", []))
        cursor = page.get("cursor")
        if not cursor:
            break
    if not rows:
        return 0
    df = pd.DataFrame(rows)
    df["funding_time"] = pd.to_datetime(df["funding_time"], utc=True)
    df["funding_rate"] = pd.to_numeric(df["funding_rate"], errors="coerce")
    df["mark_price"] = pd.to_numeric(df["mark_price"], errors="coerce")
    df["ingested_at"] = int(utcnow().timestamp())
    if out.exists():
        old = pd.read_parquet(out)
        df = pd.concat([old, df], ignore_index=True)
    df = (df.sort_values(["funding_time", "ingested_at"])
            .drop_duplicates("funding_time", keep="last")
            .reset_index(drop=True))
    df.to_parquet(out, index=False)
    return len(df)


def snapshot_markets(client: KalshiMarginClient) -> list[str]:
    """Persist the /margin/markets universe snapshot; return ACTIVE tickers."""
    mkts = client.markets()
    REFDATA_DIR.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y-%m-%d")
    (REFDATA_DIR / f"margin_markets_{stamp}.json").write_text(
        json.dumps({"fetched_at": utcnow().isoformat(), "markets": mkts}, indent=1))
    return [m["ticker"] for m in mkts if m.get("status") == "active"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--env", default="prod", choices=["prod", "demo"],
                    help="prod = real market data (default; public endpoints)")
    ap.add_argument("--tickers", default="",
                    help="comma-separated; default = live active universe")
    ap.add_argument("--periods", default="1m,1h,1d")
    ap.add_argument("--no-funding", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    client = KalshiMarginClient(env=args.env, min_interval=0.15)  # be polite on public endpoints

    tickers = ([t.strip() for t in args.tickers.split(",") if t.strip()]
               or snapshot_markets(client) or list(ACTIVE_PERPS_SNAPSHOT))
    periods = [p.strip() for p in args.periods.split(",") if p.strip()]
    for p in periods:
        if p not in CANDLE_PERIODS:
            ap.error(f"unknown period {p!r} (choose from {sorted(CANDLE_PERIODS)})")

    total = 0
    for t in tickers:
        for p in periods:
            total += backfill_candles(client, t, p)
        if not args.no_funding:
            n = backfill_funding(client, t)
            logger.info("%s funding: %d cycles stored", t, n)
    logger.info("backfill done: %d new bars across %d tickers", total, len(tickers))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
