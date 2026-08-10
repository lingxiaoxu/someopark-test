"""Crypto cost model: fees + funding + depth-aware slippage (Plan 00 §5).

Replaces the equity `backtest/costs.py` bps-tier model (kept as a read-only
template) with the three crypto components:

  1. **Fees** — maker/taker as a fraction of notional. Two scenarios carried
     everywhere (Plan 07 §1): ``zero`` (launch promo) and ``projected``
     (measured schedule). Projected defaults come from the probe-verified
     fee-tier table (maker 0.0005); refreshed from the authed
     ``/margin/fee_tiers`` snapshot when available at
     price_data/kalshi/refdata/fee_tiers.json.
  2. **Funding** — every 8h (04/12/20 UTC): payment = rate × contracts ×
     contract_price. Positive rate ⇒ longs pay shorts (sign convention:
     returned value is the P&L of the position holder).
  3. **Slippage** — depth-aware: fills WALK THE BOOK, never a flat bps guess.

Annualization everywhere: 365 days (see backtest.metrics.TRADING_DAYS).
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from decimal import Decimal

from crypto_trading.crypto_common.config import PRICE_DATA

logger = logging.getLogger(__name__)

FEE_SNAPSHOT = PRICE_DATA / "kalshi" / "refdata" / "fee_tiers.json"

# Probe-verified defaults (2026-07-07): maker 0.0005 from the fee-tier table.
# Taker unverified until the authed snapshot lands — conservative placeholder.
DEFAULT_MAKER_RATE = 0.0005
DEFAULT_TAKER_RATE = 0.0010


# ── fees ────────────────────────────────────────────────────────────────────

#: Official CFTC-filed perp fee tiers (2026-06-24 filing, effective 2026-07-08):
#: tier → (maker_bps, taker_bps). Volume thresholds: T3 ≥$1M, T4 ≥$3M, T5 ≥$10M
#: 30-day trailing (perps + prediction combined, maker + taker both count).
FEE_TIERS_BPS = {0: (5.0, 12.0), 1: (4.0, 10.0), 2: (3.2, 8.0), 3: (2.4, 6.0),
                 4: (2.0, 5.0), 5: (1.6, 4.0), 6: (1.4, 3.5), 7: (1.2, 3.2)}


def load_fee_rates(ticker: str) -> tuple[float, float]:
    """(maker, taker) fraction-of-notional for a ticker.

    Env override for tier studies: ``CRYPTO_FEE_TIER=<n>`` applies the official
    tier table above; ``CRYPTO_MAKER_BPS``/``CRYPTO_TAKER_BPS`` set exact rates.
    No env → recorded snapshot / probe defaults (tier-0 reality), unchanged.
    """
    t = os.environ.get("CRYPTO_FEE_TIER")
    if t is not None:
        m, k = FEE_TIERS_BPS[int(t)]
        return m / 1e4, k / 1e4
    mb, tb = os.environ.get("CRYPTO_MAKER_BPS"), os.environ.get("CRYPTO_TAKER_BPS")
    if mb is not None or tb is not None:
        return (float(mb or DEFAULT_MAKER_RATE * 1e4) / 1e4,
                float(tb or DEFAULT_TAKER_RATE * 1e4) / 1e4)
    return _load_fee_rates_snapshot(ticker)


def _load_fee_rates_snapshot(ticker: str) -> tuple[float, float]:
    """(maker, taker) fraction-of-notional for a ticker.

    Reads the authed fee_tiers snapshot when present (written by
    ``ledger.snapshot_fee_tiers``, Plan 07); falls back to probe defaults.
    """
    if FEE_SNAPSHOT.exists():
        try:
            data = json.loads(FEE_SNAPSHOT.read_text())
            maker = (data.get("maker_fee_rates") or {})
            taker = (data.get("taker_fee_rates") or {})
            # fee table keys carry instance suffixes (KXBTCPERP1) — prefix-match
            def _rate(table: dict, default: float) -> float:
                for k, v in table.items():
                    if k.startswith(ticker):
                        return float(v)
                return default
            return _rate(maker, DEFAULT_MAKER_RATE), _rate(taker, DEFAULT_TAKER_RATE)
        except Exception:
            logger.exception("bad fee snapshot %s — using defaults", FEE_SNAPSHOT)
    return DEFAULT_MAKER_RATE, DEFAULT_TAKER_RATE


def fee_dollars(notional: float, *, role: str = "taker", scenario: str = "projected",
                ticker: str = "KXBTCPERP") -> float:
    """Fee for one fill. ``scenario``: 'zero' (launch promo) | 'projected'."""
    if scenario == "zero":
        return 0.0
    if scenario != "projected":
        raise ValueError(f"unknown fee scenario {scenario!r}")
    maker, taker = load_fee_rates(ticker)
    rate = maker if role == "maker" else taker
    return abs(notional) * rate


# ── funding ─────────────────────────────────────────────────────────────────

def funding_payment(contracts: float, contract_price: float, funding_rate: float) -> float:
    """P&L of the holder at one funding mark.

    ``contracts`` signed (+long/−short). Positive rate ⇒ longs pay:
        long  (+c) → −rate × notional   (pays)
        short (−c) → +rate × notional   (receives)
    """
    return -funding_rate * contracts * contract_price


# ── depth-aware slippage ────────────────────────────────────────────────────

@dataclass(frozen=True)
class WalkResult:
    filled: float            # contracts filled (≤ requested)
    avg_price: float | None  # volume-weighted fill price
    worst_price: float | None
    levels_used: int
    exhausted: bool          # book ran out before the full size filled


def walk_book(levels: list[tuple[float, float]] | list[list], qty: float, *,
              side: str) -> WalkResult:
    """Walk ``qty`` contracts through raw [price, size] levels.

    ``side='buy'`` walks asks (ascending price); ``side='sell'`` walks bids
    (descending). Levels may arrive unsorted (Kalshi returns best-last) —
    sorted here.
    """
    if qty <= 0:
        return WalkResult(0.0, None, None, 0, False)
    lv = [(float(p), float(s)) for p, s in levels if float(s) > 0]
    lv.sort(key=lambda x: x[0], reverse=(side == "sell"))
    remaining = qty
    cost = 0.0
    worst = None
    used = 0
    for px, sz in lv:
        take = min(remaining, sz)
        cost += take * px
        worst = px
        used += 1
        remaining -= take
        if remaining <= 1e-12:
            break
    filled = qty - max(remaining, 0.0)
    return WalkResult(filled, (cost / filled) if filled > 0 else None, worst, used,
                      exhausted=remaining > 1e-12)


def slippage_dollars(levels, qty: float, *, side: str, mid: float) -> dict:
    """Slippage of filling qty vs mid: dollars, bps of notional, fill ratio."""
    w = walk_book(levels, qty, side=side)
    if w.filled <= 0 or w.avg_price is None or mid <= 0:
        return {"dollars": None, "bps": None, "filled": 0.0, "exhausted": True}
    per_contract = (w.avg_price - mid) if side == "buy" else (mid - w.avg_price)
    dollars = per_contract * w.filled
    notional = mid * w.filled
    return {"dollars": dollars, "bps": 1e4 * dollars / notional,
            "filled": w.filled, "exhausted": w.exhausted}


def round_trip_cost_dollars(levels_buy, levels_sell, qty: float, *, mid: float,
                            scenario: str = "projected", role: str = "taker",
                            ticker: str = "KXBTCPERP") -> float | None:
    """Full round-trip cost estimate (entry+exit fees + both slippage legs).

    The Plan 01 refusal gate: refuse signals whose edge < this at min size.
    """
    buy = slippage_dollars(levels_buy, qty, side="buy", mid=mid)
    sell = slippage_dollars(levels_sell, qty, side="sell", mid=mid)
    if buy["dollars"] is None or sell["dollars"] is None:
        return None
    notional = mid * qty
    fees = 2 * fee_dollars(notional, role=role, scenario=scenario, ticker=ticker)
    return buy["dollars"] + sell["dollars"] + fees


def decimal_book_to_levels(book: dict) -> tuple[list, list]:
    """Kalshi margin orderbook payload → (bids, asks) float levels."""
    ob = book.get("orderbook", book)
    bids = [(float(p), float(s)) for p, s in (ob.get("bids") or [])]
    asks = [(float(p), float(s)) for p, s in (ob.get("asks") or [])]
    return bids, asks


def mirror_to_levels(bids: dict[Decimal, Decimal], asks: dict[Decimal, Decimal]) -> tuple[list, list]:
    """BookMirror maps → float level lists."""
    return ([(float(p), float(s)) for p, s in bids.items()],
            [(float(p), float(s)) for p, s in asks.items()])
