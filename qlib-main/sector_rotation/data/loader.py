"""
Data Loader
===========
Dual-source price loader (MongoDB → yfinance fallback) plus FRED macro data.

Price data contract:
    Returns pd.DataFrame with DatetimeIndex, columns = tickers (str).
    Values are ADJUSTED close prices (total return, dividend-adjusted).
    No NaN in the returned window — gaps are forward-filled then backward-filled
    with a quality report printed for any gap > 1 business day.

Macro data contract:
    Returns pd.DataFrame with DatetimeIndex, columns = field names.
    Monthly data is forward-filled to daily frequency.
    All series are returned in their natural units (not normalized here).
"""

from __future__ import annotations

import io
import logging
import os
import pickle
import sys
import time
import warnings
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"


def load_config(path: Optional[Path] = None) -> dict:
    """Load YAML config from disk."""
    p = Path(path) if path else _DEFAULT_CONFIG_PATH
    with open(p, "r") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _cache_path(cache_dir: Path, key: str) -> Path:
    """Return the pickle cache path for a given key."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"{key}.pkl"


def _is_cache_fresh(path: Path, max_age_hours: float = 8.0) -> bool:
    """Return True if cache file exists and is newer than ``max_age_hours``."""
    if not path.exists():
        return False
    age_hours = (time.time() - path.stat().st_mtime) / 3600.0
    return age_hours < max_age_hours


def _load_cache(path: Path):
    with open(path, "rb") as f:
        return pickle.load(f)


def _save_cache(path: Path, obj):
    with open(path, "wb") as f:
        pickle.dump(obj, f)
    logger.debug(f"Saved cache: {path}")


# ---------------------------------------------------------------------------
# Price data loading
# ---------------------------------------------------------------------------

def _load_prices_yfinance(
    tickers: List[str],
    start: str,
    end: Optional[str],
    auto_adjust: bool = True,
) -> pd.DataFrame:
    """
    Download adjusted close prices from yfinance.

    Returns
    -------
    pd.DataFrame
        DatetimeIndex, columns = tickers, values = adjusted close prices.
    """
    import yfinance as yf

    logger.info(f"Downloading prices from yfinance: {tickers}, {start} → {end or 'today'}")
    raw = yf.download(
        tickers,
        start=start,
        end=end,
        auto_adjust=auto_adjust,
        progress=False,
    )

    # yfinance returns MultiIndex columns when multiple tickers
    if isinstance(raw.columns, pd.MultiIndex):
        closes = raw["Close"]
    else:
        # Single ticker: columns are OHLCV strings
        closes = raw[["Close"]].rename(columns={"Close": tickers[0]})

    closes.index = pd.to_datetime(closes.index)
    closes = closes[tickers] if set(tickers).issubset(set(closes.columns)) else closes
    return closes


def _load_prices_mongodb(
    tickers: List[str],
    start: str,
    end: Optional[str],
    host: str = "localhost",
    port: int = 27017,
    db: str = "market_data",
    collection: str = "prices",
) -> pd.DataFrame:
    """
    Load adjusted close prices from MongoDB (PriceDataStore schema).

    Expected document schema:
        {ticker: str, date: datetime, adj_close: float}

    Returns pd.DataFrame with DatetimeIndex.
    """
    try:
        from pymongo import MongoClient
    except ImportError:
        raise ImportError("pymongo is required for MongoDB price loading.")

    client = MongoClient(host, port, serverSelectionTimeoutMS=3000)
    try:
        client.server_info()  # Will raise if not reachable
    except Exception as e:
        raise ConnectionError(f"Cannot connect to MongoDB at {host}:{port}: {e}") from e

    db_conn = client[db]
    coll = db_conn[collection]

    start_dt = pd.Timestamp(start)
    end_dt = pd.Timestamp(end) if end else pd.Timestamp.now()

    query = {
        "ticker": {"$in": tickers},
        "date": {"$gte": start_dt.to_pydatetime(), "$lte": end_dt.to_pydatetime()},
    }
    projection = {"_id": 0, "ticker": 1, "date": 1, "adj_close": 1}

    docs = list(coll.find(query, projection))
    if not docs:
        raise ValueError(f"No MongoDB price data found for {tickers} from {start} to {end}")

    df = pd.DataFrame(docs)
    df["date"] = pd.to_datetime(df["date"])
    pivot = df.pivot(index="date", columns="ticker", values="adj_close")
    pivot.index.name = None
    return pivot[tickers]


def load_prices(
    tickers: List[str],
    start: str,
    end: Optional[str] = None,
    source: str = "yfinance",
    cache_dir: Optional[Path] = None,
    force_refresh: bool = False,
    cache_max_age_hours: float = 8.0,
    mongodb_cfg: Optional[dict] = None,
) -> pd.DataFrame:
    """
    Load adjusted close prices for ``tickers`` from ``start`` to ``end``.

    Tries ``source`` first; falls back to yfinance if MongoDB fails.

    Parameters
    ----------
    tickers : list of str
        ETF tickers to load.
    start : str
        Start date string (YYYY-MM-DD).
    end : str, optional
        End date string. Defaults to today.
    source : str
        Primary source: "yfinance" or "mongodb".
    cache_dir : Path, optional
        If provided, cache results to disk.
    force_refresh : bool
        Skip cache and re-download.
    cache_max_age_hours : float
        Cache expiry in hours.
    mongodb_cfg : dict, optional
        MongoDB connection params (host, port, db, collection).

    Returns
    -------
    pd.DataFrame
        DatetimeIndex, columns = tickers, adjusted close prices.
        Guaranteed to have no NaN after forward-fill + backward-fill.
    """
    tickers_sorted = sorted(tickers)  # Canonical order for cache key
    cache_key = f"prices_{source}_{'_'.join(tickers_sorted)}_{start}_{end or 'latest'}"

    if cache_dir:
        cp = _cache_path(cache_dir, cache_key)
        if not force_refresh and _is_cache_fresh(cp, cache_max_age_hours):
            logger.info(f"Loading prices from cache: {cp}")
            return _load_cache(cp)

    if source == "mongodb":
        try:
            cfg = mongodb_cfg or {}
            host = os.environ.get(cfg.get("host_env", "MONGO_HOST"), "localhost")
            prices = _load_prices_mongodb(
                tickers_sorted,
                start=start,
                end=end,
                host=host,
                port=cfg.get("port", 27017),
                db=cfg.get("db", "market_data"),
                collection=cfg.get("collection", "prices"),
            )
            logger.info("Prices loaded from MongoDB.")
        except Exception as e:
            logger.warning(f"MongoDB price load failed ({e}). Falling back to yfinance.")
            prices = _load_prices_yfinance(tickers_sorted, start=start, end=end)
    else:
        prices = _load_prices_yfinance(tickers_sorted, start=start, end=end)

    # Restore user-requested column order
    available = [t for t in tickers if t in prices.columns]
    missing = [t for t in tickers if t not in prices.columns]
    if missing:
        logger.warning(f"Tickers not found in price data: {missing}")
    prices = prices[available]

    prices = _validate_and_clean_prices(prices)

    if cache_dir:
        _save_cache(cp, prices)

    return prices


def _validate_and_clean_prices(df: pd.DataFrame) -> pd.DataFrame:
    """
    Quality-check and clean raw price data.

    Steps:
        1. Convert index to DatetimeIndex (UTC-naive, date-only)
        2. Remove duplicate dates (keep last)
        3. Sort ascending
        4. Report missing data gaps > 1 business day
        5. Forward-fill then backward-fill remaining NaN
        6. Drop rows where ALL values are NaN
        7. Assert no NaN remain in any column with ≥ 1 valid value
    """
    df = df.copy()
    df.index = pd.to_datetime(df.index).normalize()
    df = df[~df.index.duplicated(keep="last")].sort_index()

    # Report gaps
    for col in df.columns:
        series = df[col].dropna()
        if len(series) == 0:
            logger.warning(f"Column {col} has NO valid data.")
            continue
        # Gap detection (consecutive valid dates)
        valid_dates = series.index
        gaps = []
        for i in range(1, len(valid_dates)):
            bday_gap = np.busday_count(
                valid_dates[i - 1].date(), valid_dates[i].date()
            )
            if bday_gap > 1:
                gaps.append((valid_dates[i - 1].date(), valid_dates[i].date(), bday_gap))
        if gaps:
            logger.info(f"Price gaps in {col}: {gaps[:5]}{'...' if len(gaps) > 5 else ''}")

    # Fill NaN
    df = df.ffill().bfill()

    # Drop all-NaN rows
    df = df.dropna(how="all")

    # Final check
    nan_counts = df.isna().sum()
    if nan_counts.any():
        logger.warning(f"NaN remaining after fill:\n{nan_counts[nan_counts > 0]}")

    return df


def load_returns(prices: pd.DataFrame, method: str = "log") -> pd.DataFrame:
    """
    Compute daily returns from adjusted close prices.

    Parameters
    ----------
    prices : pd.DataFrame
        Adjusted close price DataFrame.
    method : str
        "log" for log returns, "pct" for simple percentage returns.

    Returns
    -------
    pd.DataFrame
        Daily returns, same shape as prices, first row is NaN (dropped).
    """
    if method == "log":
        returns = np.log(prices / prices.shift(1)).iloc[1:]
    elif method == "pct":
        returns = prices.pct_change().iloc[1:]
    else:
        raise ValueError(f"Unknown return method: {method}. Use 'log' or 'pct'.")
    return returns


def load_monthly_returns(prices: pd.DataFrame) -> pd.DataFrame:
    """
    Compute monthly returns from daily price data.
    Resampled to month-end, compounded from daily returns.

    Returns
    -------
    pd.DataFrame
        Monthly simple returns (end-of-month dates).
    """
    # Resample to month-end adjusted close, then compute monthly pct change
    monthly = prices.resample("ME").last()
    return monthly.pct_change().iloc[1:]


# ---------------------------------------------------------------------------
# Macro data loading (FRED)
# ---------------------------------------------------------------------------

FRED_SERIES: Dict[str, str] = {
    "vix":           "VIXCLS",           # CBOE Volatility Index (daily)
    "yield_curve":   "T10Y2Y",           # 10Y-2Y Treasury spread (daily)
    "hy_spread":     "BAMLH0A0HYM2",     # ICE BofA HY OAS (daily, bps)
    "breakeven_10y": "T10YIE",           # 10Y breakeven inflation rate (daily)
    "fed_rate":      "FEDFUNDS",         # Effective federal funds rate (monthly)
    # ISM Manufacturing PMI is proprietary (not available via free FRED API).
    # regime.py handles ism_mfg=NaN gracefully — omitted here.
}

# Some FRED series are monthly — mark them so we can forward-fill correctly
MONTHLY_SERIES: set = {"fed_rate"}


def _load_fred_series(
    field_name: str,
    series_id: str,
    start: str,
    end: Optional[str],
    api_key: str,
) -> pd.Series:
    """
    Download a single FRED series.  Returns a pd.Series with DatetimeIndex.
    """
    from fredapi import Fred

    fred = Fred(api_key=api_key)
    logger.debug(f"Fetching FRED series {series_id} for {field_name}")

    end_dt = end or datetime.today().strftime("%Y-%m-%d")
    data = fred.get_series(series_id, observation_start=start, observation_end=end_dt)
    data.index = pd.to_datetime(data.index).normalize()
    data.name = field_name
    return data


def load_macro_data(
    start: str,
    end: Optional[str] = None,
    api_key: Optional[str] = None,
    cache_dir: Optional[Path] = None,
    force_refresh: bool = False,
    cache_max_age_hours: float = 24.0,
) -> pd.DataFrame:
    """
    Load all macro indicators from FRED and return a daily-indexed DataFrame.

    Monthly series (ISM, Fed Funds) are forward-filled to daily frequency.
    Missing observations are filled: daily → ffill(limit=5), monthly → ffill(limit=31).

    Parameters
    ----------
    start : str
        Start date (YYYY-MM-DD).
    end : str, optional
        End date. Defaults to today.
    api_key : str, optional
        FRED API key. Falls back to FRED_API_KEY env var.
    cache_dir : Path, optional
        Disk cache directory.
    force_refresh : bool
        Skip cache.

    Returns
    -------
    pd.DataFrame
        DatetimeIndex, columns = field names from FRED_SERIES.
        HY spread in bps (original FRED units ×100).
    """
    cache_key = f"macro_{start}_{end or 'latest'}"

    if cache_dir:
        cp = _cache_path(cache_dir, cache_key)
        if not force_refresh and _is_cache_fresh(cp, cache_max_age_hours):
            logger.info(f"Loading macro data from cache: {cp}")
            return _load_cache(cp)

    # Resolve API key
    resolved_key = api_key or os.environ.get("FRED_API_KEY")
    if not resolved_key:
        raise ValueError(
            "FRED API key not provided. Set FRED_API_KEY environment variable "
            "or pass api_key= to load_macro_data()."
        )

    series_dict: Dict[str, pd.Series] = {}
    failed: List[str] = []
    for field_name, series_id in FRED_SERIES.items():
        try:
            s = _load_fred_series(field_name, series_id, start, end, resolved_key)
            series_dict[field_name] = s
        except Exception as e:
            logger.warning(f"Failed to load FRED series {series_id} ({field_name}): {e}")
            failed.append(field_name)

    if not series_dict:
        raise RuntimeError("All FRED series downloads failed. Check API key and connection.")

    if failed:
        logger.warning(f"Missing FRED series: {failed}. These fields will be NaN.")

    # Build common daily DatetimeIndex
    end_dt = pd.Timestamp(end) if end else pd.Timestamp.now().normalize()
    start_dt = pd.Timestamp(start)
    idx = pd.date_range(start=start_dt, end=end_dt, freq="B")  # Business days

    result = pd.DataFrame(index=idx)
    for field_name, s in series_dict.items():
        aligned = s.reindex(idx)
        if field_name in MONTHLY_SERIES:
            # Monthly data: forward-fill up to 31 days
            aligned = aligned.ffill(limit=31)
        else:
            # Daily data: forward-fill up to 5 days (handle weekends/holidays)
            aligned = aligned.ffill(limit=5)
        result[field_name] = aligned

    # Add missing fields as NaN columns so the schema is always consistent
    for field_name in FRED_SERIES:
        if field_name not in result.columns:
            result[field_name] = np.nan

    # FRED HY spread is in percentage points (e.g., 3.5 = 350 bps)
    # Convert to basis points for easier threshold comparisons
    if "hy_spread" in result.columns:
        result["hy_spread"] = result["hy_spread"] * 100  # pct → bps

    # Log data quality
    nan_pct = result.isna().mean() * 100
    for col in result.columns:
        if nan_pct[col] > 5:
            logger.warning(f"Macro {col}: {nan_pct[col]:.1f}% NaN after fill")
        else:
            logger.debug(f"Macro {col}: {nan_pct[col]:.1f}% NaN after fill")

    if cache_dir:
        _save_cache(cp, result)

    return result


# ---------------------------------------------------------------------------
# Data Quality Report
# ---------------------------------------------------------------------------

def data_quality_report(prices: pd.DataFrame, macro: pd.DataFrame) -> str:
    """
    Generate a human-readable data quality summary.

    Returns a multi-line string with:
    - Price data: date range, NaN%, gap count, return statistics
    - Macro data: date range, NaN% per series, last known value
    """
    lines = []
    lines.append("=" * 60)
    lines.append("DATA QUALITY REPORT")
    lines.append("=" * 60)

    # Price section
    lines.append("\n--- Price Data ---")
    lines.append(f"Date range : {prices.index[0].date()} → {prices.index[-1].date()}")
    lines.append(f"# Trading days: {len(prices)}")
    lines.append(f"# Tickers: {len(prices.columns)}")
    for col in prices.columns:
        nan_pct = prices[col].isna().mean() * 100
        first_valid = prices[col].first_valid_index()
        last_valid = prices[col].last_valid_index()
        lines.append(
            f"  {col:<6}: NaN={nan_pct:.1f}%  valid={first_valid.date()}→{last_valid.date()}"
        )

    # Daily return stats
    rets = prices.pct_change().iloc[1:]
    lines.append("\nDaily return statistics:")
    for col in prices.columns:
        r = rets[col].dropna()
        lines.append(
            f"  {col:<6}: mean={r.mean()*252:.2%}/yr  vol={r.std()*np.sqrt(252):.2%}/yr  "
            f"skew={r.skew():.2f}  kurt={r.kurtosis():.2f}"
        )

    # Macro section
    lines.append("\n--- Macro Data ---")
    lines.append(f"Date range : {macro.index[0].date()} → {macro.index[-1].date()}")
    for col in macro.columns:
        nan_pct = macro[col].isna().mean() * 100
        last_val = macro[col].dropna().iloc[-1] if not macro[col].dropna().empty else float("nan")
        lines.append(f"  {col:<15}: NaN={nan_pct:.1f}%  last={last_val:.4g}")

    lines.append("=" * 60)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Convenience: load everything at once
# ---------------------------------------------------------------------------

def load_all(
    config: Optional[dict] = None,
    config_path: Optional[Path] = None,
    force_refresh: bool = False,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Load price and macro data according to config.yaml settings.

    Parameters
    ----------
    config : dict, optional
        Pre-loaded config dict.  If None, loaded from config_path or default.
    config_path : Path, optional
        Path to config.yaml.
    force_refresh : bool
        Skip all caches and re-download.

    Returns
    -------
    prices : pd.DataFrame
        Adjusted close prices (ETFs + benchmark).
    macro : pd.DataFrame
        Macro indicators (daily, forward-filled).
    """
    cfg = config or load_config(config_path)

    data_cfg = cfg["data"]
    universe_cfg = cfg["universe"]

    # Resolve settings
    source = data_cfg.get("price_source", "yfinance")
    start = data_cfg.get("price_start", "2017-01-01")
    end = data_cfg.get("price_end")
    cache_dir = Path(data_cfg.get("cache_dir", "sector_rotation/data/cache"))
    fred_env = data_cfg.get("fred_api_key_env", "FRED_API_KEY")
    fred_key = os.environ.get(fred_env)
    mongo_cfg = data_cfg.get("mongodb", {})

    tickers = universe_cfg["etfs"] + [universe_cfg["benchmark"]]

    prices = load_prices(
        tickers=tickers,
        start=start,
        end=end,
        source=source,
        cache_dir=cache_dir,
        force_refresh=force_refresh,
        mongodb_cfg=mongo_cfg,
    )

    macro = load_macro_data(
        start=start,
        end=end,
        api_key=fred_key,
        cache_dir=cache_dir,
        force_refresh=force_refresh,
    )

    return prices, macro


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    import argparse

    parser = argparse.ArgumentParser(description="Load sector rotation data")
    parser.add_argument("--force-refresh", action="store_true",
                        help="Skip cache and re-download all data")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    args = parser.parse_args()

    prices, macro = load_all(
        config_path=Path(args.config) if args.config else None,
        force_refresh=args.force_refresh,
    )

    print(data_quality_report(prices, macro))
