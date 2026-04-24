"""
Transaction Cost Model
======================
Estimates round-trip transaction costs for sector ETF trades.

Cost components:
    1. Bid-ask spread  : 1–3 bps (by liquidity tier)
    2. Market impact   : 2–5 bps per $5M (by liquidity tier)
    3. ETF expense ratio: 9 bps/yr (pass-through, applied to held position daily)

Total one-way costs by tier:
    Tier 1 (XLE, XLK, XLF, XLV)    : 3 bps
    Tier 2 (XLB, XLI, XLY, XLP, XLU): 5 bps
    Tier 3 (XLC, XLRE)               : 8 bps

Annual turnover × one-way cost ≈ 30–60 bps total transaction drag.

Note: Commission is omitted (institutional ETF trading is effectively free,
or included in the spread estimate).
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cost constants (basis points, one-way)
# ---------------------------------------------------------------------------

TIER_COST_BPS: Dict[int, float] = {1: 3.0, 2: 5.0, 3: 8.0}
ETF_ANNUAL_FEE_BPS: float = 9.0   # Expense ratio for all SPDR sector ETFs

# Ticker → tier mapping (from universe.py)
TICKER_TIER: Dict[str, int] = {
    "XLE": 1, "XLK": 1, "XLF": 1, "XLV": 1,
    "XLB": 2, "XLI": 2, "XLY": 2, "XLP": 2, "XLU": 2,
    "XLC": 3, "XLRE": 3,
}


def get_one_way_cost_bps(ticker: str) -> float:
    """Return one-way transaction cost in basis points for a given ticker."""
    tier = TICKER_TIER.get(ticker, 2)  # Default to tier 2 if unknown
    return TIER_COST_BPS[tier]


def get_round_trip_cost_bps(ticker: str) -> float:
    """Return round-trip cost (entry + exit) in basis points."""
    return get_one_way_cost_bps(ticker) * 2


# ---------------------------------------------------------------------------
# Transaction cost calculation
# ---------------------------------------------------------------------------

def compute_transaction_costs(
    prev_weights: pd.Series,
    new_weights: pd.Series,
    portfolio_value: float,
    prices: Optional[pd.Series] = None,
) -> Dict[str, float]:
    """
    Compute the dollar transaction cost of a rebalance.

    Parameters
    ----------
    prev_weights : pd.Series
        Previous portfolio weights (index = tickers).
    new_weights : pd.Series
        New target weights (index = tickers).
    portfolio_value : float
        Total portfolio value in USD.
    prices : pd.Series, optional
        Current prices (not used in this model, included for future enhancement).

    Returns
    -------
    dict with keys:
        "total_cost_usd"      : Total dollar transaction cost
        "total_cost_bps"      : Cost as basis points of portfolio value
        "turnover_pct"        : Single-side turnover (%)
        "cost_by_ticker"      : Per-ticker cost in USD
    """
    all_tickers = prev_weights.index.union(new_weights.index)
    prev = prev_weights.reindex(all_tickers, fill_value=0.0)
    new = new_weights.reindex(all_tickers, fill_value=0.0)

    weight_changes = (new - prev).abs()  # Absolute weight change

    cost_by_ticker = {}
    total_cost_usd = 0.0

    for ticker in all_tickers:
        delta_w = weight_changes[ticker]
        if delta_w < 1e-6:
            cost_by_ticker[ticker] = 0.0
            continue

        one_way_bps = get_one_way_cost_bps(ticker)
        trade_value = delta_w * portfolio_value
        cost_usd = trade_value * (one_way_bps / 10000.0)
        cost_by_ticker[ticker] = cost_usd
        total_cost_usd += cost_usd

    total_cost_bps = (total_cost_usd / portfolio_value * 10000) if portfolio_value > 0 else 0
    turnover_pct = float(weight_changes.sum() / 2.0 * 100)  # Single-side

    return {
        "total_cost_usd": total_cost_usd,
        "total_cost_bps": total_cost_bps,
        "turnover_pct": turnover_pct,
        "cost_by_ticker": cost_by_ticker,
    }


def compute_daily_fee_drag(
    weights: pd.Series,
    portfolio_value: float,
    annual_fee_bps: float = ETF_ANNUAL_FEE_BPS,
    trading_days: int = 252,
) -> float:
    """
    Compute daily ETF expense ratio drag (as USD).

    All SPDR sector ETFs have 9 bps annual fee.
    Daily drag = portfolio_value × (fee_bps / 10000) / 252

    Parameters
    ----------
    weights : pd.Series
        Current portfolio weights.
    portfolio_value : float
        Total portfolio value.
    annual_fee_bps : float
        Annual management fee in bps (default 9 bps).
    trading_days : int
        Trading days per year (default 252).

    Returns
    -------
    float
        Dollar fee drag for this trading day.
    """
    invested_pct = weights.sum()  # May be < 1 if cash allocation exists
    return portfolio_value * invested_pct * (annual_fee_bps / 10000.0) / trading_days


# ---------------------------------------------------------------------------
# Annual cost estimate
# ---------------------------------------------------------------------------

def estimate_annual_costs(
    turnover_history: pd.Series,
    avg_portfolio_value: float = 1_000_000.0,
) -> Dict[str, float]:
    """
    Estimate annualized transaction costs from historical turnover data.

    Parameters
    ----------
    turnover_history : pd.Series
        Monthly single-side turnover (fraction, e.g. 0.30 = 30%).
    avg_portfolio_value : float
        Average portfolio size for dollar estimates.

    Returns
    -------
    dict with annualized cost estimates.
    """
    annual_turnover = float(turnover_history.mean() * 12)  # Monthly → annual

    # Assume blended cost of ~4.5 bps one-way (weighted average across tiers)
    blended_cost_bps = 4.5
    transaction_cost_bps = annual_turnover * blended_cost_bps * 2  # Round trip
    fee_drag_bps = ETF_ANNUAL_FEE_BPS
    total_cost_bps = transaction_cost_bps + fee_drag_bps

    return {
        "annual_turnover_pct": round(annual_turnover * 100, 1),
        "transaction_cost_bps": round(transaction_cost_bps, 1),
        "fee_drag_bps": round(fee_drag_bps, 1),
        "total_cost_bps": round(total_cost_bps, 1),
        "total_cost_usd_on_1M": round(total_cost_bps / 100 * avg_portfolio_value / 100, 0),
    }
