"""Perp Universe Definition (Plan 05 §3).

COPIED-AND-ADAPTED from qlib-main/sector_rotation/data/universe.py (read-only
template). Structure preserved: metadata dataclass + lookup dicts + get_tickers
+ validate helpers. Adaptations (plan §5 "Change" column only):
  * SectorETF → PerpMeta (contract_size, listing_date, underlying asset).
  * GICS/liquidity-tier machinery → depth-qualification gate (min top-of-book
    depth is intraday; the DAILY gate uses notional volume + OI from the panel).
  * NEW: listing_history_floor_days — a perp enters the universe only after N
    days of data exist (the AISS late-IPO-floor analog; PIT-correct).
  * Dynamic availability from the live /margin/markets snapshot via
    crypto_common.config.ACTIVE_PERPS_SNAPSHOT fallback.

Static metadata below reflects the probe snapshot 2026-07-07; the dynamic
functions re-derive activity/qualification from data, never from this table.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional

import pandas as pd

from crypto_trading.crypto_common.config import ACTIVE_PERPS_SNAPSHOT

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Kalshi perps launch epoch — no perp data exists before this (BTC live 2026-06-03).
UNIVERSE_START: date = date(2026, 6, 3)

# Benchmark instruments (plan §9: KXBTCPERP-HODL + equal-weight basket)
BENCHMARK_TICKER: str = "KXBTCPERP"
BENCHMARK_EW: str = "EW_PERP_BASKET"


# ---------------------------------------------------------------------------
# Perp Metadata Dataclass  (template: SectorETF)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PerpMeta:
    """Metadata for a single Kalshi crypto perp."""

    ticker: str
    asset: str               # underlying (BTC, ETH, …)
    contract_size: float     # underlying units per contract (probe-verified)
    listing_date: date       # first data date observed
    notes: str = ""


# Probe snapshot 2026-07-07 (13 active; DOT/HBAR/XLM listed-inactive excluded).
PERP_UNIVERSE: List[PerpMeta] = [
    PerpMeta("KXBTCPERP", "BTC", 1e-4, date(2026, 6, 3), "flagship; deepest book"),
    PerpMeta("KXETHPERP", "ETH", 1e-3, date(2026, 6, 3), "highest 24h contract volume"),
    PerpMeta("KXSOLPERP", "SOL", 0.1, date(2026, 6, 3)),
    PerpMeta("KXXRPPERP", "XRP", 1.0, date(2026, 6, 3)),
    PerpMeta("KXDOGEPERP", "DOGE", 100.0, date(2026, 6, 11)),
    PerpMeta("KXKSHIBPERP", "SHIB", 1000.0, date(2026, 6, 11), "1000 SHIB per contract"),
    PerpMeta("KXBCHPERP", "BCH", 0.01, date(2026, 6, 11), "most negative funding skew"),
    PerpMeta("KXLTCPERP", "LTC", 0.1, date(2026, 6, 11)),
    PerpMeta("KXLINKPERP", "LINK", 1.0, date(2026, 6, 10)),
    PerpMeta("KXNEARPERP", "NEAR", 1.0, date(2026, 6, 24)),
    PerpMeta("KXSUIPERP", "SUI", 10.0, date(2026, 6, 11)),
    PerpMeta("KXHYPEPERP", "HYPE", 0.1, date(2026, 6, 10), "no US spot venue for index leg"),
    PerpMeta("KXZECPERP", "ZEC", 0.01, date(2026, 6, 30), "newest listing"),
]

_BY_TICKER: Dict[str, PerpMeta] = {p.ticker: p for p in PERP_UNIVERSE}


# ---------------------------------------------------------------------------
# Public API  (template names preserved)
# ---------------------------------------------------------------------------

def get_tickers(include_benchmark: bool = False) -> List[str]:
    """All perp tickers in the static universe (benchmark IS a member here —
    included only once; flag kept for template interface compatibility)."""
    tickers = [p.ticker for p in PERP_UNIVERSE]
    if include_benchmark and BENCHMARK_TICKER not in tickers:
        tickers.append(BENCHMARK_TICKER)
    return tickers


def get_perp(ticker: str) -> PerpMeta:
    if ticker not in _BY_TICKER:
        raise KeyError(f"Ticker '{ticker}' not in perp universe.")
    return _BY_TICKER[ticker]


def universe_as_dataframe() -> pd.DataFrame:
    rows = [{"ticker": p.ticker, "asset": p.asset, "contract_size": p.contract_size,
             "listing_date": p.listing_date, "notes": p.notes} for p in PERP_UNIVERSE]
    return pd.DataFrame(rows).set_index("ticker")


def validate_date_for_universe(d: date, strict: bool = True) -> bool:
    """Template contract: no data exists before UNIVERSE_START."""
    valid = d >= UNIVERSE_START
    if not valid and strict:
        raise ValueError(
            f"Date {d} is before UNIVERSE_START ({UNIVERSE_START}). "
            "Kalshi perps did not exist — set backtest start >= 2026-06-03.")
    return valid


# ---------------------------------------------------------------------------
# NEW (plan §3): dynamic, PIT-correct universe selection
# ---------------------------------------------------------------------------

def depth_qualified(
    volumes_notional: pd.DataFrame,
    oi_notional: Optional[pd.DataFrame],
    asof: pd.Timestamp,
    *,
    min_daily_notional_usd: float = 100_000.0,
    min_oi_notional_usd: float = 50_000.0,
    lookback_days: int = 7,
) -> List[str]:
    """Tickers meeting the depth/volume gate as of ``asof`` (PIT: trailing data
    only). Plan §9's exchange-level ``min_top_depth_contracts`` is an intraday
    execution gate (enforced at order time); the DAILY qualification here uses
    notional volume + OI, which the panel actually observes."""
    window = volumes_notional.loc[:asof].tail(lookback_days)
    if window.empty:
        return []
    ok_vol = window.mean() >= min_daily_notional_usd
    qualified = set(ok_vol[ok_vol].index)
    if oi_notional is not None and not oi_notional.loc[:asof].empty:
        last_oi = oi_notional.loc[:asof].iloc[-1]
        qualified &= set(last_oi[last_oi >= min_oi_notional_usd].index)
    return sorted(qualified)


def listing_floor_ok(
    prices: pd.DataFrame,
    asof: pd.Timestamp,
    *,
    floor_days: int = 30,
) -> List[str]:
    """Tickers with ≥ floor_days of history as of ``asof`` (late-listing floor,
    the AISS late-IPO analog — a new perp must season before entering)."""
    hist = prices.loc[:asof]
    counts = hist.notna().sum()
    return sorted(counts[counts >= floor_days].index)


def get_universe(
    prices: pd.DataFrame,
    volumes_notional: Optional[pd.DataFrame] = None,
    oi_notional: Optional[pd.DataFrame] = None,
    asof: Optional[pd.Timestamp] = None,
    *,
    floor_days: int = 30,
    min_daily_notional_usd: float = 100_000.0,
    min_perps_to_activate: int = 6,
) -> tuple[List[str], bool]:
    """PIT universe at ``asof``: listing floor ∩ depth gate.

    Returns (tickers, activated) — ``activated`` False when fewer than
    ``min_perps_to_activate`` qualify (plan §3: scaffold + paper until then).
    """
    asof = asof or prices.index[-1]
    eligible = set(listing_floor_ok(prices, asof, floor_days=floor_days))
    if volumes_notional is not None:
        eligible &= set(depth_qualified(
            volumes_notional, oi_notional, asof,
            min_daily_notional_usd=min_daily_notional_usd))
    eligible &= set(ACTIVE_PERPS_SNAPSHOT) | set(prices.columns)
    tickers = sorted(eligible)
    activated = len(tickers) >= min_perps_to_activate
    if not activated:
        logger.warning("universe below activation gate: %d < %d qualified",
                       len(tickers), min_perps_to_activate)
    return tickers, activated


if __name__ == "__main__":
    print(universe_as_dataframe())
    print(f"\nTickers: {get_tickers()}")
    print(f"Universe start: {UNIVERSE_START}")
