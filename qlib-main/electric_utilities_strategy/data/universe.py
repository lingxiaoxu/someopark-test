"""
AEUS Universe Definition
========================
AI Electric Utilities Strategy (AEUS) investment universe.

Structure (AEUS_PLAN §2.1)
--------------------------
10 electric-power *subsectors* spanning the AI-power value chain upstream →
downstream, each a weighted basket of 4-5 individual stocks:

    members = [(ticker, base_w), ...]        # base_w sums to 1.0 per subsector
    reserve = 0%-weight vetted spare          # promoted only on accident

The **tradeable asset of the strategy is the subsector** (a synthetic weighted
basket), mirroring the AISS design ("板块间动态再平衡,板块内先验配比").  Signals,
the optimizer, risk overlays, the backtest engine, and all reports operate at
the subsector level (10 "assets").  The 41 underlying single stocks matter for
(a) which prices to download, (b) constructing basket return series
(``loader.build_subsector_prices``), and (c) the execution decomposition layer.

Generalisation over AISS (AEUS_PLAN §2.1 逐行核验)
--------------------------------------------------
AISS hard-coded a 3-tier 80/15/5 structure (primary/backup1/backup2).  AEUS
generalises to an N-member ``base_w`` vector per subsector — 80/15/5 is the
3-member special case.  ``stock_decompose`` (tier4+ naming) and
``loader.build_subsector_prices`` (anchor + list iteration) were verified
N-member-safe; only this module's internal structure changed.  All public API
functions keep their AISS names and semantics.

Intra-subsector purity tilt (AEUS_PLAN §2.5)
--------------------------------------------
Each stock carries a static ``purity`` score in [0, 1] — its exposure to the
"AI power" theme (GEV=1.0 pure grid, EMR=0.35 diversified industrial…).  The
signal layer tilts intra-subsector weights by κ × purity × graph-score; κ=0
reproduces static base_w byte-for-byte (the regression anchor).  Purity lives
HERE as the single source of truth (the STOCK_TIER lesson).

Point-in-time IPO handling
--------------------------
Several constituents IPO'd/spun off after the backtest start (GEV 2024-04,
CEG 2022-02, VRT 2020-02, OKLO 2024-05, SMR 2022-05, NXT 2023-02, FLNC
2021-10, TLN 2023-07, ARRY 2020-10, BE 2018-07).  ``effective_weights`` drops
any member whose ticker lacks ``min_history_months`` of data as-of a given
date and renormalises the survivors — no look-ahead, no fabricated history.
IPO_DATES below are conservative fallbacks; real first-trading dates from the
price store override them at runtime (``first_available``).

Isolation
---------
This module is part of ``qlib-main/electric_utilities_strategy`` and shares
nothing with the AISS strategy beyond a parallel interface.  Price data lives
in ``price_data/elec_strategy/`` (completely separate from semi_strategy).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional

import pandas as pd

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Earliest sensible backtest start.  The long-history anchors (ETN / BWXT / PWR /
# VST / NEE / AEE / KMI / AWK / VRT-predecessors…) all trade well before this;
# newer IPOs/spin-offs are phased in via effective_weights().
UNIVERSE_START: date = date(2019, 1, 1)

# Legacy AISS 3-tier default — kept ONLY as the special-case reference; AEUS
# subsectors define their own base_w vectors in SUBSECTORS.
SUBSECTOR_TIER_WEIGHTS: tuple = (0.80, 0.15, 0.05)

# Benchmarks — XLU (utilities beta) and GRID (smart-grid capex alpha) are THE
# strategy's hurdle (must beat both, AEUS_PLAN §2.2); SPY is the broad-market
# reference for the information ratio.  The 50/50 XLU+GRID daily-rebalanced
# blend is the active-return benchmark (built in loader).
BENCHMARK_TICKERS: List[str] = ["XLU", "GRID", "SPY"]
PRIMARY_BENCHMARK: str = "XLU"

# CapEx-pulse cloud names (NOT part of the tradeable universe; pulled separately
# by company_signals.py).  Shared upstream driver with the AISS strategy — the
# same four hyperscalers drive both chip demand and datacenter power demand.
CAPEX_PULSE_TICKERS: List[str] = ["MSFT", "GOOGL", "META", "AMZN"]

# Approximate first-trading dates for late IPOs / spin-offs (used by
# effective_weights when no live price history is supplied).  Conservative —
# a few days after listing.  Long-history names are deliberately absent.
IPO_DATES: Dict[str, date] = {
    # weighted members
    "GEV":  date(2024, 4, 2),    # GE Vernova spin-off
    "CEG":  date(2022, 2, 2),    # Constellation spin-off from Exelon
    "VRT":  date(2020, 2, 10),   # Vertiv via SPAC (GSAH merger)
    "OKLO": date(2024, 5, 10),   # Oklo via SPAC
    "SMR":  date(2022, 5, 3),    # NuScale via SPAC
    "NXT":  date(2023, 2, 9),    # Nextracker IPO
    "FLNC": date(2021, 10, 28),  # Fluence IPO
    "TLN":  date(2023, 7, 6),    # Talen relisting post-Chapter-11
    "ARRY": date(2020, 10, 15),  # Array Technologies IPO
    "BE":   date(2018, 7, 25),   # Bloom Energy IPO
    "TT":   date(2020, 3, 2),    # Trane Technologies (Ingersoll-Rand rename/split)
    "CARR": date(2020, 4, 3),    # Carrier spin-off from UTC
    "WTRG": date(2020, 2, 3),    # Essential Utilities (Aqua America rename)
    # reserves
    "SHLS": date(2021, 1, 29),   # Shoals IPO (reserve, renewables_storage)
    "NXE":  date(2016, 5, 10),   # NexGen NYSE listing (reserve, nuclear_fuel)
}


# ---------------------------------------------------------------------------
# Subsector definitions (10 subsectors, 41 weighted members + 1 reserve each)
# AEUS_PLAN §2.1 final roster (user-decided 2026-08-28/29).
#
# members  : [(ticker, base_w), ...] in basket order.  The FIRST member is the
#            basket anchor (never history-gated; anchors the basket from its
#            first available date — all anchors are long-history names).
# reserve  : (ticker, 0.0) — vetted, data-ready spare, promoted ONLY when a
#            weighted member becomes unusable (halt/delisting/data outage);
#            see effective_weights(unavailable=...).
# purity   : {ticker: score in [0,1]} — AI-power theme purity (§2.5), DRAFT
#            values pending user review; used only when purity-tilt κ > 0.
# cycle    : economic-link label; lead_lag: months vs the AI-capex signal.
# ---------------------------------------------------------------------------

SUBSECTORS: Dict[str, dict] = {
    "nuclear_fuel": {
        "members": [("BWXT", 0.45), ("LEU", 0.20), ("UUUU", 0.15),
                    ("OKLO", 0.10), ("SMR", 0.10)],
        "reserve": ("NXE", 0.00),   # candidates (docs only): DNN
        "purity":  {"BWXT": 0.70, "LEU": 0.80, "UUUU": 0.70,
                    "OKLO": 1.00, "SMR": 1.00, "NXE": 0.60},
        "cycle":   "nuclear_renaissance",
        "lead_lag": 6,     # months relative to AI-capex signal (+lag / -lead)
        "display": "Nuclear & Uranium Fuel",
    },
    "gas_midstream": {
        "members": [("KMI", 0.35), ("WMB", 0.30), ("OKE", 0.20), ("TRGP", 0.15)],
        "reserve": ("LNG", 0.00),
        "purity":  {"KMI": 0.70, "WMB": 0.70, "OKE": 0.60, "TRGP": 0.60,
                    "LNG": 0.40},
        "cycle":   "gas_fuel_chain",
        "lead_lag": 4,
        "display": "Natural Gas Midstream",
    },
    "grid_equipment": {
        "members": [("ETN", 0.40), ("EMR", 0.25), ("GEV", 0.20), ("POWL", 0.15)],
        "reserve": ("VMI", 0.00),   # candidates (docs only): AZZ, HUBB, ATKR
        "purity":  {"ETN": 0.70, "EMR": 0.35, "GEV": 1.00, "POWL": 0.80,
                    "VMI": 0.50},
        "cycle":   "transformer_bottleneck",
        "lead_lag": 3,
        "display": "Grid Equipment & Transformers",
    },
    "grid_epc": {
        "members": [("PWR", 0.45), ("FIX", 0.25), ("STRL", 0.15), ("MYRG", 0.15)],
        "reserve": ("DY", 0.00),    # candidates (docs only): ACM
        "purity":  {"PWR": 0.90, "FIX": 0.80, "STRL": 0.80, "MYRG": 0.70,
                    "DY": 0.50},
        "cycle":   "interconnection_buildout",
        "lead_lag": 5,
        "display": "Grid Construction & EPC",
    },
    "ipp_wholesale": {
        "members": [("VST", 0.40), ("CEG", 0.30), ("NRG", 0.20), ("TLN", 0.10)],
        "reserve": ("ORA", 0.00),
        "purity":  {"VST": 0.95, "CEG": 0.90, "NRG": 0.70, "TLN": 0.90,
                    "ORA": 0.60},
        "cycle":   "ppa_scarcity_premium",
        "lead_lag": 0,
        "display": "Independent Power Producers",
    },
    "regulated_mega": {
        "members": [("NEE", 0.35), ("SO", 0.25), ("DUK", 0.25), ("AEP", 0.15)],
        "reserve": ("D", 0.00),
        "purity":  {"NEE": 0.60, "SO": 0.30, "DUK": 0.35, "AEP": 0.40,
                    "D": 0.30},
        "cycle":   "rate_base_growth",
        "lead_lag": 9,
        "display": "Regulated Mega Utilities",
    },
    "regional_utility": {
        "members": [("AEE", 0.35), ("LNT", 0.25), ("OGE", 0.20), ("ATO", 0.20)],
        "reserve": ("BKH", 0.00),   # candidates (docs only): AVA
        "purity":  {"AEE": 0.30, "LNT": 0.30, "OGE": 0.30, "ATO": 0.35,
                    "BKH": 0.30},
        "cycle":   "regional_load_growth",
        "lead_lag": 2,
        "display": "Regional Utilities",
    },
    "dc_power_cooling": {
        "members": [("VRT", 0.45), ("TT", 0.25), ("CARR", 0.20), ("BE", 0.10)],
        "reserve": ("AOS", 0.00),   # candidates (docs only): NVT, CAT, CMI
        "purity":  {"VRT": 1.00, "TT": 0.50, "CARR": 0.40, "BE": 0.80,
                    "AOS": 0.30},
        "cycle":   "dc_buildout_direct",
        "lead_lag": 0,
        "display": "Datacenter Power & Cooling",
    },
    "renewables_storage": {
        "members": [("NXT", 0.40), ("FSLR", 0.25), ("FLNC", 0.20), ("ARRY", 0.15)],
        "reserve": ("SHLS", 0.00),  # candidates (docs only): CSIQ, CWEN, BEPC
        "purity":  {"NXT": 0.90, "FSLR": 0.80, "FLNC": 0.90, "ARRY": 0.70,
                    "SHLS": 0.70},
        "cycle":   "green_compute_pledges",
        "lead_lag": 3,
        "display": "Utility-Scale Renewables & Storage",
    },
    "water_cooling": {
        "members": [("AWK", 0.40), ("WTRG", 0.30), ("AWR", 0.15), ("CWT", 0.15)],
        "reserve": ("YORW", 0.00),   # SJW 2025-05 被收购退市(价格数据实证),换 York Water
        "purity":  {"AWK": 0.40, "WTRG": 0.30, "AWR": 0.25, "CWT": 0.25,
                    "YORW": 0.25},
        "cycle":   "cooling_water_defensive",
        "lead_lag": 6,
        "display": "Water Utilities & Cooling",
    },
}

# Subsector groups consumed by signals/composite.py (AEUS_PLAN §3.4)
AI_CYCLE_SUBSECTORS: List[str] = ["ipp_wholesale", "dc_power_cooling", "grid_equipment"]
DEFENSIVE_SUBSECTORS: List[str] = ["regulated_mega", "regional_utility", "water_cooling"]

# Per-subsector capex beta (composite.py CAPEX_BETA reads this; §3.4)
CAPEX_BETA: Dict[str, float] = {
    "ipp_wholesale": 1.0, "dc_power_cooling": 0.9, "grid_equipment": 0.7,
    "grid_epc": 0.6, "nuclear_fuel": 0.5, "gas_midstream": 0.4,
    "renewables_storage": 0.4, "regulated_mega": 0.1,
    "regional_utility": -0.2, "water_cooling": -0.3,
}

# ---------------------------------------------------------------------------
# Per-stock liquidity tiers → one-way transaction cost (bps).
# SINGLE SOURCE OF TRUTH (config costs.tier_* mirrors this for reference only).
# AEUS_PLAN §2.3.
# ---------------------------------------------------------------------------
STOCK_TIER: Dict[str, int] = {
    # Tier 1 (3 bps) — mega/large-cap, deep liquidity
    "NEE": 1, "SO": 1, "DUK": 1, "AEP": 1, "VST": 1, "CEG": 1, "NRG": 1,
    "ETN": 1, "EMR": 1, "GEV": 1, "PWR": 1, "VRT": 1, "TT": 1, "CARR": 1,
    "FSLR": 1, "KMI": 1, "WMB": 1, "OKE": 1, "AWK": 1,
    # Tier 2 (5 bps) — large/mid-cap
    "D": 2, "AEE": 2, "LNT": 2, "ATO": 2, "FIX": 2, "BWXT": 2, "NXT": 2,
    "TLN": 2, "STRL": 2, "TRGP": 2, "WTRG": 2,
    # Tier 3 (8 bps) — mid/small-cap or recent IPO
    "OGE": 3, "BKH": 3, "POWL": 3, "VMI": 3, "MYRG": 3, "DY": 3, "LEU": 3, "UUUU": 3,
    "OKLO": 3, "SMR": 3, "NXE": 3, "ORA": 3, "FLNC": 3, "ARRY": 3, "SHLS": 3,
    "BE": 3, "AOS": 3, "AWR": 3, "CWT": 3, "YORW": 3, "LNG": 3,
}
TIER_COST_BPS: Dict[int, int] = {1: 3, 2: 5, 3: 8}


# ---------------------------------------------------------------------------
# Lightweight metadata dataclass (N-member generalisation of the AISS 3-tuple)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SubsectorMeta:
    name: str
    display: str
    cycle: str
    lead_lag: int
    tickers: tuple          # (member1, member2, ..., memberN) in basket order
    weights: tuple          # (w1, w2, ..., wN), sums to 1.0


def _meta(name: str) -> SubsectorMeta:
    d = SUBSECTORS[name]
    return SubsectorMeta(
        name=name,
        display=d.get("display", name),
        cycle=d["cycle"],
        lead_lag=d["lead_lag"],
        tickers=tuple(t for t, _ in d["members"]),
        weights=tuple(w for _, w in d["members"]),
    )


# ---------------------------------------------------------------------------
# Public API (names and semantics preserved from AISS)
# ---------------------------------------------------------------------------

def subsector_names() -> List[str]:
    """Ordered list of the 10 subsector keys (the strategy's tradeable assets)."""
    return list(SUBSECTORS.keys())


def subsector_display(name: str) -> str:
    """Human-readable label for a subsector (for plots / reports)."""
    return SUBSECTORS[name].get("display", name)


def subsector_tickers(subsector: str) -> List[str]:
    """Return the weighted member tickers in basket order (anchor first)."""
    return [t for t, _ in SUBSECTORS[subsector]["members"]]


def subsector_weights(subsector: str) -> Dict[str, float]:
    """Return {ticker: base_w} for a subsector's weighted members.

    AEUS subsectors never duplicate a ticker within themselves, so one entry
    per member.  Weights sum to 1.0.
    """
    return {t: w for t, w in SUBSECTORS[subsector]["members"]}


def subsector_reserve(subsector: str) -> Optional[str]:
    """Return the 0%-weight reserve ticker for a subsector (None if undefined).

    The reserve is a vetted, data-ready spare that carries 0% normally and is
    only promoted into the basket when one of the weighted members becomes
    unusable (halt / delisting / data outage).  It is deliberately kept OUT of
    ``members``, so the normal basket construction is unchanged.
    """
    r = SUBSECTORS[subsector].get("reserve")
    return r[0] if r else None


def reserve_tickers() -> List[str]:
    """All reserve tickers (de-duped, subsector order)."""
    seen: List[str] = []
    for s in SUBSECTORS:
        r = subsector_reserve(s)
        if r and r not in seen:
            seen.append(r)
    return seen


def get_purity(ticker: str, subsector: Optional[str] = None) -> float:
    """AI-power theme purity score in [0, 1] (AEUS_PLAN §2.5).

    Looks up the subsector-local purity table (a ticker in two subsectors could
    in principle carry different scores; AEUS currently has no duplicates).
    Unknown tickers return 0.0 — an unknown name gets no tilt, never a crash.
    """
    if subsector is not None:
        return float(SUBSECTORS[subsector].get("purity", {}).get(ticker, 0.0))
    for s in SUBSECTORS:
        p = SUBSECTORS[s].get("purity", {})
        if ticker in p:
            return float(p[ticker])
    return 0.0


def subsector_of(ticker: str, include_reserve: bool = True) -> List[str]:
    """Return the subsector(s) a ticker belongs to.

    With ``include_reserve`` also matches reserve slots (so a promoted reserve
    resolves to its subsector during stock aggregation).
    """
    hits = [s for s in SUBSECTORS if ticker in subsector_tickers(s)]
    if include_reserve:
        hits += [s for s in SUBSECTORS
                 if subsector_reserve(s) == ticker and s not in hits]
    if not hits:
        raise KeyError(f"Ticker {ticker!r} is not in the AEUS universe.")
    return hits


def all_tickers(include_benchmark: bool = False, include_reserve: bool = True) -> List[str]:
    """All unique single-stock tickers in the universe.

    Deterministically ordered: subsector order, member order, first occurrence.
    ``include_reserve`` appends the 0%-weight reserve tickers (so their prices
    are downloaded and the spare is data-ready); they never affect the normal
    basket.
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
    """Benchmarks: XLU (primary hurdle), GRID (secondary hurdle), SPY (broad)."""
    return list(BENCHMARK_TICKERS)


def lead_lag_months(subsector: str) -> int:
    """Lead/lag (months) vs the AI-capex signal: positive = lags, negative = leads."""
    return SUBSECTORS[subsector]["lead_lag"]


# --- compatibility shims so copied engine code keeps working unchanged -------

def get_tickers(include_benchmark: bool = False) -> List[str]:
    """Engine-compatible alias.

    The strategy's *assets* are subsectors; several copied modules historically
    call ``get_tickers()`` expecting the tradeable-asset list.  Here that means
    the 10 subsector names (optionally + benchmarks).  Use ``all_tickers()``
    when you specifically need the 41 single-stock tickers.
    """
    names = subsector_names()
    if include_benchmark:
        names = names + benchmark_tickers()
    return names


def get_liquidity_tier_stock(ticker: str) -> int:
    return STOCK_TIER.get(ticker, 3)


def subsector_cost_bps(subsector: str) -> float:
    """base_w-blended one-way transaction cost (bps) for a subsector basket."""
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
    """Reference 'market' weights over the 10 subsectors (equal-weight proxy).

    AEUS is a satellite AI-power strategy with no natural S&P sector weights;
    callers that need a benchmark-deviation reference get an equal weighting
    across subsectors.  Used only by optional optimizer modes.
    """
    n = len(SUBSECTORS)
    return pd.Series({s: 1.0 / n for s in SUBSECTORS}, name="ref_weight")


# ---------------------------------------------------------------------------
# Point-in-time effective weights (IPO-aware downgrade + renormalisation)
# Mechanism inherited verbatim from AISS; operates on N-member base_w dicts.
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

    A member whose ticker lacks ``min_history_months`` of history as of the
    date is dropped (weight 0) and its weight is redistributed proportionally
    to the surviving members (the AISS downgrade rule, N-member generalised).

    ACCIDENT cascade (mechanism B, inherited verbatim): a weighted member that
    HAD enough history but is now in ``unavailable`` (halt / delisting / data
    outage) hands its weight to the 0%-reserve, if the reserve is itself
    available.  IPO-history gaps are NOT accidents — they keep the proportional
    redistribution and never reach the reserve.  With ``unavailable`` empty the
    block is a no-op and the result is byte-identical to plain base_w
    renormalisation.

    Returns
    -------
    dict[str, float]
        Weights summing to 1.0 over the surviving tickers (empty -> {anchor: 1}).
    """
    as_of = pd.Timestamp(as_of_date).date() if not isinstance(as_of_date, date) else as_of_date
    unavailable = set(unavailable or ())
    raw = subsector_weights(subsector)  # {ticker: base_w} (N weighted members)

    def _avail(t: str) -> bool:
        return (_ticker_available(t, as_of, min_history_months, first_available)
                and t not in unavailable)

    surviving = {t: w for t, w in raw.items() if _avail(t)}

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
        # Degenerate (very early date): force the anchor even if short — the
        # caller's price availability check still gates actual usage.
        anchor = subsector_tickers(subsector)[0]
        return {anchor: 1.0}
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
            "members": " ".join(f"{t}:{w:.2f}" for t, w in zip(m.tickers, m.weights)),
            "n_members": len(m.tickers),
            "reserve": subsector_reserve(s) or "",
            "cost_bps": round(subsector_cost_bps(s), 3),
            "defensive": s in DEFENSIVE_SUBSECTORS,
        })
    return pd.DataFrame(rows).set_index("subsector")


def validate_date_for_universe(d: date, strict: bool = True) -> bool:
    """Check whether ``d`` is on/after UNIVERSE_START."""
    valid = d >= UNIVERSE_START
    if not valid and strict:
        raise ValueError(
            f"Date {d} is before AEUS UNIVERSE_START ({UNIVERSE_START}). "
            "Set the backtest start to 2019-01-01 or later."
        )
    return valid


if __name__ == "__main__":
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 20)
    print("=== AEUS Subsector Universe ===")
    print(universe_as_dataframe())
    print(f"\nUnique single-stock tickers ({len(all_tickers())}): {all_tickers()}")
    print(f"Benchmarks: {benchmark_tickers()}")
    print(f"CapEx-pulse tickers: {CAPEX_PULSE_TICKERS}")
    print("\nEffective weights for grid_equipment on a few dates (IPO-aware):")
    for d in ("2019-06-30", "2024-06-30", "2026-06-30"):
        print(f"  {d}: {effective_weights('grid_equipment', d)}")
    print("\nSubsector blended cost (bps):")
    for s in subsector_names():
        print(f"  {s:20} {subsector_cost_bps(s):.2f} bps")
    print("\nBase-weight sums (must all be 1.0):")
    for s in subsector_names():
        print(f"  {s:20} {sum(subsector_weights(s).values()):.4f}")
