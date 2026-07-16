"""Data loader over crypto_trading's own stores (Plan 00 §2 `loader.py`).

Mirrors the sector_rotation loader CONTRACT (read-only template
qlib-main/sector_rotation/data/loader.py): pure functions returning clean,
tz-aware-UTC-indexed DataFrames with an explicit **no-NaN contract** at the
feature-frame boundary; raw stores are never mutated.

PIT rules enforced here (Plan 08 §4):
  * funding is known only AT settlement → any join uses backward as-of logic,
    and the ESTIMATE is a live-only feature (never joined into backtests).
  * candle sentinel rows arrive pre-nulled (backfill.parse_candle) — loaders
    expose ``drop_sentinels`` (default True for feature frames).
  * proxy tables carry their ``source`` column through untouched.
"""
from __future__ import annotations

import gzip
import json
import logging
from pathlib import Path

import pandas as pd

from crypto_trading.crypto_common.config import PRICE_DATA

logger = logging.getLogger(__name__)

PERPS_DIR = PRICE_DATA / "kalshi" / "perps"
FUNDING_DIR = PRICE_DATA / "kalshi" / "funding"
INDEX_DIR = PRICE_DATA / "index_proxy"
OFFSHORE_DIR = PRICE_DATA / "offshore"


def _utc_index(df: pd.DataFrame, ts_col: str = "ts", unit: str = "s") -> pd.DataFrame:
    df = df.copy()
    df.index = pd.to_datetime(df[ts_col], unit=unit, utc=True)
    df.index.name = "dt"
    return df.drop(columns=[ts_col]).sort_index()


def _utc_ts(x) -> pd.Timestamp:
    """Coerce to a tz-aware UTC Timestamp, whether input is naive or aware."""
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _clip(df: pd.DataFrame, start=None, end=None) -> pd.DataFrame:
    if start is not None:
        df = df[df.index >= _utc_ts(start)]
    if end is not None:
        df = df[df.index <= _utc_ts(end)]
    return df


# ── Kalshi perps ────────────────────────────────────────────────────────────

def load_perp_candles(ticker: str, period: str = "1m", *, start=None, end=None,
                      drop_sentinels: bool = True) -> pd.DataFrame:
    """Backfilled candles → UTC-indexed frame. Columns: price/bid/ask OHLC, oi…"""
    path = PERPS_DIR / f"candles_{period}" / f"{ticker}.parquet"
    if not path.exists():
        raise FileNotFoundError(f"no candles for {ticker} {period} — run backfill first")
    df = _utc_index(pd.read_parquet(path))
    if drop_sentinels and "had_sentinel" in df.columns:
        df = df[~df.had_sentinel]
    return _clip(df, start, end)


def load_funding(ticker: str, *, start=None, end=None) -> pd.DataFrame:
    """Realized funding cycles (PIT: known at funding_time, not before)."""
    path = FUNDING_DIR / f"{ticker}.parquet"
    if not path.exists():
        raise FileNotFoundError(f"no funding history for {ticker}")
    df = pd.read_parquet(path)
    df.index = pd.DatetimeIndex(df.funding_time).tz_convert("UTC")
    df.index.name = "dt"
    df = df.drop(columns=["funding_time"]).sort_index()
    return _clip(df, start, end)


def _read_jsonl_days(root: Path, *, days: list[str] | None = None):
    """Yield parsed lines from daily jsonl/jsonl.gz files (raw tape reader)."""
    if not root.exists():
        return
    files = sorted(root.glob("*.jsonl")) + sorted(root.glob("*.jsonl.gz"))
    for f in files:
        day = f.name.split(".")[0]
        if days and day not in days:
            continue
        opener = gzip.open if f.suffix == ".gz" else open
        with opener(f, "rt") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("bad jsonl line in %s — skipped", f)


def load_poll_books(ticker: str, *, env: str = "prod",
                    days: list[str] | None = None) -> list[dict]:
    """Prod-poller book snapshots: [{'recv_ts', 'ob': {'asks': …, 'bids': …}}].

    Deduped by recv_ts (overlap/reconnect-safe) preserving chronological order.
    """
    seen: set = set()
    out = []
    for line in _read_jsonl_days(root := PERPS_DIR / "poll" / env / "orderbook" / ticker,
                                 days=days):
        key = line.get("recv_ts")
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
    return out


def load_poll_trades(ticker: str, *, env: str = "prod",
                     days: list[str] | None = None) -> pd.DataFrame:
    rows = []
    for line in _read_jsonl_days(PERPS_DIR / "poll" / env / "trades" / ticker, days=days):
        t = line.get("t") or {}
        rows.append({"recv_ts": line.get("recv_ts"), "created_time": t.get("created_time"),
                     "price": float(t.get("price") or "nan"),
                     "count": float(t.get("count") or "nan"),
                     "taker_side": t.get("taker_side"), "trade_id": t.get("trade_id")})
    df = pd.DataFrame(rows)
    if len(df):
        # dedupe by trade_id: raw tape can carry duplicates from WS reconnect
        # replays or overlapping-recorder windows — the id is the source of truth
        df = df.drop_duplicates("trade_id", keep="first")
        df.index = pd.DatetimeIndex(pd.to_datetime(df.created_time, utc=True))
        df.index.name = "dt"
        df = df.drop(columns=["created_time"]).sort_index()
        df = df.dropna(subset=["price", "count"])
    return df


def load_poll_market_stats(ticker: str, *, env: str = "prod",
                           days: list[str] | None = None) -> pd.DataFrame:
    """Per-cycle market stats (OI, liq mark, leverage estimates flattened)."""
    rows = []
    for line in _read_jsonl_days(PERPS_DIR / "poll" / env / "markets" / ticker, days=days):
        m = line.get("m") or {}
        rows.append({
            "recv_ts": line.get("recv_ts"),
            "bid": float(m.get("bid") or "nan"), "ask": float(m.get("ask") or "nan"),
            "price": float(m.get("price") or "nan"),
            "oi": float(m.get("open_interest") or "nan"),
            "oi_notional": float(m.get("open_interest_notional_value_dollars") or "nan"),
            "liq_mark": float((m.get("liquidation_mark_price") or {}).get("price") or "nan"),
            "contract_size": float(m.get("contract_size") or "nan"),
        })
    df = pd.DataFrame(rows)
    if len(df):
        df = df.drop_duplicates("recv_ts", keep="first")   # overlap-safe
        df = _utc_index(df, "recv_ts", unit="s")
    return df


# ── index proxy ─────────────────────────────────────────────────────────────

def load_index_composite(asset: str, *, start=None, end=None) -> pd.DataFrame:
    path = INDEX_DIR / f"{asset}_composite_1m.parquet"
    if not path.exists():
        raise FileNotFoundError(f"no composite for {asset} — run refdata.index backfill")
    return _clip(_utc_index(pd.read_parquet(path)), start, end)


def load_index_live(asset: str, *, days: list[str] | None = None) -> pd.DataFrame:
    rows = [{"recv_ts": l["ts"], "index": l.get("index"),
             "n_venues": l.get("n_venues"), "stale": l.get("stale")}
            for l in _read_jsonl_days(INDEX_DIR / "live" / asset, days=days)]
    df = pd.DataFrame(rows)
    if len(df):
        df = _utc_index(df, "recv_ts", unit="s").dropna(subset=["index"])
    return df


# ── offshore proxy (labeled) ────────────────────────────────────────────────

def load_offshore(kind: str, symbol: str, *, driver: str | None = None) -> pd.DataFrame:
    """kind ∈ {'klines_1h','klines_1d','funding'}. Auto-detects driver dir."""
    drivers = [driver] if driver else [d.name for d in OFFSHORE_DIR.iterdir()
                                       if d.is_dir()] if OFFSHORE_DIR.exists() else []
    for d in drivers:
        path = OFFSHORE_DIR / d / kind / f"{symbol}.parquet"
        if path.exists():
            df = pd.read_parquet(path)
            ts_col = "ts" if "ts" in df.columns else "funding_time"
            return _utc_index(df, ts_col)
    raise FileNotFoundError(f"no offshore {kind} for {symbol}")


# ── feature frames (the no-NaN boundary) ────────────────────────────────────

def build_basis_frame(ticker: str = "KXBTCPERP", asset: str = "BTC", *,
                      start=None, end=None, max_gap_min: int = 3) -> pd.DataFrame:
    """Plan 01 feature frame at 1m: mark mid, index proxy, basis b_t (bps).

    Mark mid = (bid_close+ask_close)/2 from Kalshi 1m candles, divided by
    contract_size to underlying level; index = composite vw_close. Short gaps
    forward-filled up to ``max_gap_min``; rows still NaN are dropped (no-NaN
    contract).
    """
    candles = load_perp_candles(ticker, "1m", start=start, end=end)
    stats = None
    try:
        stats = load_poll_market_stats(ticker)
    except Exception:
        pass
    csize = None
    if stats is not None and len(stats):
        csize = float(stats.contract_size.dropna().iloc[-1])
    if not csize:
        # probe-verified contract sizes as fallback
        csize = {"KXBTCPERP": 1e-4, "KXETHPERP": 1e-3}.get(ticker)
    if not csize:
        raise ValueError(f"unknown contract_size for {ticker} — need poll stats")

    idx = load_index_composite(asset, start=start, end=end)
    mid = (candles.bid_close + candles.ask_close) / 2.0
    frame = pd.DataFrame({
        "mark_mid_contract": mid,
        "mark_mid_underlying": mid / csize,
        "index_proxy": idx.vw_close,
        "index_venues": idx.n_venues,
    })
    frame = frame.ffill(limit=max_gap_min)
    frame = frame.dropna()
    frame["b_t"] = (frame.mark_mid_underlying - frame.index_proxy) / frame.index_proxy
    frame["b_t_bps"] = 1e4 * frame.b_t
    if frame.isna().any().any():   # explicit raise (survives python -O, unlike assert)
        raise ValueError("no-NaN contract violated in build_basis_frame")
    return frame
