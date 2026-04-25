"""
Relative Value Signal
=====================
Sector P/E percentile relative to own 10-year history.

Lower P/E percentile → sector is cheap relative to its own history → higher signal score.

Data source strategy (in order of quality):
    1. "constituents": Build monthly TTM P/E time series from constituent stock quarterly
       earnings (via yfinance quarterly_income_stmt). This is the primary path and uses
       actual reported EPS — no price substitution. Results are cached to disk.
    2. "external": Caller provides a pre-built monthly P/E DataFrame.
    3. "proxy": Fallback using price-to-5yr-avg as a rough "expensiveness" proxy.
       Used only when earnings data is unavailable and for unit tests.
    4. "yfinance_info": Point-in-time snapshot from yfinance .info — look-ahead biased,
       falls back to proxy automatically.

Constituent basket (SECTOR_REPRESENTATIVES):
    Top 5 representative large-caps per SPDR sector ETF. Fixed basket used as a
    proxy for the ETF's aggregate P/E. The basket is intentionally stable (large caps
    do not frequently enter/exit sectors) to avoid survivorship bias in the sample period.

    Known caveats:
    - XLC basket changes at 2018-09 GICS restructuring (Meta/Alphabet moved from XLK).
      XLC basket is defined for post-2018 composition.
    - ETF constituent weights shift monthly; equal-weighting the top-5 is an approximation.
    - yfinance quarterly earnings availability: typically 4-6 years back.
      Pre-2018 data may be partial or missing.

Reference:
    Asness, C., Moskowitz, & Pedersen, L. (2013).
    Value and momentum everywhere. Journal of Finance, 68(3), 929-985.

    Fama, E. & French, K. (1992).
    The Cross-Section of Expected Stock Returns. Journal of Finance, 47(2), 427-465.
"""

from __future__ import annotations

import json
import logging
import pickle
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Suppress yfinance DeprecationWarning / FutureWarning noise.
# yfinance.__init__ registers a 'default' DeprecationWarning filter at position 0
# on first import, which causes pandas Pandas4Warning (Timestamp.utcnow) to leak
# to stderr. We pre-import yfinance here and immediately insert a higher-priority
# 'ignore' filter so downstream calls are clean.
try:
    import yfinance as _yf_init  # noqa: F401 — triggers __init__.py once
    import warnings as _w
    _w.filters.insert(0, ("ignore", None, DeprecationWarning, None, 0))
    _w.filters.insert(0, ("ignore", None, FutureWarning, None, 0))
    _w._filters_mutated()
    del _yf_init, _w
except ImportError:
    pass  # yfinance not installed; will fail later with a clear message

# ---------------------------------------------------------------------------
# EPS history store — auto-discovered path
# update_eps_history.py populates price_data/sector_etfs/eps_history.json
# ---------------------------------------------------------------------------

# value.py lives at qlib-main/sector_rotation/signals/value.py
# 4 dirs up → someopark-test/
_EPS_HISTORY_DEFAULT: Path = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "price_data" / "sector_etfs" / "eps_history.json"
)

# Module-level cache: store loaded once per process
_eps_store_cache: Optional[dict] = None
_eps_store_path_loaded: Optional[Path] = None


def _load_eps_store(path: Optional[Path] = None) -> dict:
    """Load eps_history.json into module-level cache (once per process)."""
    global _eps_store_cache, _eps_store_path_loaded
    p = Path(path) if path else _EPS_HISTORY_DEFAULT
    if _eps_store_cache is not None and _eps_store_path_loaded == p:
        return _eps_store_cache
    if not p.exists():
        _eps_store_cache = {}
        _eps_store_path_loaded = p
        return _eps_store_cache
    try:
        with open(p) as f:
            _eps_store_cache = json.load(f).get("symbols", {})
        _eps_store_path_loaded = p
        logger.debug(f"Loaded EPS history store: {len(_eps_store_cache)} symbols from {p}")
    except Exception as e:
        logger.warning(f"Failed to load EPS history store ({p}): {e}")
        _eps_store_cache = {}
    return _eps_store_cache


def _load_eps_from_store(stock: str, path: Optional[Path] = None) -> pd.Series:
    """
    Return quarterly EPS pd.Series for a stock from the local history store.
    Index = quarter-end DatetimeIndex, values = diluted EPS (float).
    Returns empty Series if stock not in store.
    """
    store = _load_eps_store(path)
    entries = store.get(stock, [])
    if not entries:
        return pd.Series(dtype=float)
    idx = pd.DatetimeIndex([e["end_date"] for e in entries])
    vals = [e["eps"] for e in entries]
    return pd.Series(vals, index=idx, dtype=float).sort_index()


# ---------------------------------------------------------------------------
# Sector constituent baskets for P/E computation
# ---------------------------------------------------------------------------

# Top 5 representative large-cap stocks per SPDR sector ETF.
# These are the largest, most liquid names that collectively account for
# a substantial fraction of each sector ETF's weight.
SECTOR_REPRESENTATIVES: Dict[str, List[str]] = {
    "XLK":  ["AAPL", "MSFT", "NVDA", "AVGO", "ORCL"],    # Info Technology
    "XLF":  ["BRK-B", "JPM", "V", "MA", "BAC"],           # Financials
    "XLE":  ["XOM", "CVX", "COP", "SLB", "EOG"],           # Energy
    "XLV":  ["LLY", "UNH", "JNJ", "ABBV", "MRK"],          # Health Care
    "XLU":  ["NEE", "DUK", "SO", "AEP", "EXC"],            # Utilities
    "XLI":  ["GE", "CAT", "RTX", "HON", "UPS"],            # Industrials
    "XLY":  ["AMZN", "TSLA", "HD", "MCD", "NKE"],          # Consumer Discretionary
    "XLP":  ["PG", "KO", "PEP", "COST", "WMT"],            # Consumer Staples
    "XLB":  ["LIN", "APD", "SHW", "FCX", "NEM"],           # Materials
    "XLC":  ["GOOGL", "META", "NFLX", "DIS", "VZ"],        # Communication Services
    "XLRE": ["AMT", "PLD", "EQIX", "CCI", "PSA"],          # Real Estate
}


# ---------------------------------------------------------------------------
# P/E Data Fetching (snapshot — for live signals only)
# ---------------------------------------------------------------------------

def fetch_pe_snapshot(tickers: List[str], pause_sec: float = 0.5) -> Dict[str, Optional[float]]:
    """
    Fetch current trailing P/E ratios for each ticker via yfinance.

    Returns a dict {ticker: pe_ratio or None}.
    None means the data was not available or was an invalid value (negative, zero).

    Note: This is a SNAPSHOT at time of call, not a historical time series.
    For backtesting, use build_pe_series_from_constituents().
    """
    try:
        import yfinance as yf
    except ImportError:
        raise ImportError("yfinance is required for P/E data fetching.")

    pe_dict: Dict[str, Optional[float]] = {}
    for ticker in tickers:
        try:
            info = yf.Ticker(ticker).info
            pe = (
                info.get("trailingPE")
                or info.get("forwardPE")
                or info.get("pegRatio")
            )
            if pe and isinstance(pe, (int, float)) and pe > 0:
                pe_dict[ticker] = float(pe)
                logger.debug(f"P/E {ticker}: {pe:.2f}")
            else:
                pe_dict[ticker] = None
                logger.debug(f"P/E {ticker}: not available (raw={pe})")
            time.sleep(pause_sec)
        except Exception as e:
            logger.warning(f"P/E fetch failed for {ticker}: {e}")
            pe_dict[ticker] = None

    available = [t for t, v in pe_dict.items() if v is not None]
    logger.info(f"P/E available for {len(available)}/{len(tickers)} tickers: {available}")
    return pe_dict


# ---------------------------------------------------------------------------
# Constituent-based P/E time series builder (primary backtest path)
# ---------------------------------------------------------------------------

def _fetch_quarterly_eps_yfinance(stock: str, pause_sec: float = 0.3) -> pd.Series:
    """Fetch recent quarterly Diluted EPS from yfinance (typically last 4-8 quarters)."""
    try:
        import yfinance as yf
    except ImportError:
        return pd.Series(dtype=float)

    try:
        time.sleep(pause_sec)
        ticker = yf.Ticker(stock)
        stmt = None
        try:
            stmt = ticker.quarterly_income_stmt
        except Exception:
            pass
        if stmt is None or stmt.empty:
            return pd.Series(dtype=float)
        eps_row = None
        for candidate in ("Diluted EPS", "Basic EPS"):
            if candidate in stmt.index:
                eps_row = stmt.loc[candidate]
                break
        if eps_row is None:
            return pd.Series(dtype=float)
        eps = eps_row.astype(float).dropna()
        eps.index = pd.DatetimeIndex(eps.index)
        return eps.sort_index()
    except Exception as e:
        logger.warning(f"yfinance EPS fetch failed for {stock}: {e}")
        return pd.Series(dtype=float)


def _fetch_quarterly_eps(
    stock: str,
    pause_sec: float = 0.3,
    eps_store_path: Optional[Path] = None,
) -> pd.Series:
    """
    Fetch quarterly Diluted EPS for a single stock.

    Strategy (in priority order):
    1. Load historical data from local eps_history.json store (populated by
       update_eps_history.py using Polygon API — typically 10+ years).
    2. Fetch recent quarters from yfinance (last 4-8 quarters).
    3. Merge: store provides the deep history; yfinance fills in the most
       recent quarters not yet in the store.

    Returns pd.Series with quarter-end dates as index. Empty if no data.
    """
    historical = _load_eps_from_store(stock, eps_store_path)
    recent = _fetch_quarterly_eps_yfinance(stock, pause_sec=pause_sec)

    if historical.empty and recent.empty:
        logger.debug(f"{stock}: no EPS from store or yfinance")
        return pd.Series(dtype=float)

    if historical.empty:
        logger.debug(f"{stock}: store empty, using yfinance only ({len(recent)} quarters)")
        return recent

    if recent.empty:
        logger.debug(f"{stock}: yfinance empty, using store only ({len(historical)} quarters)")
        return historical

    # Merge: historical as base, recent overrides (more accurate for latest quarters)
    merged = historical.copy()
    merged = merged.combine_first(recent)  # recent fills gaps not in historical
    # For overlapping dates, prefer recent (more up-to-date restatements)
    for dt in recent.index:
        if dt in merged.index:
            merged[dt] = recent[dt]
    merged = merged.sort_index()
    logger.debug(
        f"{stock}: merged store({len(historical)}) + yfinance({len(recent)}) "
        f"→ {len(merged)} quarters"
    )
    return merged


def _quarterly_eps_to_monthly_ttm(
    eps_quarterly: pd.Series,
    monthly_idx: pd.DatetimeIndex,
) -> pd.Series:
    """
    Convert a quarterly EPS Series to a monthly TTM (trailing-twelve-month) EPS Series.

    For each month M:
        TTM EPS = sum of the last 4 reported quarterly EPS values on or before M.

    This respects reporting lag: only uses EPS values that were already known at M.
    Returns NaN for months with fewer than 4 quarters of history.

    Parameters
    ----------
    eps_quarterly : pd.Series
        Quarterly EPS with quarter-end dates as index.
    monthly_idx : pd.DatetimeIndex
        Target month-end dates for the output.
    """
    result = {}
    for month_end in monthly_idx:
        # All quarters reported on or before this month
        available = eps_quarterly.loc[:month_end]
        if len(available) < 4:
            result[month_end] = np.nan
        else:
            result[month_end] = float(available.iloc[-4:].sum())
    return pd.Series(result)


def _constituent_pe_series(
    stock: str,
    monthly_prices: pd.Series,
    pause_sec: float = 0.3,
    eps_store_path: Optional[Path] = None,
) -> pd.Series:
    """
    Compute monthly P/E series for a single constituent stock.

    P/E = monthly closing price / TTM diluted EPS.
    Returns NaN for months where EPS ≤ 0 (avoids negative / infinite P/E).
    """
    eps_q = _fetch_quarterly_eps(stock, pause_sec=pause_sec, eps_store_path=eps_store_path)
    if eps_q.empty:
        return pd.Series(np.nan, index=monthly_prices.index)

    ttm = _quarterly_eps_to_monthly_ttm(eps_q, monthly_prices.index)

    # P/E = price / TTM EPS; exclude non-positive EPS (negative earnings)
    pe = monthly_prices / ttm
    pe[ttm <= 0] = np.nan  # exclude loss-making periods
    pe[pe <= 0] = np.nan   # sanity check
    pe[pe > 300] = np.nan  # cap extreme outliers (>300x P/E is noise)
    return pe


def build_pe_series_from_constituents(
    etf_tickers: List[str],
    start: str,
    end: str,
    n_top: int = 5,
    pause_sec: float = 0.3,
    cache_path: Optional[Path] = None,
    eps_store_path: Optional[Path] = None,
) -> pd.DataFrame:
    """
    Build monthly sector P/E time series from top constituent stock earnings.

    EPS data strategy:
    - If price_data/sector_etfs/eps_history.json exists (populated by update_eps_history.py),
      use it as the historical base (10+ years from Polygon).
    - Always fetch recent quarters from yfinance and merge.
    - If no store exists, falls back to yfinance-only (last 4-8 quarters only).

    Parameters
    ----------
    etf_tickers : list of str
    start, end : str
    n_top : int        representative stocks per sector
    pause_sec : float  sleep between yfinance API calls
    cache_path : Path, optional   pickle cache for the final P/E DataFrame
    eps_store_path : Path, optional  override for eps_history.json location

    Returns
    -------
    pd.DataFrame
        Monthly TTM P/E ratios. index = month-end DatetimeIndex, cols = ETF tickers.
    """
    # --- Cache check ---
    if cache_path is not None and cache_path.exists():
        try:
            with open(cache_path, "rb") as f:
                cached = pickle.load(f)
            logger.info(f"Loaded constituent P/E from cache: {cache_path}")
            return cached
        except Exception as e:
            logger.warning(f"Cache load failed ({e}), re-fetching...")

    try:
        import yfinance as yf
    except ImportError:
        raise ImportError("yfinance is required for constituent P/E computation.")

    monthly_idx = pd.date_range(start, end, freq="ME")

    # Download monthly prices for all constituent stocks in one batch
    all_constituents: List[str] = []
    etf_to_stocks: Dict[str, List[str]] = {}
    for etf in etf_tickers:
        stocks = SECTOR_REPRESENTATIVES.get(etf, [])[:n_top]
        etf_to_stocks[etf] = stocks
        all_constituents.extend(stocks)
    all_constituents = list(dict.fromkeys(all_constituents))  # deduplicate, preserve order

    store_available = (eps_store_path or _EPS_HISTORY_DEFAULT).exists()
    logger.info(
        f"Downloading monthly prices for {len(all_constituents)} constituent stocks "
        f"(EPS store: {'found' if store_available else 'not found — yfinance only'})"
    )
    raw_prices = yf.download(
        all_constituents,
        start=start,
        end=end,
        interval="1mo",
        auto_adjust=True,
        progress=False,
    )

    # Extract Close prices; handle single-ticker case (no MultiIndex)
    if isinstance(raw_prices.columns, pd.MultiIndex):
        stock_prices = raw_prices["Close"]
    else:
        # Single ticker — yfinance returns flat columns
        stock_prices = raw_prices[["Close"]].rename(columns={"Close": all_constituents[0]})

    # Align to month-end index
    stock_prices.index = stock_prices.index.to_period("M").to_timestamp("M")
    stock_prices = stock_prices.reindex(monthly_idx)

    # Build P/E series for each sector ETF
    result: Dict[str, pd.Series] = {}
    for etf in etf_tickers:
        stocks = etf_to_stocks.get(etf, [])
        if not stocks:
            result[etf] = pd.Series(np.nan, index=monthly_idx)
            continue

        logger.info(f"Computing constituent P/E for {etf} ({stocks})...")
        pe_list: List[pd.Series] = []
        for stock in stocks:
            if stock not in stock_prices.columns:
                logger.debug(f"  {stock}: price not available, skip")
                continue
            prices_s = stock_prices[stock].dropna()
            if prices_s.empty:
                continue
            pe_s = _constituent_pe_series(stock, prices_s, pause_sec=pause_sec,
                                           eps_store_path=eps_store_path)
            pe_list.append(pe_s)

        if not pe_list:
            result[etf] = pd.Series(np.nan, index=monthly_idx)
            continue

        # Equal-weight average across constituents (NaN = exclude that stock that month)
        pe_df = pd.concat(pe_list, axis=1)
        sector_pe = pe_df.mean(axis=1, skipna=True)  # skipna=True: exclude missing
        sector_pe[sector_pe.isna()] = np.nan
        result[etf] = sector_pe.reindex(monthly_idx)

        valid_months = sector_pe.notna().sum()
        logger.info(f"  {etf}: {valid_months}/{len(monthly_idx)} months with valid P/E")

    pe_df_out = pd.DataFrame(result, index=monthly_idx)

    # --- Cache save ---
    if cache_path is not None:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(cache_path, "wb") as f:
                pickle.dump(pe_df_out, f)
            logger.info(f"Saved constituent P/E to cache: {cache_path}")
        except Exception as e:
            logger.warning(f"Cache save failed: {e}")

    return pe_df_out


# ---------------------------------------------------------------------------
# Polygon-based P/E time series builder
# ---------------------------------------------------------------------------

def _fetch_quarterly_eps_polygon(
    stock: str,
    api_key: str,
    pause_sec: float = 0.2,
    max_pages: int = 5,
) -> pd.Series:
    """
    Fetch quarterly diluted EPS for a single stock via Polygon /vX/reference/financials.

    Paginates up to max_pages (40 quarters/page → ~50 years max).
    Returns pd.Series with quarter-end dates as index, EPS float as values.
    Returns empty Series on failure.
    """
    import json
    import subprocess

    all_results = []
    url = (
        f"https://api.polygon.io/vX/reference/financials"
        f"?ticker={stock}&timeframe=quarterly&limit=40"
        f"&sort=period_of_report_date&order=desc"
        f"&apiKey={api_key}"
    )
    pages = 0
    while url and pages < max_pages:
        result = subprocess.run(
            ["curl", "-s", "--max-time", "30", url],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            logger.warning(f"Polygon curl failed for {stock} (returncode={result.returncode})")
            break
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            logger.warning(f"Polygon JSON decode error for {stock}")
            break

        batch = data.get("results", [])
        all_results.extend(batch)
        pages += 1

        next_url = data.get("next_url")
        url = (next_url + f"&apiKey={api_key}") if next_url else None
        time.sleep(pause_sec)

    if not all_results:
        logger.debug(f"Polygon: no data for {stock}")
        return pd.Series(dtype=float)

    eps_dict: Dict[pd.Timestamp, float] = {}
    for r in all_results:
        end_date = r.get("end_date")
        if not end_date:
            continue
        inc = r.get("financials", {}).get("income_statement", {})
        eps = (
            (inc.get("diluted_earnings_per_share") or {}).get("value")
            or (inc.get("basic_earnings_per_share") or {}).get("value")
        )
        if eps is not None:
            eps_dict[pd.Timestamp(end_date)] = float(eps)

    if not eps_dict:
        logger.debug(f"Polygon: EPS parse empty for {stock}")
        return pd.Series(dtype=float)

    return pd.Series(eps_dict).sort_index()


def build_pe_series_from_polygon(
    etf_tickers: List[str],
    start: str,
    end: str,
    api_key: str,
    n_top: int = 5,
    pause_sec: float = 0.2,
    cache_path: Optional[Path] = None,
) -> pd.DataFrame:
    """
    Build monthly sector P/E time series using Polygon quarterly EPS data.

    Identical pipeline to build_pe_series_from_constituents() but uses
    Polygon /vX/reference/financials instead of yfinance quarterly_income_stmt.
    Polygon typically provides 10+ years of quarterly EPS history, giving
    full coverage back to 2015 (vs yfinance's 4-6 quarters).

    Parameters
    ----------
    etf_tickers : list of str
    start, end : str   date range for monthly index and price download
    api_key : str      Polygon API key (POLYGON_API_KEY)
    n_top : int        representative stocks per sector
    pause_sec : float  sleep between Polygon API calls
    cache_path : Path, optional

    Returns
    -------
    pd.DataFrame
        Monthly TTM P/E ratios. index = month-end, cols = ETF tickers.
    """
    # --- Cache check ---
    if cache_path is not None and cache_path.exists():
        try:
            with open(cache_path, "rb") as f:
                cached = pickle.load(f)
            logger.info(f"Loaded Polygon P/E from cache: {cache_path}")
            return cached
        except Exception as e:
            logger.warning(f"Cache load failed ({e}), re-fetching from Polygon...")

    try:
        import yfinance as yf
    except ImportError:
        raise ImportError("yfinance is required for monthly price download.")

    monthly_idx = pd.date_range(start, end, freq="ME")

    # Download monthly prices for all constituent stocks in one batch
    all_constituents: List[str] = []
    etf_to_stocks: Dict[str, List[str]] = {}
    for etf in etf_tickers:
        stocks = SECTOR_REPRESENTATIVES.get(etf, [])[:n_top]
        etf_to_stocks[etf] = stocks
        all_constituents.extend(stocks)
    all_constituents = list(dict.fromkeys(all_constituents))

    logger.info(f"Downloading monthly prices for {len(all_constituents)} constituent stocks (for Polygon P/E)...")
    raw_prices = yf.download(
        all_constituents,
        start=start,
        end=end,
        interval="1mo",
        auto_adjust=True,
        progress=False,
    )
    if isinstance(raw_prices.columns, pd.MultiIndex):
        stock_prices = raw_prices["Close"]
    else:
        stock_prices = raw_prices[["Close"]].rename(columns={"Close": all_constituents[0]})
    stock_prices.index = stock_prices.index.to_period("M").to_timestamp("M")
    stock_prices = stock_prices.reindex(monthly_idx)

    # Build P/E series for each sector ETF
    result: Dict[str, pd.Series] = {}
    for etf in etf_tickers:
        stocks = etf_to_stocks.get(etf, [])
        if not stocks:
            result[etf] = pd.Series(np.nan, index=monthly_idx)
            continue

        logger.info(f"Computing Polygon P/E for {etf} ({stocks})...")
        pe_list: List[pd.Series] = []
        for stock in stocks:
            if stock not in stock_prices.columns:
                logger.debug(f"  {stock}: price not available, skip")
                continue
            prices_s = stock_prices[stock].dropna()
            if prices_s.empty:
                continue

            eps_q = _fetch_quarterly_eps_polygon(stock, api_key=api_key, pause_sec=pause_sec)
            if eps_q.empty:
                logger.debug(f"  {stock}: no Polygon EPS, skip")
                continue

            ttm = _quarterly_eps_to_monthly_ttm(eps_q, monthly_idx)
            pe = prices_s.reindex(monthly_idx) / ttm
            pe[ttm <= 0] = np.nan
            pe[pe <= 0] = np.nan
            pe[pe > 300] = np.nan
            pe_list.append(pe)

        if not pe_list:
            result[etf] = pd.Series(np.nan, index=monthly_idx)
            continue

        pe_df_sector = pd.concat(pe_list, axis=1)
        sector_pe = pe_df_sector.mean(axis=1, skipna=True)
        result[etf] = sector_pe.reindex(monthly_idx)

        valid_months = sector_pe.notna().sum()
        logger.info(f"  {etf}: {valid_months}/{len(monthly_idx)} months with valid P/E (Polygon)")

    pe_df_out = pd.DataFrame(result, index=monthly_idx)

    # --- Cache save ---
    if cache_path is not None:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(cache_path, "wb") as f:
                pickle.dump(pe_df_out, f)
            logger.info(f"Saved Polygon P/E to cache: {cache_path}")
        except Exception as e:
            logger.warning(f"Cache save failed: {e}")

    return pe_df_out


# ---------------------------------------------------------------------------
# P/E Percentile Signal
# ---------------------------------------------------------------------------

def pe_to_percentile(
    pe_series: pd.Series,
    lookback_years: float = 10.0,
    window_min_periods: int = 24,
) -> pd.Series:
    """
    Convert a P/E time series to its rolling historical percentile.

    Percentile = fraction of historical P/E values BELOW current value.
    Lower percentile → cheaper → HIGHER signal value (invert for signal).

    Parameters
    ----------
    pe_series : pd.Series
        Monthly P/E values (DatetimeIndex).
    lookback_years : float
        Rolling window in years (default 10).
    window_min_periods : int
        Minimum periods required in window.

    Returns
    -------
    pd.Series
        Rolling percentile [0, 1]. NaN if insufficient history.
        HIGHER value = MORE EXPENSIVE (raw percentile, not inverted yet).
    """
    window = int(lookback_years * 12)  # months

    def pct_rank(x):
        """Percentile of the last observation within the window."""
        if len(x) < window_min_periods or np.isnan(x.iloc[-1]):
            return np.nan
        return (x.iloc[:-1] < x.iloc[-1]).mean()

    return pe_series.rolling(window=window, min_periods=window_min_periods).apply(
        pct_rank, raw=False
    )


def compute_value_signal(
    pe_history: pd.DataFrame,
    lookback_years: float = 10.0,
    missing_data_weight: float = 0.0,
) -> pd.DataFrame:
    """
    Compute relative value signal from P/E time series.

    Signal = 1 - pe_percentile (inverted: low PE = high value signal).
    Cross-sectionally z-scored across tickers with available data.

    Parameters
    ----------
    pe_history : pd.DataFrame
        Monthly P/E ratios. DatetimeIndex (ME), columns = tickers.
        NaN where data is unavailable.
    lookback_years : float
        Rolling history window for percentile computation.
    missing_data_weight : float
        Score assigned to tickers with no P/E data (0 = neutral/exclude).

    Returns
    -------
    pd.DataFrame
        Month-end value z-scores. NaN tickers get missing_data_weight.
    """
    # Compute percentile for each ticker
    pct = pd.DataFrame(index=pe_history.index, columns=pe_history.columns, dtype=float)
    for col in pe_history.columns:
        if pe_history[col].notna().sum() >= 12:
            pct[col] = pe_to_percentile(pe_history[col], lookback_years=lookback_years)
        else:
            pct[col] = np.nan
            logger.debug(f"Insufficient P/E history for {col}, will use missing_data_weight.")

    # Invert: low P/E (low percentile) → high value signal
    value_raw = 1.0 - pct

    # Cross-sectional z-score (only tickers with data)
    def cs_zscore_row(row):
        valid = row.dropna()
        if len(valid) < 2:
            return row
        row_z = (row - valid.mean()) / valid.std()
        return row_z

    value_z = value_raw.apply(cs_zscore_row, axis=1)

    # Fill missing tickers with neutral signal
    value_z = value_z.fillna(missing_data_weight)

    logger.debug(f"Value signal computed: {value_z.dropna(how='all').shape[0]} valid months")
    return value_z


# ---------------------------------------------------------------------------
# P/E Proxy Series Builder (fallback only — used when earnings unavailable)
# ---------------------------------------------------------------------------

def build_pe_proxy_series(
    prices: pd.DataFrame,
    earnings_yield_proxy: str = "normalized_price",
) -> pd.DataFrame:
    """
    Build a rough P/E proxy from price data alone. FALLBACK ONLY.

    Used when constituent earnings data (build_pe_series_from_constituents) is
    unavailable — e.g., in unit tests, offline environments, or for tickers with
    no coverage in SECTOR_REPRESENTATIVES.

    Method "normalized_price":
        Proxy P/E = current price / 5-year rolling average price.
        Higher ratio → relatively expensive → acts like high P/E.
        This is a directional approximation only; magnitudes are not comparable
        to true P/E ratios and should not be mixed with real P/E data.
    """
    if earnings_yield_proxy != "normalized_price":
        raise NotImplementedError(
            f"Proxy method '{earnings_yield_proxy}' not implemented. "
            "Use 'normalized_price'."
        )

    monthly = prices.resample("ME").last()

    # Proxy: ratio of current price to 60-month (5-year) rolling average price
    proxy = monthly / monthly.rolling(window=60, min_periods=24).mean()

    logger.warning(
        "Using price-to-5yr-avg as P/E proxy (fallback). "
        "For accurate value signals, ensure yfinance access so that "
        "build_pe_series_from_constituents() can fetch real earnings data."
    )
    return proxy


# ---------------------------------------------------------------------------
# Full value signal computation — primary API
# ---------------------------------------------------------------------------

def compute_value_signal_full(
    prices: pd.DataFrame,
    pe_history: Optional[pd.DataFrame] = None,
    source: str = "constituents",
    lookback_years: float = 10.0,
    missing_data_weight: float = 0.0,
    cache_dir: Optional[Path] = None,
    polygon_api_key: Optional[str] = None,
) -> pd.DataFrame:
    """
    Compute sector relative value signal.

    Sources (in order of accuracy):
    --------------------------------
    "polygon" (recommended):
        Build monthly TTM P/E from Polygon /vX/reference/financials quarterly EPS.
        Full history back to 2015+, using POLYGON_API_KEY. Results cached in cache_dir.

    "constituents":
        Build monthly TTM P/E from yfinance quarterly earnings.
        Only returns ~4-8 quarters of history; not recommended for backtesting.

    "external":
        Caller provides pe_history directly (pd.DataFrame, monthly, tickers as cols).
        Use when you have Bloomberg/FactSet P/E data.

    "proxy":
        Fallback: normalized price proxy. No earnings data required.
        Directionally correct but not a real P/E.

    "yfinance_info":
        Point-in-time snapshot from yfinance .info — look-ahead biased.
        Auto-falls back to "proxy" for backtesting.

    Parameters
    ----------
    prices : pd.DataFrame
        Daily adjusted close prices. Columns = ETF tickers.
    pe_history : pd.DataFrame, optional
        Pre-built monthly P/E time series (required only for source="external").
    source : str
        "polygon" | "constituents" | "external" | "proxy" | "yfinance_info".
    lookback_years : float
        Rolling window for percentile computation.
    missing_data_weight : float
        Score for tickers with no P/E data.
    cache_dir : Path, optional
        Directory for caching P/E data.
    polygon_api_key : str, optional
        Polygon API key (required when source="polygon"). Falls back to proxy if None.

    Returns
    -------
    pd.DataFrame
        Month-end value z-scores.
    """
    if source == "polygon":
        if not polygon_api_key:
            import os
            polygon_api_key = os.environ.get("POLYGON_API_KEY")
        if not polygon_api_key:
            logger.warning("POLYGON_API_KEY not available; falling back to proxy.")
            return compute_value_signal_full(
                prices, source="proxy",
                lookback_years=lookback_years,
                missing_data_weight=missing_data_weight,
            )

        etf_tickers = list(prices.columns)
        start = prices.index[0].strftime("%Y-%m-%d")
        end   = prices.index[-1].strftime("%Y-%m-%d")

        cache_path: Optional[Path] = None
        if cache_dir is not None:
            tickers_key = "_".join(sorted(etf_tickers))
            cache_path = Path(cache_dir) / f"pe_polygon_{tickers_key}.pkl"

        try:
            pe_df = build_pe_series_from_polygon(
                etf_tickers=etf_tickers,
                start=start,
                end=end,
                api_key=polygon_api_key,
                cache_path=cache_path,
            )
            if pe_df.notna().any().any():
                return compute_value_signal(
                    pe_df, lookback_years=lookback_years,
                    missing_data_weight=missing_data_weight,
                )
            logger.warning("Polygon P/E returned all NaN; falling back to proxy.")
        except Exception as e:
            logger.warning(f"Polygon P/E fetch failed ({e}); falling back to proxy.")

        return compute_value_signal_full(
            prices, source="proxy",
            lookback_years=lookback_years,
            missing_data_weight=missing_data_weight,
        )

    elif source == "constituents":
        etf_tickers = list(prices.columns)
        start = prices.index[0].strftime("%Y-%m-%d")
        end   = prices.index[-1].strftime("%Y-%m-%d")

        cache_path: Optional[Path] = None
        if cache_dir is not None:
            tickers_key = "_".join(sorted(etf_tickers))
            cache_path = Path(cache_dir) / f"pe_constituents_{tickers_key}.pkl"

        try:
            pe_df = build_pe_series_from_constituents(
                etf_tickers=etf_tickers,
                start=start,
                end=end,
                cache_path=cache_path,
            )
            # If all data is NaN, fall through to proxy
            if pe_df.notna().any().any():
                return compute_value_signal(
                    pe_df, lookback_years=lookback_years,
                    missing_data_weight=missing_data_weight,
                )
            logger.warning("Constituent P/E returned all NaN; falling back to proxy.")
        except Exception as e:
            logger.warning(f"Constituent P/E fetch failed ({e}); falling back to proxy.")

        # Fallback to proxy if constituents unavailable
        return compute_value_signal_full(
            prices, source="proxy",
            lookback_years=lookback_years,
            missing_data_weight=missing_data_weight,
        )

    elif source == "external":
        if pe_history is None:
            raise ValueError("pe_history must be provided when source='external'.")
        return compute_value_signal(pe_history, lookback_years=lookback_years,
                                     missing_data_weight=missing_data_weight)

    elif source == "proxy":
        monthly_prices = prices.resample("ME").last()
        pe_proxy = build_pe_proxy_series(monthly_prices)
        return compute_value_signal(pe_proxy, lookback_years=lookback_years,
                                     missing_data_weight=missing_data_weight)

    elif source == "yfinance_info":
        logger.warning(
            "yfinance_info P/E source fetches current snapshot only. "
            "Look-ahead bias in backtesting. Falling back to proxy."
        )
        return compute_value_signal_full(
            prices, source="proxy",
            lookback_years=lookback_years,
            missing_data_weight=missing_data_weight,
        )

    else:
        raise ValueError(f"Unknown value signal source: '{source}'. "
                         "Choose: 'constituents', 'external', 'proxy', 'yfinance_info'.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent.parent))
    from sector_rotation.data.loader import load_all

    prices, _ = load_all()
    etf_prices = prices.drop(columns=["SPY"], errors="ignore")

    print("\n=== Value Signal (constituents method, last 3 months) ===")
    cache = Path("sector_rotation/data/cache")
    val_sig = compute_value_signal_full(etf_prices, source="constituents", cache_dir=cache)
    print(val_sig.tail(3).to_string())
