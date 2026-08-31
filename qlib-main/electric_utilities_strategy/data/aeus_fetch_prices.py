"""
AEUS price fetcher (self-contained, isolated storage)
=====================================================
Production-grade OHLCV fetcher for the AI Infra & Electric Utilities Strategy.

Why a dedicated fetcher (not the root ``PriceDataStore``)
--------------------------------------------------------
The root ``PriceDataStore.load()`` mutates the *shared* ``price_data/index.json``
and the shared weekly-parquet cache used by the MRPT/MTFS pair strategies.  The
AEUS build is constrained to **add-only** files under ``price_data/`` and must
not modify any existing file.  So this module re-implements the same Polygon
fetch + dividend-adjustment logic (copied & adapted from ``PriceDataStore.py``)
but stores everything in its **own** isolated directory:

    price_data/elec_strategy/prices/{TICKER}_prices.parquet      # per-ticker OHLCV+AdjClose
    price_data/elec_strategy/prices/_fetch_meta.json             # per-ticker coverage metadata

Data contract (per ``{TICKER}_prices.parquet``)
-----------------------------------------------
    DatetimeIndex 'Date' (daily, ascending, de-duplicated)
    columns: Open, High, Low, Close, Volume, AdjClose
      - Open/High/Low/Close/Volume : SPLIT-adjusted (Polygon adjusted=true)
      - AdjClose                   : split+dividend total-return adjusted close

Sources (in priority order)
---------------------------
    1. Polygon.io  (v2/aggs split-adjusted bars + v3/reference/dividends)
    2. yfinance    (fallback; auto_adjust=False so we keep raw OHLC + 'Adj Close')

Both are used read-only against the network; nothing outside
``price_data/elec_strategy/`` is ever written.

CLI
---
    python -m electric_utilities_strategy.data.aeus_fetch_prices --init  --start 2016-01-01
    python -m electric_utilities_strategy.data.aeus_fetch_prices --update
    python -m electric_utilities_strategy.data.aeus_fetch_prices --verify
    python -m electric_utilities_strategy.data.aeus_fetch_prices --tickers NVDA XLU
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

# --- robust imports whether run as module or script -------------------------
_THIS_DIR = Path(__file__).resolve().parent              # .../electric_utilities_strategy/data
_PKG_DIR = _THIS_DIR.parent                              # .../electric_utilities_strategy
_QLIB_DIR = _PKG_DIR.parent                              # .../qlib-main
_PROJECT_DIR = _QLIB_DIR.parent                          # .../someopark-test
for _p in (str(_QLIB_DIR), str(_PROJECT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from electric_utilities_strategy.data import universe as U
except Exception:  # pragma: no cover - fallback when run as a loose script
    import universe as U  # type: ignore

log = logging.getLogger("aeus.fetch_prices")

# ---------------------------------------------------------------------------
# Storage paths (isolated under price_data/elec_strategy/prices)
# ---------------------------------------------------------------------------

PRICES_DIR = _PROJECT_DIR / "price_data" / "elec_strategy" / "prices"
META_PATH = PRICES_DIR / "_fetch_meta.json"

STORED_FIELDS = ["Open", "High", "Low", "Close", "Volume"]
ALL_COLUMNS = STORED_FIELDS + ["AdjClose"]

DEFAULT_INIT_START = "2016-01-01"
_API_DELAY = 0.2


# ---------------------------------------------------------------------------
# Meta helpers
# ---------------------------------------------------------------------------

def _resolve_dir(prices_dir: Optional[Path]) -> Path:
    """Store directory for this call — defaults to the AEUS elec_strategy store.
    Pass a different dir to keep a SEPARATE per-ticker Polygon store (e.g. SSRS
    sector ETFs), so universes don't get mixed into one store."""
    return Path(prices_dir) if prices_dir else PRICES_DIR


def _load_meta(prices_dir: Optional[Path] = None) -> dict:
    mp = _resolve_dir(prices_dir) / "_fetch_meta.json"
    if mp.exists():
        try:
            return json.loads(mp.read_text())
        except Exception:
            return {}
    return {}


def _save_meta(meta: dict, prices_dir: Optional[Path] = None) -> None:
    d = _resolve_dir(prices_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / "_fetch_meta.json").write_text(json.dumps(meta, indent=2, sort_keys=True))


def _parquet_path(ticker: str, prices_dir: Optional[Path] = None) -> Path:
    return _resolve_dir(prices_dir) / f"{ticker}_prices.parquet"


def _polygon_key() -> Optional[str]:
    return os.environ.get("POLYGON_API_KEY")


# ---------------------------------------------------------------------------
# Polygon: OHLCV (split-adjusted)
# ---------------------------------------------------------------------------

def fetch_polygon_ohlcv(
    ticker: str, start: str, end: str, api_key: str, api_delay: float = _API_DELAY,
) -> Optional[pd.DataFrame]:
    """Fetch split-adjusted daily OHLCV from Polygon for one ticker.

    Returns DataFrame[Open,High,Low,Close,Volume] indexed by Date, or None on
    empty result.  Raises on persistent network failure.
    """
    import requests

    url = (
        f"https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/"
        f"{start}/{end}?adjusted=true&sort=asc&limit=50000&apiKey={api_key}"
    )
    last_err = None
    for attempt in range(3):
        try:
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
            body = resp.json()
            if body.get("status") not in ("OK", "DELAYED"):
                if body.get("resultsCount", 0) == 0:
                    return None
                raise ValueError(f"Polygon status={body.get('status')} for {ticker}")
            results = body.get("results")
            if not results:
                return None
            df = pd.DataFrame(results)
            df["Date"] = pd.to_datetime(df["t"], unit="ms").dt.normalize()
            df = df.set_index("Date")
            out = pd.DataFrame({
                "Open": df["o"], "High": df["h"], "Low": df["l"],
                "Close": df["c"], "Volume": df["v"],
            })
            time.sleep(api_delay)
            return out
        except Exception as e:  # noqa: BLE001
            last_err = e
            wait = 2 ** attempt
            log.warning("Polygon OHLCV %s attempt %d/3 failed: %s (retry %ds)",
                        ticker, attempt + 1, e, wait)
            time.sleep(wait)
    raise RuntimeError(f"Polygon OHLCV failed for {ticker}: {last_err}")


def fetch_polygon_dividends(
    ticker: str, api_key: str, api_delay: float = _API_DELAY,
) -> List[dict]:
    """Fetch the full dividend history for a ticker (paginated)."""
    import requests

    divs: List[dict] = []
    url = (
        f"https://api.polygon.io/v3/reference/dividends?ticker={ticker}"
        f"&order=asc&limit=1000&apiKey={api_key}"
    )
    while url:
        for attempt in range(3):
            try:
                resp = requests.get(url, timeout=60)
                resp.raise_for_status()
                break
            except Exception:  # noqa: BLE001
                if attempt == 2:
                    log.warning("Polygon dividends failed for %s; treating as none", ticker)
                    return divs
                time.sleep(2 ** attempt)
        body = resp.json()
        divs.extend(body.get("results", []))
        nxt = body.get("next_url")
        url = f"{nxt}&apiKey={api_key}" if nxt else None
    time.sleep(api_delay)
    return divs


def compute_adj_close(close: pd.Series, dividends: List[dict]) -> pd.Series:
    """Total-return adjusted close from split-adjusted close + dividends.

    Standard back-adjustment: for each ex-date, the multiplicative factor is
    ``1 - cash / close_on_last_session_before_ex`` (using the split-adjusted
    close), and every price strictly before the ex-date is multiplied by the
    product of all such factors at or after it.  Applying factors from latest to
    earliest on a running series yields exactly that cumulative product.
    """
    adj = close.astype(float).copy()
    # sort ex-dates descending so the running product compounds correctly
    rows = []
    for d in dividends:
        ex = d.get("ex_dividend_date")
        amt = d.get("cash_amount")
        if ex is None or amt in (None, 0):
            continue
        rows.append((pd.Timestamp(ex).normalize(), float(amt)))
    rows.sort(key=lambda r: r[0], reverse=True)
    for ex_ts, amt in rows:
        mask = adj.index < ex_ts
        if not mask.any():
            continue
        pre_ex_close = float(close.loc[close.index[mask][-1]])
        if pre_ex_close <= 0:
            continue
        factor = 1.0 - (amt / pre_ex_close)
        if factor <= 0:
            continue
        adj.loc[mask] = adj.loc[mask] * factor
    return adj


# ---------------------------------------------------------------------------
# yfinance fallback
# ---------------------------------------------------------------------------

def fetch_yfinance_ohlcv(ticker: str, start: str, end: Optional[str]) -> Optional[pd.DataFrame]:
    """Fallback OHLCV via yfinance (auto_adjust=False keeps raw OHLC + Adj Close)."""
    try:
        import warnings as _w
        _w.filterwarnings("ignore")
        import yfinance as yf
    except Exception as e:  # noqa: BLE001
        log.error("yfinance unavailable: %s", e)
        return None
    raw = yf.download(ticker, start=start, end=end, auto_adjust=False, progress=False)
    if raw is None or len(raw) == 0:
        return None
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    raw.index = pd.to_datetime(raw.index).normalize()
    cols = {}
    for src, dst in [("Open", "Open"), ("High", "High"), ("Low", "Low"),
                     ("Close", "Close"), ("Volume", "Volume")]:
        if src in raw.columns:
            cols[dst] = raw[src]
    out = pd.DataFrame(cols)
    if "Adj Close" in raw.columns:
        out["AdjClose"] = raw["Adj Close"]
    elif "Close" in raw.columns:
        out["AdjClose"] = raw["Close"]
    return out


# ---------------------------------------------------------------------------
# Per-ticker fetch / merge / persist
# ---------------------------------------------------------------------------

def _read_existing(ticker: str, prices_dir: Optional[Path] = None) -> Optional[pd.DataFrame]:
    p = _parquet_path(ticker, prices_dir)
    if not p.exists():
        return None
    try:
        df = pd.read_parquet(p)
        df.index = pd.to_datetime(df.index).normalize()
        return df.sort_index()
    except Exception as e:  # noqa: BLE001
        log.warning("Failed reading %s (%s); will refetch", p, e)
        return None


def _earliest_cached_date(existing: Optional[pd.DataFrame]):
    """First date string of a cached frame, or None. Used to floor heal refetches."""
    if existing is None or len(existing) == 0:
        return None
    try:
        return existing.index[0].date().isoformat()
    except Exception:
        return None


# A refetch that returns far less history than what's already cached is almost
# always a bug (wrong/short ``start`` on a force=True path), not a legitimate
# shrink. Refuse to overwrite when the new frame keeps < this fraction of the
# cached rows AND starts materially later — unless ALLOW_TRUNCATE is set (used by
# the explicit --force/--init full-rebuild path).
_TRUNCATE_KEEP_FRACTION = 0.5


def _persist(ticker: str, df: pd.DataFrame, source: str, meta: dict,
             prices_dir: Optional[Path] = None,
             allow_truncate: bool = False) -> bool:
    """Write the frame to the store. Returns True if written, False if the
    truncation guard refused the overwrite (disk cache left intact). Callers
    MUST check the result and fall back to the cached frame on False, so a bad
    short refetch never propagates into in-memory signal computation."""
    _resolve_dir(prices_dir).mkdir(parents=True, exist_ok=True)
    df = df[[c for c in ALL_COLUMNS if c in df.columns]].copy()
    df.index.name = "Date"
    # ── Truncation guard: don't let a short/bad refetch clobber long history ──
    if not allow_truncate:
        prev = _read_existing(ticker, prices_dir)
        if prev is not None and len(prev) > 10:
            shrinks = len(df) < len(prev) * _TRUNCATE_KEEP_FRACTION
            starts_later = (len(df) and len(prev)
                            and df.index[0] > prev.index[0] + pd.Timedelta(days=180))
            if shrinks and starts_later:
                log.error(
                    "[CA][store] %s: REFUSING truncating overwrite — new=%d rows "
                    "(%s→%s) vs cached=%d rows (%s→%s). Keeping cache. Pass "
                    "allow_truncate (CLI --force/--init) to rebuild intentionally.",
                    ticker, len(df), df.index[0].date(), df.index[-1].date(),
                    len(prev), prev.index[0].date(), prev.index[-1].date())
                return False
    df.to_parquet(_parquet_path(ticker, prices_dir), engine="pyarrow")
    meta[ticker] = {
        "source": source,
        "first_date": df.index[0].date().isoformat(),
        "last_date": df.index[-1].date().isoformat(),
        "rows": int(len(df)),
        "fetched_at": date.today().isoformat(),                       # legacy (date)
        "fetched_at_ts": datetime.now().isoformat(timespec="seconds"),  # for staleness throttle
    }
    return True


def update_ticker(
    ticker: str,
    start: str = DEFAULT_INIT_START,
    end: Optional[str] = None,
    api_key: Optional[str] = None,
    force: bool = False,
    meta: Optional[dict] = None,
    prices_dir: Optional[Path] = None,
    allow_truncate: bool = False,
) -> Optional[pd.DataFrame]:
    """Fetch/refresh a single ticker into its isolated parquet.

    Incremental: re-fetches only the missing tail since the last cached date
    (with a small overlap), then recomputes AdjClose over the *full* merged
    series (dividends retro-adjust history).  ``force=True`` does a full refetch.
    ``prices_dir`` selects the store (default = AEUS elec_strategy).
    """
    end = end or date.today().isoformat()
    api_key = api_key or _polygon_key()
    standalone = meta is None
    if meta is None:
        meta = _load_meta(prices_dir)

    existing = None if force else _read_existing(ticker, prices_dir)
    fetch_start = start
    if existing is not None and len(existing) > 0:
        last = existing.index[-1].date()
        # refetch last ~7 days too (late corrections), then append
        fetch_start = (last - timedelta(days=7)).isoformat()

    # ---- fetch OHLCV (Polygon, then yfinance) ----
    source = "polygon"
    ohlcv = None
    if api_key:
        try:
            ohlcv = fetch_polygon_ohlcv(ticker, fetch_start, end, api_key)
        except Exception as e:  # noqa: BLE001
            log.warning("Polygon failed for %s (%s); trying yfinance", ticker, e)
            ohlcv = None
    if ohlcv is None:
        source = "yfinance"
        yf_end = (pd.Timestamp(end) + pd.Timedelta(days=1)).date().isoformat()
        ohlcv = fetch_yfinance_ohlcv(ticker, fetch_start, yf_end)

    if ohlcv is None or len(ohlcv) == 0:
        if existing is not None:
            log.info("%s: no new data; keeping %d cached rows", ticker, len(existing))
            return existing
        log.error("%s: NO data from any source", ticker)
        return None

    # ---- Retro-adjustment self-heal (splits / corporate actions) ----
    # Polygon/yfinance 返回的是回溯调整后的历史。7 天 overlap 窗口正好用来检测：
    # 若同一日期的 Close 与缓存值差异 >2%，说明发生了回溯调整（如 KLAC 1:10 拆股，
    # 2026-06-12 事故：增量 merge 在 overlap 边界留下 10 倍断崖，pct_change 产生
    # 假 -90% 回报直接打崩 returns-based subsector basket 信号）。
    # → 丢弃缓存，整段历史重拉一次，保证全序列同口径。
    if existing is not None and "Close" in existing.columns and "Close" in ohlcv.columns:
        _common = existing.index.intersection(ohlcv.index)
        if len(_common) > 0:
            _old = existing.loc[_common, "Close"].astype(float)
            _new = ohlcv.loc[_common, "Close"].astype(float)
            _ok = (_old > 0) & (_new > 0)
            if _ok.any():
                _ratio = (_old[_ok] / _new[_ok])
                _dev = (_ratio - 1.0).abs()
                if (_dev > 0.02).any():
                    _worst = float(_ratio.loc[_dev.idxmax()])
                    # A "FULL refetch" must use the ORIGINAL history start, not the
                    # caller's (possibly recent) ``start`` — otherwise force=True (which
                    # skips reading existing) overwrites full history with a short window.
                    # 2026-06-13: UpdateMasterPerformance loads with start=live-10d
                    # (2026-05-22); without this floor the heal truncated MU to 15 rows.
                    _heal_start = min(start, _earliest_cached_date(existing) or start,
                                      DEFAULT_INIT_START)
                    log.warning(
                        "[CA][store] %s: retro price adjustment detected on overlap "
                        "(stored/new ratio %.4f on %s — likely split) — discarding "
                        "cache, FULL refetch %s→%s",
                        ticker, _worst, _dev.idxmax().date(), _heal_start, end)
                    healed = update_ticker(ticker, start=_heal_start, end=end,
                                           api_key=api_key, force=True,
                                           meta=meta, prices_dir=prices_dir)
                    if standalone:
                        _save_meta(meta, prices_dir)
                    return healed

    # ---- merge with existing raw OHLCV ----
    if existing is not None:
        base = existing[[c for c in STORED_FIELDS if c in existing.columns]]
        merged = pd.concat([base, ohlcv[[c for c in STORED_FIELDS if c in ohlcv.columns]]])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    else:
        merged = ohlcv[[c for c in STORED_FIELDS if c in ohlcv.columns]].copy()

    merged = merged[~merged.index.duplicated(keep="last")].sort_index()

    # ---- AdjClose ----
    if source == "polygon" and api_key:
        divs = fetch_polygon_dividends(ticker, api_key)
        merged["AdjClose"] = compute_adj_close(merged["Close"], divs)
    else:
        # yfinance path: carry its Adj Close where available, else fall back to Close
        if "AdjClose" in ohlcv.columns:
            adj = ohlcv["AdjClose"].reindex(merged.index)
            if existing is not None and "AdjClose" in existing.columns:
                adj = adj.combine_first(existing["AdjClose"].reindex(merged.index))
            merged["AdjClose"] = adj.fillna(merged["Close"])
        else:
            merged["AdjClose"] = merged["Close"]

    written = _persist(ticker, merged, source, meta, prices_dir,
                       allow_truncate=allow_truncate)
    if not written:
        # Guard refused a truncating overwrite: the disk cache is intact but the
        # in-memory `merged` is the bad short frame. Return the cached frame so the
        # caller (e.g. load_prices_wide) never computes on the truncated series.
        cached = _read_existing(ticker, prices_dir)
        if cached is not None and len(cached):
            log.warning("%s: refetch rejected by truncation guard — returning "
                        "cached %d rows (%s→%s) instead of short %d-row frame",
                        ticker, len(cached), cached.index[0].date(),
                        cached.index[-1].date(), len(merged))
            return cached
        return None  # no cache to fall back to → signal "no usable data"
    if standalone:
        _save_meta(meta, prices_dir)
    log.info("%s: %d rows %s→%s (%s)", ticker, len(merged),
             merged.index[0].date(), merged.index[-1].date(), source)
    return merged


def update_all(
    tickers: Optional[List[str]] = None,
    start: str = DEFAULT_INIT_START,
    end: Optional[str] = None,
    force: bool = False,
    prices_dir: Optional[Path] = None,
) -> Dict[str, bool]:
    """Fetch/refresh all AEUS tickers (universe stocks + benchmarks).
    ``prices_dir`` selects the store (default = AEUS elec_strategy).

    ``force`` (CLI --force/--init) means an INTENTIONAL full rebuild from
    ``start``, so it also passes allow_truncate=True — an explicit rebuild is
    allowed to shrink history (e.g. a deliberate re-init), while the incremental
    daily/weekly path keeps the truncation guard on."""
    if tickers is None:
        tickers = U.all_tickers(include_benchmark=True)
    meta = _load_meta(prices_dir)
    results: Dict[str, bool] = {}
    for i, t in enumerate(tickers):
        try:
            df = update_ticker(t, start=start, end=end, force=force, meta=meta,
                               prices_dir=prices_dir, allow_truncate=force)
            results[t] = df is not None and len(df) > 0
        except Exception as e:  # noqa: BLE001
            log.error("update_ticker(%s) raised: %s", t, e)
            results[t] = False
        if (i + 1) % 5 == 0:
            _save_meta(meta, prices_dir)  # checkpoint
    _save_meta(meta, prices_dir)
    return results


# ---------------------------------------------------------------------------
# Load API (used by loader.py)
# ---------------------------------------------------------------------------

# A ticker whose store does not yet cover the requested ``end`` is re-fetched,
# but at most once per this many hours, to avoid hammering Polygon on repeated
# loads or when ``end``'s close is genuinely not published yet. A pre-close
# fetch (which only gets the prior close) therefore does NOT block a later
# after-close fetch — that was the bug where the daily after-close run reused
# the morning's stale prices.
_REFETCH_THROTTLE_HOURS = 1.0


def _fetched_age_hours(entry: dict) -> float:
    """Hours since this ticker's last fetch attempt. Prefers the precise
    'fetched_at_ts' timestamp; falls back to the legacy 'fetched_at' date
    (midnight). Returns a large number if unknown → eligible to re-fetch."""
    raw = (entry or {}).get("fetched_at_ts") or (entry or {}).get("fetched_at")
    if not raw:
        return 1e9
    try:
        return (pd.Timestamp.now() - pd.Timestamp(raw)).total_seconds() / 3600.0
    except Exception:
        return 1e9


def load_prices_wide(
    tickers: List[str],
    start: Optional[str] = None,
    end: Optional[str] = None,
    field: str = "AdjClose",
    auto_fetch: bool = True,
    prices_dir: Optional[Path] = None,
) -> pd.DataFrame:
    """Return a wide (Date x ticker) DataFrame of ``field`` for the given tickers.

    Reads the isolated per-ticker parquet store (``prices_dir``, default = AEUS
    elec_strategy; pass a different dir for a separate universe such as SSRS sector
    ETFs).  When ``auto_fetch`` is True a ticker is (incrementally) fetched if it is
    MISSING, or if its cached store is STALE for ``end`` — i.e. the store's last date
    is before ``end`` and it has not been fetched within the last
    ``_REFETCH_THROTTLE_HOURS``.  Crucially this is time-based (not "fetched today"),
    so a pre-close fetch that only captured the prior close does NOT block a later
    after-close fetch of the actual close.  Mirrors SSRS ``loader.load_prices``
    (which auto-refreshes via ``cache_max_age_hours``); callers just load prices.
    """
    cols = {}
    meta = _load_meta(prices_dir)
    _end_d = pd.Timestamp(end).date() if end else None
    _changed = False
    for t in tickers:
        df = _read_existing(t, prices_dir)
        need = df is None
        if (not need) and auto_fetch and _end_d is not None and len(df):
            last = df.index[-1].date()
            # Re-fetch when we don't yet have ``end``'s data, throttled by time so
            # repeated loads (and genuinely-unpublished closes) don't hammer Polygon.
            if last < _end_d and _fetched_age_hours(meta.get(t)) >= _REFETCH_THROTTLE_HOURS:
                need = True
        if need and auto_fetch:
            df = update_ticker(t, start=start or DEFAULT_INIT_START, end=end,
                               meta=meta, prices_dir=prices_dir)
            _changed = True
        if df is None or field not in df.columns:
            log.warning("No %s data for %s", field, t)
            continue
        cols[t] = df[field]
    if _changed:
        _save_meta(meta, prices_dir)
    if not cols:
        return pd.DataFrame()
    wide = pd.DataFrame(cols).sort_index()
    if start:
        wide = wide.loc[pd.Timestamp(start):]
    if end:
        wide = wide.loc[:pd.Timestamp(end)]
    return wide


def first_available_dates(tickers: List[str]) -> Dict[str, date]:
    """{ticker: first cached date} from the parquet store (for IPO-aware weights)."""
    out: Dict[str, date] = {}
    for t in tickers:
        df = _read_existing(t)
        if df is not None and len(df):
            out[t] = df.index[0].date()
    return out


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify(start: str = "2019-01-01", end: Optional[str] = None) -> bool:
    """Print a coverage report; return True if all required tickers cover [start,end]."""
    end = end or date.today().isoformat()
    required = U.all_tickers(include_benchmark=True)
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    print("=" * 78)
    print(f"AEUS PRICE COVERAGE — required window {start} → {end}")
    print("=" * 78)
    ok_all = True
    for t in required:
        df = _read_existing(t)
        if df is None or len(df) == 0:
            print(f"  {t:6} MISSING")
            ok_all = False
            continue
        f, l = df.index[0], df.index[-1]
        # IPO names are allowed to start late; everything else must cover `start`
        is_ipo = t in U.IPO_DATES
        covers_start = (f <= start_ts) or is_ipo
        covers_end = l >= (end_ts - pd.Timedelta(days=6))
        nan_adj = int(df["AdjClose"].isna().sum()) if "AdjClose" in df else -1
        flag = "ok " if (covers_start and covers_end and nan_adj == 0) else "!! "
        if not (covers_start and covers_end and nan_adj == 0):
            ok_all = False
        print(f"  {flag}{t:6} {f.date()}→{l.date()}  rows={len(df):<5} "
              f"adjNaN={nan_adj}{'  (IPO)' if is_ipo else ''}")
    print("=" * 78)
    print("RESULT:", "ALL REQUIRED TICKERS OK" if ok_all else "SOME GAPS — see !! rows")
    return ok_all


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    ap = argparse.ArgumentParser(description="AEUS isolated price fetcher")
    ap.add_argument("--init", action="store_true", help="Full historical backfill")
    ap.add_argument("--update", action="store_true", help="Incremental daily update")
    ap.add_argument("--verify", action="store_true", help="Print coverage report")
    ap.add_argument("--force", action="store_true", help="Force full refetch")
    ap.add_argument("--start", default=DEFAULT_INIT_START, help="Init start date")
    ap.add_argument("--end", default=None, help="End date (default today)")
    ap.add_argument("--tickers", nargs="*", default=None, help="Subset of tickers")
    args = ap.parse_args()

    if args.verify and not (args.init or args.update):
        verify(start="2019-01-01", end=args.end)
        return

    if not (args.init or args.update):
        print("Nothing to do. Use --init / --update / --verify.")
        return

    start = args.start if args.init else DEFAULT_INIT_START
    res = update_all(tickers=args.tickers, start=start, end=args.end, force=args.force or args.init)
    n_ok = sum(1 for v in res.values() if v)
    print(f"\nFetched {n_ok}/{len(res)} tickers OK.")
    failed = [t for t, v in res.items() if not v]
    if failed:
        print("FAILED:", failed)
    verify(start="2019-01-01", end=args.end)


if __name__ == "__main__":
    main()
