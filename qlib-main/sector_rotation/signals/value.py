"""
Relative Value Signal
=====================
Sector P/E percentile relative to own 10-year history.

Lower P/E percentile → sector is cheap relative to its own history → higher signal score.

Data source strategy:
    - Primary: yfinance `.info` dict for each ETF (trailing PE or forward PE).
    - Limitation: yfinance P/E for ETFs is often stale, estimated, or unavailable.
    - Fallback: If P/E is missing for a ticker, that ticker's value weight is set
      to 0 and its composite score comes from momentum + regime only.

Important caveats:
    - ETF P/E represents aggregate portfolio P/E, which is a weighted average of
      constituent stock P/Es. This is not directly comparable to individual stock P/Es.
    - Survivorship and composition changes (e.g., XLC restructuring 2018-09) affect
      historical P/E series comparability.
    - P/E from yfinance .info is a point-in-time snapshot, not time-series.
      For a proper time-series analysis, a commercial data vendor is needed.
    - This module implements a best-effort approach with transparent caveats.

Reference:
    Asness, C., Moskowitz, T., & Pedersen, L. (2013).
    Value and momentum everywhere. Journal of Finance, 68(3), 929-985.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# P/E Data Fetching
# ---------------------------------------------------------------------------

def fetch_pe_snapshot(tickers: List[str], pause_sec: float = 0.5) -> Dict[str, Optional[float]]:
    """
    Fetch current trailing P/E ratios for each ticker via yfinance.

    Returns a dict {ticker: pe_ratio or None}.
    None means the data was not available or was an invalid value (negative, zero).

    Note: This is a SNAPSHOT at time of call, not a historical time series.
    For backtesting, use `build_pe_proxy_series` or a commercial data provider.
    """
    try:
        import yfinance as yf
    except ImportError:
        raise ImportError("yfinance is required for P/E data fetching.")

    pe_dict: Dict[str, Optional[float]] = {}
    for ticker in tickers:
        try:
            info = yf.Ticker(ticker).info
            # yfinance keys vary; try multiple
            pe = (
                info.get("trailingPE")
                or info.get("forwardPE")
                or info.get("pegRatio")   # Last resort proxy
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
# P/E Proxy Series Builder (for backtesting)
# ---------------------------------------------------------------------------

def build_pe_proxy_series(
    prices: pd.DataFrame,
    earnings_yield_proxy: str = "price_to_book",
) -> pd.DataFrame:
    """
    Build a proxy P/E time series for backtesting using available price data.

    IMPORTANT: This is a fallback proxy when true P/E time series are unavailable.
    Real P/E data requires a commercial data vendor (Bloomberg, FactSet, etc.).

    Proxy approaches:
        1. "price_to_book": Not implementable from price data alone.
        2. "normalized_price": Use 12-month rolling price level / 5-year avg price level.
           This is a very rough proxy for "expensiveness" relative to history.
           NOT a true P/E but directionally correct for sector rotation timing.

    Parameters
    ----------
    prices : pd.DataFrame
        Monthly ETF prices.
    earnings_yield_proxy : str
        Proxy method. Currently only "normalized_price" is supported.

    Returns
    -------
    pd.DataFrame
        Proxy "P/E"-like series for value signal computation.
        NOTE: Do NOT mix with real P/E data without rescaling.
    """
    if earnings_yield_proxy != "normalized_price":
        raise NotImplementedError(
            f"Proxy method '{earnings_yield_proxy}' not implemented. "
            "Use 'normalized_price'."
        )

    monthly = prices.resample("ME").last()

    # Proxy: ratio of current price to 60-month (5-year) rolling average price
    # Higher ratio → relatively expensive → acts like high P/E
    proxy = monthly / monthly.rolling(window=60, min_periods=24).mean()

    logger.warning(
        "Using price-to-5yr-avg as P/E proxy. This is a rough approximation. "
        "For production use, obtain actual P/E data from a commercial provider."
    )
    return proxy


# ---------------------------------------------------------------------------
# Full value signal computation with proxy fallback
# ---------------------------------------------------------------------------

def compute_value_signal_full(
    prices: pd.DataFrame,
    pe_history: Optional[pd.DataFrame] = None,
    source: str = "proxy",
    lookback_years: float = 10.0,
    missing_data_weight: float = 0.0,
) -> pd.DataFrame:
    """
    Compute value signal with automatic fallback to price proxy.

    Parameters
    ----------
    prices : pd.DataFrame
        Daily adjusted close prices.
    pe_history : pd.DataFrame, optional
        External P/E time series (monthly). If None, uses proxy.
    source : str
        "yfinance_info" | "proxy" | "external"
        "yfinance_info" fetches current snapshot (backtest-incompatible).
        "proxy" uses normalized price as P/E proxy.
        "external" uses pe_history argument.
    lookback_years : float
        Rolling history window for percentile.
    missing_data_weight : float
        Score for tickers with no data.

    Returns
    -------
    pd.DataFrame
        Month-end value z-scores.
    """
    if source == "proxy":
        monthly_prices = prices.resample("ME").last()
        pe_proxy = build_pe_proxy_series(monthly_prices)
        return compute_value_signal(pe_proxy, lookback_years=lookback_years,
                                     missing_data_weight=missing_data_weight)

    elif source == "external":
        if pe_history is None:
            raise ValueError("pe_history must be provided when source='external'.")
        return compute_value_signal(pe_history, lookback_years=lookback_years,
                                     missing_data_weight=missing_data_weight)

    elif source == "yfinance_info":
        logger.warning(
            "yfinance_info P/E source fetches current snapshot only. "
            "Using this in backtesting creates a look-ahead bias. "
            "Treating as missing data and using proxy instead."
        )
        # Fall back to proxy for backtesting
        return compute_value_signal_full(
            prices, pe_history=None, source="proxy",
            lookback_years=lookback_years,
            missing_data_weight=missing_data_weight,
        )

    else:
        raise ValueError(f"Unknown value signal source: {source}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s: %(message)s")

    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent.parent))
    from sector_rotation.data.loader import load_all

    prices, _ = load_all()
    etf_prices = prices.drop(columns=["SPY"], errors="ignore")

    print("\n=== Value Signal (proxy method, last 3 months) ===")
    val_sig = compute_value_signal_full(etf_prices, source="proxy")
    print(val_sig.tail(3).to_string())
