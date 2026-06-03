"""
stock_decompose.py
==================
Decompose AISS **subsector** target weights into the underlying **individual-stock**
holdings + trades — the executable layer one level below the subsector "asset".

Why this exists (the AISS vs SSRS difference)
---------------------------------------------
SSRS trades 11 GICS **ETFs** directly, so its daily signal stops at the ETF level
— that *is* the tradeable instrument.  AISS's tradeable "asset" is a *synthetic*
80/15/5 subsector basket, which is not directly executable.  So AISS needs **one
extra layer and one extra step**: take the subsector target weights the engine
produces and decompose them, PIT-correctly, into the real single-stock holdings
and orders you would actually place.

Mirrors the backtest's ``portfolio_record._write_stock_decomp_sheets`` (which adds
``*_stock_decomp`` sheets to every subsector-keyed sheet), but for the *live*
daily signal (report + inventory).

Key correctness points
-----------------------
* **PIT within-weights** via ``universe.effective_weights`` — a late-IPO backup
  tier (ARM/ALAB/CRDO/GFS) is dropped until it has ``min_history_months`` and the
  surviving tiers are renormalised, exactly as the basket is built.
* **Per-ticker aggregation** — a stock can sit in two subsectors (ARM is ai_gpu
  backup2 5% **and** logic_cpu backup1 15%), so the executable order layer sums a
  ticker's weight across every held subsector → one combined order per ticker.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Dict, List, Optional

import pandas as pd

from semiconductor_strategy.data import universe as U

_TIER_ROLES = ["primary", "backup1", "backup2"]


def recently_unavailable(stock_prices: pd.DataFrame, as_of_date,
                         stale_days: int = 10) -> set:
    """Tickers with NO valid price in the trailing ``stale_days`` window as of date.

    Detects halts / delistings / data outages (an "accident") so the caller can
    route the affected subsector's weight to its 0%-reserve.  Short 1–2 day gaps
    do not trigger.  Mirrors the live-mask rule in ``loader.build_subsector_prices``.
    """
    if stock_prices is None or stock_prices.empty:
        return set()
    as_of = pd.Timestamp(as_of_date)
    window = stock_prices.loc[:as_of].tail(stale_days)
    if window.empty:
        return set()
    return {t for t in window.columns if window[t].notna().sum() == 0}


def decompose_to_stocks(
    subsector_weights: Dict[str, float],
    capital: float,
    as_of_date,
    stock_prices_today: pd.Series,
    first_available: Optional[Dict[str, date]] = None,
    min_history_months: int = 24,
    unavailable: Optional[set] = None,
) -> dict:
    """Decompose held-subsector target weights into individual-stock holdings.

    Parameters
    ----------
    subsector_weights : {subsector: portfolio_weight}
        The engine's final (post-risk) subsector target weights.  Zero/empty
        entries are skipped.
    capital : float
        Portfolio capital (USD) used to turn weights into share counts.
    as_of_date : date | str | pd.Timestamp
        Signal date — drives the PIT ``effective_weights`` IPO gating.
    stock_prices_today : pd.Series
        {ticker: latest adjusted-close price}.  Missing/0 price → 0 shares.
    first_available : dict, optional
        {ticker: first_trading_date} measured from real price data (PIT).  Falls
        back to ``universe.IPO_DATES`` inside ``effective_weights`` when absent.
    min_history_months : int
        Backup-tier entry threshold (default 24, matches config/basket build).

    Returns
    -------
    dict
        ``breakdown`` : list of one row per (subsector, ticker) —
            {subsector, ticker, tier_role, within_weight, portfolio_weight,
             price, target_value, target_shares}
        ``by_ticker`` : {ticker: {portfolio_weight, target_value, price,
             target_shares, subsectors}} aggregated across subsectors (the
             executable order layer).
    """
    breakdown: List[dict] = []
    agg: Dict[str, dict] = {}

    def _price(tk: str) -> float:
        if stock_prices_today is None:
            return 0.0
        try:
            p = float(stock_prices_today.get(tk, 0.0))
        except Exception:
            p = 0.0
        return p if (p == p and p > 0) else 0.0  # NaN-safe

    for sub, sub_w in subsector_weights.items():
        if sub_w is None or sub_w <= 0:
            continue
        eff = U.effective_weights(
            sub, as_of_date,
            min_history_months=min_history_months,
            first_available=first_available,
            unavailable=unavailable,
        )
        # tier role (primary/backup1/backup2/reserve) for display, in basket order
        base_order = list(U.subsector_weights(sub).keys())
        role = {tk: (_TIER_ROLES[i] if i < len(_TIER_ROLES) else f"tier{i+1}")
                for i, tk in enumerate(base_order)}
        _res = U.subsector_reserve(sub)
        if _res:
            role[_res] = "reserve"

        for tk, within in eff.items():
            port_w = sub_w * within
            price = _price(tk)
            tgt_val = port_w * capital
            shares = int(math.floor(tgt_val / price)) if price > 0 else 0
            breakdown.append({
                "subsector":        sub,
                "ticker":           tk,
                "tier_role":        role.get(tk, ""),
                "within_weight":    round(within, 6),
                "portfolio_weight": round(port_w, 6),
                "price":            round(price, 4),
                "target_value":     round(tgt_val, 2),
                "target_shares":    shares,
            })
            a = agg.setdefault(tk, {
                "portfolio_weight": 0.0, "target_value": 0.0,
                "price": round(price, 4), "subsectors": [],
            })
            a["portfolio_weight"] += port_w
            a["target_value"]     += tgt_val
            if sub not in a["subsectors"]:
                a["subsectors"].append(sub)

    # Executable share count from the *aggregated* dollar target (ARM in two
    # subsectors becomes a single order, floored once).
    for tk, a in agg.items():
        price = a["price"]
        a["portfolio_weight"] = round(a["portfolio_weight"], 6)
        a["target_value"]     = round(a["target_value"], 2)
        a["target_shares"]    = int(math.floor(a["target_value"] / price)) if price > 0 else 0

    return {"breakdown": breakdown, "by_ticker": agg}


def build_stock_trades(
    by_ticker: Dict[str, dict],
    prev_stock_holdings: Optional[Dict[str, dict]],
) -> List[dict]:
    """Per-ticker delta orders between target shares and previously-held shares.

    ``prev_stock_holdings`` is the inventory ``stock_holdings`` dict from the last
    run: {ticker: {shares, last_price, ...}}.  Returns one trade per ticker whose
    share count changes (BUY / SELL with dollar value).
    """
    prev = prev_stock_holdings or {}
    trades: List[dict] = []
    for tk in sorted(set(by_ticker) | set(prev)):
        tgt = int(by_ticker.get(tk, {}).get("target_shares", 0))
        cur = int(prev.get(tk, {}).get("shares", 0))
        delta = tgt - cur
        if delta == 0:
            continue
        price = float(by_ticker.get(tk, {}).get("price", 0.0)) \
            or float(prev.get(tk, {}).get("last_price", 0.0))
        trades.append({
            "ticker":         tk,
            "side":           "BUY" if delta > 0 else "SELL",
            "delta_shares":   abs(delta),
            "current_shares": cur,
            "target_shares":  tgt,
            "price":          round(price, 4),
            "est_value":      round(abs(delta) * price, 2),
        })
    return trades


def stock_holdings_from_by_ticker(by_ticker: Dict[str, dict]) -> Dict[str, dict]:
    """Build the inventory ``stock_holdings`` record from a decomposition."""
    return {
        tk: {
            "subsectors":       a.get("subsectors", []),
            "portfolio_weight": a.get("portfolio_weight", 0.0),
            "shares":           int(a.get("target_shares", 0)),
            "last_price":       a.get("price", 0.0),
            "target_value":     a.get("target_value", 0.0),
        }
        for tk, a in by_ticker.items()
    }
