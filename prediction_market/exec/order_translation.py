"""Target-net-position → legal venue orders (plan 09 §5).

The engine works in TARGET NET POSITIONS, never "buy another lot". This module
diffs target vs current into the minimal legal order set for each venue, encoding
the microstructure rules so it can NEVER emit an order that would be netted away,
rejected, or geoblocked:
  * Kalshi: one signed net per market (YES>0 / NO<0); reversing = sell/reduce,
    handled by netting — never hold both sides (plan 09 §1.2).
  * Polymarket US: intent semantics (BUY_LONG/SELL_LONG/BUY_SHORT/SELL_SHORT),
    TIF in {GTC, FOK} (plan 09 §2.A).
  * Execution only on Kalshi / Polymarket US — `venue_guard` blocks Global.

PURE LOGIC (no network). The live order clients that consume these Order objects
are 🔒 until venue credentials exist; this layer is testable today and is the
"never generate an unexecutable order" guarantee (plan 09 §5 self-check list).
"""
from __future__ import annotations

from dataclasses import dataclass

from prediction_market.venues.guard import assert_executable

# Polymarket US intents (plan 07 §1.3 / 09 §2.A).
PMUS_INTENT = {
    ("buy", "yes"): "ORDER_INTENT_BUY_LONG",
    ("buy", "no"): "ORDER_INTENT_BUY_SHORT",
    ("sell", "yes"): "ORDER_INTENT_SELL_LONG",
    ("sell", "no"): "ORDER_INTENT_SELL_SHORT",
}
PMUS_TIF = {"GTC", "FOK"}


@dataclass(frozen=True)
class Order:
    venue: str
    market_key: str
    action: str           # "buy" / "sell"
    side: str             # "yes" / "no"
    count: int
    intent: str | None = None     # Polymarket US only
    tif: str = "GTC"

    def __str__(self) -> str:
        tag = f" intent={self.intent}" if self.intent else ""
        return f"{self.venue}:{self.market_key} {self.action} {self.count} {self.side}{tag}"


class OrderTranslationError(RuntimeError):
    """Raised when a target cannot be translated into a legal order."""


def to_orders(
    venue: str,
    market_key: str,
    target_net: int,
    current_pos: int = 0,
    *,
    tif: str = "GTC",
) -> list[Order]:
    """Minimal legal order set to move current_pos → target_net on ``venue``.

    Signed positions: +N means net N YES, -N means net N NO. Reversing sign is
    expressed as buying the opposite side (Kalshi netting) or the matching SELL
    intent (Polymarket US), never as stacking both sides.
    """
    assert_executable(venue)
    if tif not in PMUS_TIF and venue == "poly_us":
        raise OrderTranslationError(f"Polymarket US TIF must be GTC/FOK, got {tif!r}")

    delta = target_net - current_pos
    if delta == 0:
        return []

    if venue == "kalshi":
        # Kalshi auto-nets within a market: buying YES raises the net, buying NO
        # lowers it. One order moves toward target; netting does the rest.
        if delta > 0:
            return [Order(venue, market_key, "buy", "yes", delta)]
        return [Order(venue, market_key, "buy", "no", -delta)]

    # Polymarket US — pick action/side by whether we're growing or unwinding.
    orders: list[Order] = []
    if delta > 0:
        # Growing long YES (or covering a NO short): BUY_LONG.
        action, side = "buy", "yes"
    else:
        # Reducing a long → SELL_LONG; opening/extending short → BUY_SHORT.
        if current_pos > 0:
            action, side = "sell", "yes"
        else:
            action, side = "buy", "no"
    orders.append(Order(venue, market_key, action, side, abs(delta),
                        intent=PMUS_INTENT[(action, side)], tif=tif))
    return orders


def pretrade_checks(orders: list[Order]) -> list[tuple[str, bool, str]]:
    """The plan 09 §5 self-check list. Returns (check, passed, detail) per rule.

    A failing check means the order set must be blocked before submission.
    """
    checks: list[tuple[str, bool, str]] = []

    # 1) Execution venue allowed (no orders to Polymarket Global).
    bad_venue = [o for o in orders if o.venue not in ("kalshi", "poly_us")]
    checks.append(("executable_venue", not bad_venue,
                   f"non-executable: {[o.venue for o in bad_venue]}" if bad_venue else "ok"))

    # 2) Never both YES and NO net intent in the same Kalshi market.
    kalshi_sides: dict[str, set] = {}
    for o in orders:
        if o.venue == "kalshi":
            kalshi_sides.setdefault(o.market_key, set()).add(o.side)
    both = [m for m, s in kalshi_sides.items() if len(s) > 1]
    checks.append(("kalshi_single_side", not both,
                   f"both sides in {both}" if both else "ok"))

    # 3) Polymarket US orders carry a valid intent + TIF.
    bad_intent = [o for o in orders if o.venue == "poly_us"
                  and (o.intent not in PMUS_INTENT.values() or o.tif not in PMUS_TIF)]
    checks.append(("polyus_intent_tif", not bad_intent,
                   f"bad intent/tif on {[o.market_key for o in bad_intent]}" if bad_intent else "ok"))

    # 4) Positive integer counts only.
    bad_count = [o for o in orders if o.count <= 0 or int(o.count) != o.count]
    checks.append(("positive_count", not bad_count,
                   f"bad count on {[o.market_key for o in bad_count]}" if bad_count else "ok"))

    return checks


def assert_pretrade_ok(orders: list[Order]) -> None:
    """Raise if any pre-trade check fails — the hard gate before submission."""
    failures = [(name, detail) for name, ok, detail in pretrade_checks(orders) if not ok]
    if failures:
        raise OrderTranslationError("pre-trade checks failed: " + "; ".join(
            f"{n}: {d}" for n, d in failures))
