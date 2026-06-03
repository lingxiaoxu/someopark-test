"""
AISS Universe Definition
========================
AI Infra & Semiconductor Strategy (AISS) investment universe.

Structure
---------
8 semiconductor *subsectors*, each a 3-tier basket of individual stocks:

    primary  (80%)  +  backup1 (15%)  +  backup2 (5%)

The **tradeable asset of the strategy is the subsector** (a synthetic 80/15/5
basket), mirroring how the sibling SSRS strategy trades 11 GICS ETFs.  Signals,
the optimizer, risk overlays, the backtest engine, and all reports operate at
the subsector level (8 "assets").  The 24 underlying single stocks only matter
for (a) which prices to download and (b) constructing the basket return series
(see ``loader.build_subsector_prices``).

Why subsector-as-asset
----------------------
The economics are identical to executing the 24 stocks directly: a subsector
basket return is ``0.8*r_primary + 0.15*r_backup1 + 0.05*r_backup2`` and a
weight change ``dw`` in a subsector implies stock trades of ``0.8*dw, 0.15*dw,
0.05*dw`` — so the blended transaction cost is the 80/15/5 weighted average of
the three stocks' per-stock cost tiers (see ``backtest/costs.py``).  This keeps
maximal reuse of the SSRS engine while staying faithful to the AISS design
("板块间动态再平衡，板块内固定比例").

Point-in-time IPO handling
--------------------------
Several constituents IPO'd well after the backtest start (ARM 2023-09, ALAB
2024-03, CRDO 2022-01, GFS 2021-10).  ``effective_weights`` drops any tier whose
underlying ticker lacks ``min_history_months`` of data as-of a given date and
renormalises the remaining tiers, so the basket is always built only from data
that actually existed at that time (no look-ahead, no fabricated history).

Isolation
---------
This module is part of ``qlib-main/semiconductor_strategy`` and shares nothing
with ``sector_rotation`` beyond having a parallel interface.  Price data lives
in ``price_data/semi_strategy/`` (completely separate from ``sector_etfs``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional

import pandas as pd

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Earliest sensible backtest start.  Chosen so the strategy has a stable core
# (NVDA / AVGO / TXN / MU / TSM / AMD / QCOM / KLAC all trade well before this).
# Newer IPOs (ARM/ALAB/CRDO/GFS) are phased in via effective_weights().
UNIVERSE_START: date = date(2019, 1, 1)

# Fixed within-subsector tier weights: primary / backup1 / backup2
SUBSECTOR_TIER_WEIGHTS: tuple = (0.80, 0.15, 0.05)
_TIER_ORDER = ("primary", "backup1", "backup2")

# Benchmarks — SOXX and SMH are THE strategy's hurdle (must beat both);
# SPY is the broad-market reference for the information ratio.
BENCHMARK_TICKERS: List[str] = ["SOXX", "SMH", "SPY"]
PRIMARY_BENCHMARK: str = "SOXX"

# CapEx-pulse cloud names (NOT part of the tradeable universe; pulled separately
# by company_signals.py).  Listed here as the single source of truth.
CAPEX_PULSE_TICKERS: List[str] = ["MSFT", "GOOGL", "META", "AMZN"]

# Approximate first-trading dates for late IPOs (used by effective_weights when
# no live price history is supplied).  Conservative — a few days after IPO.
IPO_DATES: Dict[str, date] = {
    "ARM":  date(2023, 9, 14),
    "ALAB": date(2024, 3, 20),
    "CRDO": date(2022, 1, 27),
    "GFS":  date(2021, 10, 28),
    # SIMO/UMC/WDC/ADI/MCHP/SWKS/QRVO/LRCX/AMAT/KLAC/INTC/TXN/MU/QCOM/NVDA/
    # AVGO/AMD/TSM all trade since well before UNIVERSE_START.
}


# ---------------------------------------------------------------------------
# Subsector definitions (8 subsectors x 3 weighted tiers = 24 slots, 22 unique)
# ARM appears in BOTH ai_gpu(backup2) and logic_cpu(backup2) by design.
#
# Each subsector also has a 4th "reserve" slot at 0% weight — a vetted, data-ready
# spare (chosen by correlation/liquidity to the basket). It is NOT in _TIER_ORDER,
# so the normal 3-tier basket is unchanged; it is promoted ONLY when one of the
# three weighted tiers becomes unusable (halt/delisting/data outage) — see
# effective_weights(unavailable=...) and loader.build_subsector_prices.
# ---------------------------------------------------------------------------

SUBSECTORS: Dict[str, dict] = {
    "ai_gpu": {
        "primary":  ("NVDA", 0.80),
        "backup1":  ("ALAB", 0.15),
        "backup2":  ("ARM",  0.05),
        "reserve":  ("AMD",  0.00),   # 0% spare; promoted only on accident (dup: logic_cpu primary)
        "cycle":    "ai_capex_direct",
        "lead_lag": 0,    # months relative to AI-Capex signal (+lag / -lead)
        "display":  "AI / GPU",
    },
    "custom_asic": {
        "primary":  ("AVGO", 0.80),
        "backup1":  ("MRVL", 0.15),
        "backup2":  ("CRDO", 0.05),
        "reserve":  ("ANET", 0.00),   # 0% spare; promoted only on accident
        "cycle":    "ai_capex_derived",
        "lead_lag": 2,
        "display":  "Custom ASIC / Networking",
    },
    "equipment": {
        "primary":  ("KLAC", 0.80),
        "backup1":  ("LRCX", 0.15),
        "backup2":  ("AMAT", 0.05),
        "reserve":  ("ASML", 0.00),   # 0% spare; promoted only on accident
        "cycle":    "foundry_orders",
        "lead_lag": -12,  # leads the chip cycle ~12 months
        "display":  "Equipment",
    },
    "memory_hbm": {
        "primary":  ("MU",   0.80),
        "backup1":  ("WDC",  0.15),
        "backup2":  ("SIMO", 0.05),
        "reserve":  ("STX",  0.00),   # 0% spare; promoted only on accident
        "cycle":    "inventory_driven",
        "lead_lag": 4,
        "display":  "Memory / HBM",
    },
    "foundry": {
        "primary":  ("TSM",  0.80),
        "backup1":  ("UMC",  0.15),
        "backup2":  ("GFS",  0.05),
        "reserve":  ("TSEM", 0.00),   # 0% spare; promoted only on accident
        "cycle":    "capex_to_capacity",
        "lead_lag": -9,   # leads fabless ~9 months
        "display":  "Foundry",
    },
    "analog_defense": {
        "primary":  ("TXN",  0.80),
        "backup1":  ("ADI",  0.15),
        "backup2":  ("MCHP", 0.05),
        "reserve":  ("NXPI", 0.00),   # 0% spare; promoted only on accident
        "cycle":    "defensive_late_cycle",
        "lead_lag": 8,
        "display":  "Analog / Defensive",
    },
    "logic_cpu": {
        "primary":  ("AMD",  0.80),
        "backup1":  ("INTC", 0.15),
        "backup2":  ("ARM",  0.05),
        "reserve":  ("MRVL", 0.00),   # 0% spare; promoted only on accident (dup: custom_asic backup1)
        "cycle":    "ai_server_competition",
        "lead_lag": 4,
        "display":  "Logic / CPU",
    },
    "rf_edge": {
        "primary":  ("QCOM", 0.80),
        "backup1":  ("SWKS", 0.15),
        "backup2":  ("QRVO", 0.05),
        "reserve":  ("MTSI", 0.00),   # 0% spare; promoted only on accident
        "cycle":    "consumer_independent",
        "lead_lag": 0,
        "display":  "RF / Edge",
    },
}

# Defensive subsector(s) — receive a regime bonus in risk-off (cf. SSRS XLU/XLP/XLV).
DEFENSIVE_SUBSECTORS: List[str] = ["analog_defense"]

# Subsectors most directly geared to the AI-capex cycle (used by capex pulse tilt).
AI_CYCLE_SUBSECTORS: List[str] = ["ai_gpu", "custom_asic", "equipment"]


# ---------------------------------------------------------------------------
# Per-stock liquidity tiers (one-way transaction cost in bps).
# These feed the 80/15/5-blended *subsector* cost in backtest/costs.py.
# ---------------------------------------------------------------------------

STOCK_TIER: Dict[str, int] = {
    # Tier 1 (3 bps) — mega-cap, deepest liquidity
    "NVDA": 1, "AVGO": 1, "AMD": 1, "QCOM": 1, "TSM": 1, "MU": 1, "TXN": 1, "INTC": 1,
    "ASML": 1,
    # Tier 2 (5 bps) — large-cap
    "KLAC": 2, "LRCX": 2, "AMAT": 2, "WDC": 2, "ARM": 2, "ADI": 2,
    "NXPI": 2, "ANET": 2, "STX": 2,
    # Tier 3 (8 bps) — mid/small-cap or recent IPO
    "ALAB": 3, "MRVL": 3, "CRDO": 3, "MCHP": 3, "UMC": 3, "GFS": 3,
    "SIMO": 3, "SWKS": 3, "QRVO": 3, "TSEM": 3, "MTSI": 3,
}
TIER_COST_BPS: Dict[int, int] = {1: 3, 2: 5, 3: 8}


# ---------------------------------------------------------------------------
# Lightweight metadata dataclass (parallel to SSRS SectorETF, kept minimal)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SubsectorMeta:
    name: str
    display: str
    cycle: str
    lead_lag: int
    tickers: tuple          # (primary, backup1, backup2)
    weights: tuple          # (0.80, 0.15, 0.05)


def _meta(name: str) -> SubsectorMeta:
    d = SUBSECTORS[name]
    return SubsectorMeta(
        name=name,
        display=d.get("display", name),
        cycle=d["cycle"],
        lead_lag=d["lead_lag"],
        tickers=tuple(d[t][0] for t in _TIER_ORDER),
        weights=tuple(d[t][1] for t in _TIER_ORDER),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def subsector_names() -> List[str]:
    """Ordered list of the 8 subsector keys (the strategy's tradeable assets)."""
    return list(SUBSECTORS.keys())


def subsector_display(name: str) -> str:
    """Human-readable label for a subsector (for plots / reports)."""
    return SUBSECTORS[name].get("display", name)


def subsector_tickers(subsector: str) -> List[str]:
    """Return [primary, backup1, backup2] for a subsector (in tier order)."""
    d = SUBSECTORS[subsector]
    return [d[t][0] for t in _TIER_ORDER]


def subsector_weights(subsector: str) -> Dict[str, float]:
    """Return {ticker: tier_weight} (0.80/0.15/0.05) for a subsector.

    AISS subsectors never duplicate a ticker within themselves, so one entry per tier.
    """
    d = SUBSECTORS[subsector]
    return {d[t][0]: d[t][1] for t in _TIER_ORDER}


def subsector_reserve(subsector: str) -> Optional[str]:
    """Return the 0%-weight reserve ticker for a subsector (None if undefined).

    The reserve is a vetted, data-ready spare that carries 0% normally and is only
    promoted into the basket when one of the three weighted tiers (primary/backup1/
    backup2) becomes unusable (halt / delisting / data outage).  It is deliberately
    kept OUT of ``_TIER_ORDER`` so the 3-tier API (subsector_tickers / _weights /
    _meta) and basket returns are unchanged in the normal case.
    """
    d = SUBSECTORS[subsector]
    r = d.get("reserve")
    return r[0] if r else None


def reserve_tickers() -> List[str]:
    """All reserve tickers (de-duped, subsector order)."""
    seen: List[str] = []
    for s in SUBSECTORS:
        r = subsector_reserve(s)
        if r and r not in seen:
            seen.append(r)
    return seen


def subsector_of(ticker: str, include_reserve: bool = True) -> List[str]:
    """Return the subsector(s) a ticker belongs to (ARM is in two).

    With ``include_reserve`` also matches reserve slots (so a promoted reserve such
    as ASML resolves to its subsector during stock aggregation).
    """
    hits = [s for s in SUBSECTORS if ticker in subsector_tickers(s)]
    if include_reserve:
        hits += [s for s in SUBSECTORS if subsector_reserve(s) == ticker and s not in hits]
    if not hits:
        raise KeyError(f"Ticker {ticker!r} is not in the AISS universe.")
    return hits


def all_tickers(include_benchmark: bool = False, include_reserve: bool = True) -> List[str]:
    """All unique single-stock tickers in the universe (ARM de-duped).

    Deterministically ordered: subsector order, tier order, first occurrence.
    ``include_reserve`` appends the 0%-weight reserve tickers (so their prices are
    downloaded and the spare is data-ready); they never affect the normal basket.
    """
    seen: List[str] = []
    for s in SUBSECTORS:
        for t in subsector_tickers(s):
            if t not in seen:
                seen.append(t)
    if include_reserve:
        for r in reserve_tickers():
            if r not in seen:
                seen.append(r)
    if include_benchmark:
        for b in BENCHMARK_TICKERS:
            if b not in seen:
                seen.append(b)
    return seen


def benchmark_tickers() -> List[str]:
    """Benchmarks: SOXX (primary hurdle), SMH (secondary hurdle), SPY (broad)."""
    return list(BENCHMARK_TICKERS)


def lead_lag_months(subsector: str) -> int:
    """Lead/lag (months) vs the AI-Capex signal: positive = lags, negative = leads."""
    return SUBSECTORS[subsector]["lead_lag"]


# --- compatibility shims so copied SSRS code keeps working unchanged ---------

def get_tickers(include_benchmark: bool = False) -> List[str]:
    """SSRS-compatible alias.

    In AISS the strategy's *assets* are subsectors, but several copied modules
    historically called ``get_tickers()`` expecting the tradeable-asset list.
    Here that means the 8 subsector names (optionally + benchmarks).  Use
    ``all_tickers()`` when you specifically need the 24 single-stock tickers.
    """
    names = subsector_names()
    if include_benchmark:
        names = names + benchmark_tickers()
    return names


def get_liquidity_tier_stock(ticker: str) -> int:
    return STOCK_TIER.get(ticker, 3)


def subsector_cost_bps(subsector: str) -> float:
    """80/15/5-blended one-way transaction cost (bps) for a subsector basket."""
    total = 0.0
    for tkr, w in subsector_weights(subsector).items():
        total += w * TIER_COST_BPS[get_liquidity_tier_stock(tkr)]
    return total


def get_cost_bps(asset: str) -> float:
    """One-way cost bps for either a subsector name or a raw ticker."""
    if asset in SUBSECTORS:
        return subsector_cost_bps(asset)
    return float(TIER_COST_BPS[get_liquidity_tier_stock(asset)])


def get_sp500_weights() -> pd.Series:
    """Reference 'market' weights over the 8 subsectors (equal-weight proxy).

    AISS is a satellite semiconductor strategy with no natural S&P sector
    weights; callers that need a benchmark-deviation reference get an equal
    weighting across subsectors.  Used only by optional optimizer modes.
    """
    n = len(SUBSECTORS)
    return pd.Series({s: 1.0 / n for s in SUBSECTORS}, name="ref_weight")


# ---------------------------------------------------------------------------
# Point-in-time effective weights (IPO-aware downgrade + renormalisation)
# ---------------------------------------------------------------------------

def _ticker_available(
    ticker: str,
    as_of: date,
    min_history_months: int,
    first_available: Optional[Dict[str, date]],
) -> bool:
    """True if ``ticker`` has >= min_history_months of data on/before ``as_of``."""
    months = pd.DateOffset(months=min_history_months)
    if first_available and ticker in first_available and first_available[ticker] is not None:
        start = pd.Timestamp(first_available[ticker])
    elif ticker in IPO_DATES:
        start = pd.Timestamp(IPO_DATES[ticker])
    else:
        return True  # long-history name
    return pd.Timestamp(as_of) >= (start + months)


def effective_weights(
    subsector: str,
    as_of_date,
    min_history_months: int = 24,
    first_available: Optional[Dict[str, date]] = None,
    unavailable: Optional[set] = None,
) -> Dict[str, float]:
    """Return PIT-correct {ticker: weight} for a subsector on ``as_of_date``.

    A tier whose ticker lacks ``min_history_months`` of history as of the date is
    dropped (weight 0) and its weight is redistributed proportionally to the
    surviving tiers.  Mirrors the plan's downgrade rule
    (backup2 -> backup1 -> primary).

    Parameters
    ----------
    subsector : str
    as_of_date : date | str | pd.Timestamp
    min_history_months : int
        Minimum history required to include a tier (default 24).
    first_available : dict, optional
        {ticker: first_trading_date} measured from real price data.  Falls back
        to ``IPO_DATES`` when a ticker is absent.

    Returns
    -------
    dict[str, float]
        Weights summing to 1.0 over the surviving tickers (empty -> {primary:1}).
    """
    as_of = pd.Timestamp(as_of_date).date() if not isinstance(as_of_date, date) else as_of_date
    unavailable = set(unavailable or ())
    raw = subsector_weights(subsector)  # {ticker: 0.80/0.15/0.05} (3 weighted tiers)

    def _avail(t: str) -> bool:
        return (_ticker_available(t, as_of, min_history_months, first_available)
                and t not in unavailable)

    surviving = {t: w for t, w in raw.items() if _avail(t)}

    # ACCIDENT cascade (mechanism B): a weighted tier that HAD enough history but is
    # now in ``unavailable`` (halt / delisting / data outage) hands its weight to the
    # 0%-reserve, if the reserve is itself available.  IPO-history gaps are NOT
    # accidents — they keep the existing proportional-redistribution behaviour (they
    # never reach the reserve).  When ``unavailable`` is empty this whole block is a
    # no-op and the result is byte-identical to the pre-reserve implementation.
    accident_weight = sum(
        w for t, w in raw.items()
        if t in unavailable
        and _ticker_available(t, as_of, min_history_months, first_available)
    )
    if accident_weight > 0:
        reserve = subsector_reserve(subsector)
        if reserve and _avail(reserve):
            surviving[reserve] = surviving.get(reserve, 0.0) + accident_weight

    if not surviving:
        # Degenerate (very early date): force the primary even if short — the
        # caller's price availability check will still gate actual usage.
        primary = subsector_tickers(subsector)[0]
        return {primary: 1.0}
    total = sum(surviving.values())
    return {t: w / total for t, w in surviving.items()}


def universe_as_dataframe() -> pd.DataFrame:
    """Full subsector universe as a DataFrame (one row per subsector)."""
    rows = []
    for s in SUBSECTORS:
        m = _meta(s)
        rows.append({
            "subsector": s,
            "display": m.display,
            "cycle": m.cycle,
            "lead_lag_months": m.lead_lag,
            "primary": m.tickers[0],
            "backup1": m.tickers[1],
            "backup2": m.tickers[2],
            "cost_bps": round(subsector_cost_bps(s), 3),
            "defensive": s in DEFENSIVE_SUBSECTORS,
        })
    return pd.DataFrame(rows).set_index("subsector")


def validate_date_for_universe(d: date, strict: bool = True) -> bool:
    """Check whether ``d`` is on/after UNIVERSE_START."""
    valid = d >= UNIVERSE_START
    if not valid and strict:
        raise ValueError(
            f"Date {d} is before AISS UNIVERSE_START ({UNIVERSE_START}). "
            "Set the backtest start to 2019-01-01 or later."
        )
    return valid


if __name__ == "__main__":
    pd.set_option("display.width", 160)
    pd.set_option("display.max_columns", 20)
    print("=== AISS Subsector Universe ===")
    print(universe_as_dataframe())
    print(f"\nUnique single-stock tickers ({len(all_tickers())}): {all_tickers()}")
    print(f"Benchmarks: {benchmark_tickers()}")
    print(f"CapEx-pulse tickers: {CAPEX_PULSE_TICKERS}")
    print("\nEffective weights for ai_gpu on a few dates (IPO-aware):")
    for d in ("2019-06-30", "2023-12-31", "2024-12-31"):
        print(f"  {d}: {effective_weights('ai_gpu', d)}")
    print("\nSubsector blended cost (bps):")
    for s in subsector_names():
        print(f"  {s:16} {subsector_cost_bps(s):.2f} bps")
