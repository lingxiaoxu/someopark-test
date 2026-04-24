"""
ETF Universe Definition
=======================
SPDR Select Sector ETF universe with full GICS mapping, S&P 500 weights,
liquidity metadata, and structural-break handling.

GICS structural break notes:
    - 2016-09: Real Estate (XLRE) spun off from Financials. XLRE launched 2015-10.
    - 2018-09: Telecom → Communication Services (XLC).
               Meta (GOOGL) moved from XLK to XLC; Disney (DIS) moved from XLY to XLC.
               This is a hard structural break — XLC was launched 2018-06-18.
    - 2023-03: Visa/Mastercard reclassified from XLK to XLF (~2% each).
               Impact: XLK -4%, XLF +4% (approx).

Backtest rule: start no earlier than UNIVERSE_START (2018-07-01) for full 11-sector
universe. Any pre-break statistics must be annotated with a caveat.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional

import pandas as pd

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# First date when all 11 SPDR Select Sector ETFs were trading
UNIVERSE_START: date = date(2018, 7, 1)

# Date of the GICS Communication Services restructuring (hard structural break)
GICS_COMMSVCS_BREAK: date = date(2018, 9, 28)

# Backtest baseline benchmark
BENCHMARK_TICKER: str = "SPY"


# ---------------------------------------------------------------------------
# ETF Metadata Dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SectorETF:
    """Metadata for a single SPDR Select Sector ETF."""

    ticker: str
    gics_code: int           # GICS sector code (2-digit)
    sector_name: str         # Full GICS sector name
    short_name: str          # Abbreviated name for plots
    inception_date: date     # ETF launch date
    expense_ratio_bps: int   # Annual management fee in basis points
    # Approximate S&P 500 sector weight (as of 2024-Q4, for reference only)
    sp500_weight_pct: float
    # Approximate daily dollar volume (USD millions, 2024 average)
    avg_daily_volume_m: float
    # Liquidity tier (1 = highest, 3 = lowest) — drives transaction cost model
    liquidity_tier: int
    # Descriptive notes on holdings or known breaks
    notes: str = ""


# ---------------------------------------------------------------------------
# ETF Universe Definition
# ---------------------------------------------------------------------------

ETF_UNIVERSE: List[SectorETF] = [
    SectorETF(
        ticker="XLE",
        gics_code=10,
        sector_name="Energy",
        short_name="Energy",
        inception_date=date(1998, 12, 16),
        expense_ratio_bps=9,
        sp500_weight_pct=3.8,
        avg_daily_volume_m=1500,
        liquidity_tier=1,
        notes="Top holdings: XOM, CVX. High correlation with oil prices.",
    ),
    SectorETF(
        ticker="XLB",
        gics_code=15,
        sector_name="Materials",
        short_name="Materials",
        inception_date=date(1998, 12, 16),
        expense_ratio_bps=9,
        sp500_weight_pct=2.5,
        avg_daily_volume_m=500,
        liquidity_tier=2,
        notes="Top holdings: LIN, FCX, APD.",
    ),
    SectorETF(
        ticker="XLI",
        gics_code=20,
        sector_name="Industrials",
        short_name="Industrials",
        inception_date=date(1998, 12, 16),
        expense_ratio_bps=9,
        sp500_weight_pct=8.5,
        avg_daily_volume_m=800,
        liquidity_tier=2,
        notes="Top holdings: GE, CAT, RTX, UPS.",
    ),
    SectorETF(
        ticker="XLY",
        gics_code=25,
        sector_name="Consumer Discretionary",
        short_name="Cons.Disc",
        inception_date=date(1998, 12, 16),
        expense_ratio_bps=9,
        sp500_weight_pct=10.0,
        avg_daily_volume_m=700,
        liquidity_tier=2,
        notes=(
            "GICS break 2018-09: Disney/Comcast moved to XLC. "
            "Top holdings: AMZN, TSLA, HD."
        ),
    ),
    SectorETF(
        ticker="XLP",
        gics_code=30,
        sector_name="Consumer Staples",
        short_name="Cons.Stap",
        inception_date=date(1998, 12, 16),
        expense_ratio_bps=9,
        sp500_weight_pct=6.0,
        avg_daily_volume_m=600,
        liquidity_tier=2,
        notes="Defensive sector. Top holdings: PG, KO, PEP, COST.",
    ),
    SectorETF(
        ticker="XLV",
        gics_code=35,
        sector_name="Health Care",
        short_name="Healthcare",
        inception_date=date(1998, 12, 16),
        expense_ratio_bps=9,
        sp500_weight_pct=12.0,
        avg_daily_volume_m=1200,
        liquidity_tier=1,
        notes="Defensive sector. Top holdings: UNH, LLY, JNJ, ABBV.",
    ),
    SectorETF(
        ticker="XLF",
        gics_code=40,
        sector_name="Financials",
        short_name="Financials",
        inception_date=date(1998, 12, 16),
        expense_ratio_bps=9,
        sp500_weight_pct=13.0,
        avg_daily_volume_m=1500,
        liquidity_tier=1,
        notes=(
            "XLRE spun off 2016-09. 2023-03: Visa/MC reclassified from XLK. "
            "Top holdings: BRK.B, JPM, V, MA."
        ),
    ),
    SectorETF(
        ticker="XLK",
        gics_code=45,
        sector_name="Information Technology",
        short_name="InfoTech",
        inception_date=date(1998, 12, 16),
        expense_ratio_bps=9,
        sp500_weight_pct=30.0,
        avg_daily_volume_m=1500,
        liquidity_tier=1,
        notes=(
            "GICS break 2018-09: Meta/Google moved to XLC (~9% reduction). "
            "2023-03: Visa/MC moved to XLF (~4% reduction). "
            "Top holdings: NVDA, MSFT, AAPL."
        ),
    ),
    SectorETF(
        ticker="XLC",
        gics_code=50,
        sector_name="Communication Services",
        short_name="CommSvcs",
        inception_date=date(2018, 6, 18),
        expense_ratio_bps=9,
        sp500_weight_pct=9.0,
        avg_daily_volume_m=300,
        liquidity_tier=3,
        notes=(
            "Created 2018-06-18 from GICS Telecom restructuring. "
            "Includes GOOGL, META, NFLX, DIS, CMCSA. "
            "No valid data before UNIVERSE_START (2018-07-01)."
        ),
    ),
    SectorETF(
        ticker="XLU",
        gics_code=55,
        sector_name="Utilities",
        short_name="Utilities",
        inception_date=date(1998, 12, 16),
        expense_ratio_bps=9,
        sp500_weight_pct=2.5,
        avg_daily_volume_m=500,
        liquidity_tier=2,
        notes="Defensive sector. High dividend yield. Rate-sensitive.",
    ),
    SectorETF(
        ticker="XLRE",
        gics_code=60,
        sector_name="Real Estate",
        short_name="RealEst",
        inception_date=date(2015, 10, 7),
        expense_ratio_bps=9,
        sp500_weight_pct=2.5,
        avg_daily_volume_m=200,
        liquidity_tier=3,
        notes=(
            "Spun off from Financials 2016-09. ETF launched 2015-10-07. "
            "REIT-heavy, rate-sensitive. Low daily volume."
        ),
    ),
]

# Lookup dicts for fast access
_BY_TICKER: Dict[str, SectorETF] = {etf.ticker: etf for etf in ETF_UNIVERSE}
_BY_GICS: Dict[int, SectorETF] = {etf.gics_code: etf for etf in ETF_UNIVERSE}


# ---------------------------------------------------------------------------
# Liquidity Tier Mapping (for transaction cost model)
# ---------------------------------------------------------------------------

TIER_1_TICKERS: List[str] = [e.ticker for e in ETF_UNIVERSE if e.liquidity_tier == 1]
TIER_2_TICKERS: List[str] = [e.ticker for e in ETF_UNIVERSE if e.liquidity_tier == 2]
TIER_3_TICKERS: List[str] = [e.ticker for e in ETF_UNIVERSE if e.liquidity_tier == 3]

# Cost in basis points (one-way)
TIER_COST_BPS: Dict[int, int] = {1: 3, 2: 5, 3: 8}

# Defensive sectors (used for regime-conditional bonus)
DEFENSIVE_TICKERS: List[str] = ["XLU", "XLP", "XLV"]

# Cyclical sectors (high economic cycle sensitivity)
CYCLICAL_TICKERS: List[str] = ["XLE", "XLB", "XLI", "XLY", "XLF"]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_tickers(include_benchmark: bool = False) -> List[str]:
    """Return all ETF tickers in the universe."""
    tickers = [etf.ticker for etf in ETF_UNIVERSE]
    if include_benchmark:
        tickers.append(BENCHMARK_TICKER)
    return tickers


def get_etf(ticker: str) -> SectorETF:
    """Return SectorETF metadata for a given ticker."""
    if ticker not in _BY_TICKER:
        raise KeyError(f"Ticker '{ticker}' not in sector ETF universe.")
    return _BY_TICKER[ticker]


def get_etf_by_gics(gics_code: int) -> SectorETF:
    """Return SectorETF metadata by GICS 2-digit code."""
    if gics_code not in _BY_GICS:
        raise KeyError(f"GICS code {gics_code} not in sector ETF universe.")
    return _BY_GICS[gics_code]


def get_liquidity_tier(ticker: str) -> int:
    """Return 1/2/3 liquidity tier for transaction cost assignment."""
    return get_etf(ticker).liquidity_tier


def get_cost_bps(ticker: str) -> int:
    """Return estimated one-way transaction cost in basis points."""
    tier = get_liquidity_tier(ticker)
    return TIER_COST_BPS[tier]


def get_sp500_weights() -> pd.Series:
    """
    Return approximate S&P 500 sector weights as a pd.Series (indexed by ticker).
    Values are approximate as of 2024-Q4.  Use only for reference / beta estimation.
    """
    return pd.Series(
        {etf.ticker: etf.sp500_weight_pct / 100.0 for etf in ETF_UNIVERSE},
        name="sp500_weight",
    )


def universe_as_dataframe() -> pd.DataFrame:
    """Return the full ETF universe as a DataFrame (one row per ETF)."""
    rows = []
    for etf in ETF_UNIVERSE:
        rows.append(
            {
                "ticker": etf.ticker,
                "gics_code": etf.gics_code,
                "sector_name": etf.sector_name,
                "short_name": etf.short_name,
                "inception_date": etf.inception_date,
                "expense_ratio_bps": etf.expense_ratio_bps,
                "sp500_weight_pct": etf.sp500_weight_pct,
                "avg_daily_volume_m": etf.avg_daily_volume_m,
                "liquidity_tier": etf.liquidity_tier,
                "notes": etf.notes,
            }
        )
    return pd.DataFrame(rows).set_index("ticker")


def validate_date_for_universe(d: date, strict: bool = True) -> bool:
    """
    Check whether date ``d`` is valid for the full 11-sector universe.

    Parameters
    ----------
    d : date
        The date to check.
    strict : bool
        If True, raise ValueError for invalid dates. If False, return bool.

    Returns
    -------
    bool
        True if the date is >= UNIVERSE_START.
    """
    valid = d >= UNIVERSE_START
    if not valid and strict:
        raise ValueError(
            f"Date {d} is before UNIVERSE_START ({UNIVERSE_START}). "
            "XLC (Communication Services) was not listed until 2018-06-18. "
            "The GICS structural break on 2018-09-28 invalidates cross-sector "
            "comparisons before that date. Set backtest start >= 2018-07-01."
        )
    return valid


if __name__ == "__main__":
    print("=== Sector ETF Universe ===")
    df = universe_as_dataframe()
    print(df[["gics_code", "sector_name", "inception_date",
               "sp500_weight_pct", "avg_daily_volume_m", "liquidity_tier"]])
    print(f"\nTickers: {get_tickers()}")
    print(f"Tier 1 (cheapest): {TIER_1_TICKERS}")
    print(f"Tier 2: {TIER_2_TICKERS}")
    print(f"Tier 3 (most expensive): {TIER_3_TICKERS}")
    print(f"\nS&P 500 weights:\n{get_sp500_weights()}")
    print(f"\nUniverse start: {UNIVERSE_START}")
