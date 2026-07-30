"""Perp panel loader (Plan 05 §4).

COPIED CONTRACT from qlib-main/sector_rotation/data/loader.py (read-only
template): pure load functions returning wide DataFrames (rows = dates,
cols = tickers), explicit no-NaN boundary, cached stores never mutated,
`load_returns` helper. Adaptations (plan §5 "Change" column):
  * Sources: crypto_common.loader (our parquet stores) instead of yfinance.
  * 24/7 daily bars, tz-aware UTC (no NYSE calendar).
  * Funding panel (per-day SUM of 8h cycle rates) — the carry factor input.
  * ``proxy_panel()``: same shapes from the offshore OKX store, every frame
    labeled ``attrs["source"]="proxy:…"`` — factor prototyping ONLY, never the
    validation gate (Plan 00/08 proxy discipline).
"""

from __future__ import annotations

import logging
from typing import Dict, Optional, Tuple

import pandas as pd

from crypto_trading.crypto_common.loader import (load_funding, load_offshore,
                                                 load_perp_candles)
from crypto_trading.crypto_strategies.perp_rotation.data.universe import (PERP_UNIVERSE,
                                                                          get_tickers)

logger = logging.getLogger(__name__)

# Kalshi perp ↔ offshore proxy symbol map (only pairs with offshore data)
PROXY_SYMBOLS: Dict[str, str] = {
    "KXBTCPERP": "BTCUSDT", "KXETHPERP": "ETHUSDT", "KXSOLPERP": "SOLUSDT",
    "KXXRPPERP": "XRPUSDT", "KXDOGEPERP": "DOGEUSDT",
}


def _to_daily_utc(s: pd.Series) -> pd.Series:
    # End-labeled candles: the point at exactly T+1 00:00 is day T's close.
    # Bin (T, T+1] labeled T puts each day's close on its own calendar day so
    # the price panel lines up with the funding panel (day-T settlements at
    # label T). Default label-left put day-T's close on row T+1, leaving the
    # two panels one day apart. PIT holds because the engine lags decisions.
    return s.resample("1D", label="left", closed="right").last()


def build_perp_panel(
    tickers: Optional[list[str]] = None,
    *,
    start=None,
    end=None,
    max_ffill_days: int = 2,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Daily Kalshi panels: (prices, volumes_notional, oi_notional).

    prices = 1d-candle price_close ($/contract — cross-sectional returns are
    identical to underlying returns since contract_size is constant).
    No-NaN contract applies to PRICES only after per-ticker listing start
    (leading NaN before listing is legitimate PIT emptiness and preserved —
    the universe listing floor consumes it).
    """
    tickers = tickers or get_tickers()
    prices, vols, ois = {}, {}, {}
    for t in tickers:
        try:
            c = load_perp_candles(t, "1d", start=start, end=end)
        except FileNotFoundError:
            logger.warning("no 1d candles for %s — excluded from panel", t)
            continue
        prices[t] = _to_daily_utc(c.price_close)
        if "volume" in c.columns and "price_close" in c.columns:
            vols[t] = _to_daily_utc(c.volume * c.price_close)
        if "oi_notional" in c.columns:
            ois[t] = _to_daily_utc(c.oi_notional)
    if not prices:
        raise FileNotFoundError("no perp candles found — run kalshi backfill first")

    px = pd.DataFrame(prices).sort_index()
    # interior gaps only: short ffill, never backfill (PIT)
    px = px.ffill(limit=max_ffill_days)
    vol = pd.DataFrame(vols).reindex(px.index)
    oi = pd.DataFrame(ois).reindex(px.index)
    px.attrs["source"] = "kalshi"
    return px, vol, oi


def build_funding_panel(
    tickers: Optional[list[str]] = None,
    *,
    start=None,
    end=None,
) -> pd.DataFrame:
    """Daily funding panel: per-day SUM of realized 8h cycle rates per ticker.

    PIT: funding_time is the settlement instant — a day's value is known only
    at that day's end; signals must lag ≥ 1 day (the composite's shift handles
    it, same discipline as the template's monthly signals).
    """
    tickers = tickers or get_tickers()
    cols = {}
    for t in tickers:
        try:
            f = load_funding(t, start=start, end=end)
        except FileNotFoundError:
            continue
        cols[t] = f.funding_rate.resample("1D").sum()
    panel = pd.DataFrame(cols).fillna(0.0)
    panel.attrs["source"] = "kalshi"
    return panel


def load_returns(prices: pd.DataFrame) -> pd.DataFrame:
    """Template-contract helper: simple daily returns from the price panel."""
    return prices.pct_change()


# ---------------------------------------------------------------------------
# Offshore proxy panel (prototyping only — labeled)
# ---------------------------------------------------------------------------

def proxy_panel(
    *,
    start=None,
    end=None,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """(prices, volumes_notional, funding_daily) from the offshore store,
    columns renamed to the Kalshi tickers they proxy. attrs['source'] marks
    every frame ``proxy`` — factor prototyping ONLY (plan §8 history caveat)."""
    px, vol, fund = {}, {}, {}
    src = "proxy:?"
    for kx, sym in PROXY_SYMBOLS.items():
        try:
            k = load_offshore("klines_1d", sym)
        except FileNotFoundError:
            continue
        src = str(k["source"].iloc[-1]) if "source" in k.columns else "proxy:offshore"
        daily_close = k.close.resample("1D").last()
        px[kx] = daily_close
        vol[kx] = (k.volume * k.close).resample("1D").last()
        try:
            f = load_offshore("funding", sym)
            fund[kx] = f.funding_rate.resample("1D").sum()
        except FileNotFoundError:
            pass
    if not px:
        raise FileNotFoundError("no offshore proxy data — run refdata.derivs backfill")

    def _clip(df: pd.DataFrame) -> pd.DataFrame:
        if start is not None:
            df = df[df.index >= pd.Timestamp(start, tz="UTC")]
        if end is not None:
            df = df[df.index <= pd.Timestamp(end, tz="UTC")]
        return df

    prices = _clip(pd.DataFrame(px).sort_index().ffill(limit=2))
    volumes = _clip(pd.DataFrame(vol)).reindex(prices.index)
    funding = _clip(pd.DataFrame(fund)).reindex(prices.index).fillna(0.0)
    for frame in (prices, volumes, funding):
        frame.attrs["source"] = src
    logger.warning("PROXY panel in use (%s) — prototyping only, never the "
                   "validation gate", src)
    return prices, volumes, funding
