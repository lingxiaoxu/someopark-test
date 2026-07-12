"""Local order-book mirror for Kalshi perps (Plan 00 §2 `book.py`).

Consumes WS ``orderbook_snapshot`` / ``orderbook_delta`` messages and maintains
price→size maps per side, plus per-market sequence tracking so gaps are
detected (a gap ⇒ the mirror is stale until the next snapshot; the recorder
logs it and the strategies must treat the book as unusable).

Wire format notes (probe + Kalshi docs):
  * snapshot msg: {"type":"orderbook_snapshot","sid":…,"seq":N,
                   "msg":{"market_ticker":…, …book payload…}}
  * delta msg:    {"type":"orderbook_delta","seq":N,
                   "msg":{"market_ticker":…, "price":…, "delta":…, "side":…}}
  * perp payloads carry decimal-dollar strings; sides may appear as yes/no
    (event heritage) or bid/ask-style arrays ("asks"/"bids" — margin REST).
    We normalise to "bids"/"asks".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

_SIDE_ALIASES = {
    "yes": "bids", "bid": "bids", "bids": "bids", "buy": "bids",
    "no": "asks", "ask": "asks", "asks": "asks", "sell": "asks",
}


@dataclass
class BookMirror:
    """One market's live book. Prices/sizes are Decimal (never float money)."""

    ticker: str
    bids: dict[Decimal, Decimal] = field(default_factory=dict)
    asks: dict[Decimal, Decimal] = field(default_factory=dict)
    last_seq: int | None = None
    synced: bool = False
    gaps: int = 0

    # ── message ingestion ──────────────────────────────────────────────────
    def apply_snapshot(self, msg: dict, seq: int | None = None) -> None:
        self.bids.clear()
        self.asks.clear()
        for raw_side, levels in (msg.get("orderbook") or msg).items() if isinstance(msg, dict) else []:
            side = _SIDE_ALIASES.get(str(raw_side).lower().removesuffix("_dollars"))
            if side is None or not isinstance(levels, (list, tuple)):
                continue
            book = self.bids if side == "bids" else self.asks
            for lvl in levels:
                try:
                    px, sz = Decimal(str(lvl[0])), Decimal(str(lvl[1]))
                except (IndexError, TypeError, ValueError, ArithmeticError):
                    continue
                if sz > 0:
                    book[px] = sz
        self.last_seq = seq
        self.synced = True

    def apply_delta(self, msg: dict, seq: int | None = None) -> bool:
        """Apply one delta. Returns False (and marks unsynced) on a seq gap."""
        if seq is not None and self.last_seq is not None and seq != self.last_seq + 1:
            self.gaps += 1
            self.synced = False
            return False
        side = _SIDE_ALIASES.get(str(msg.get("side", "")).lower())
        if side is None:
            return self.synced
        book = self.bids if side == "bids" else self.asks
        try:
            px = Decimal(str(msg["price"]))
            delta = Decimal(str(msg["delta"]))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            return self.synced
        new_sz = book.get(px, Decimal(0)) + delta
        if new_sz > 0:
            book[px] = new_sz
        else:
            book.pop(px, None)
        if seq is not None:
            self.last_seq = seq
        return True

    # ── views ──────────────────────────────────────────────────────────────
    def best_bid(self) -> tuple[Decimal, Decimal] | None:
        if not self.bids:
            return None
        px = max(self.bids)
        return px, self.bids[px]

    def best_ask(self) -> tuple[Decimal, Decimal] | None:
        if not self.asks:
            return None
        px = min(self.asks)
        return px, self.asks[px]

    def mid(self) -> Decimal | None:
        bb, ba = self.best_bid(), self.best_ask()
        if bb is None or ba is None:
            return None
        return (bb[0] + ba[0]) / 2

    def microprice(self) -> Decimal | None:
        """Depth-weighted top-of-book price (Plan 01 §6 micro-vs-mid option)."""
        bb, ba = self.best_bid(), self.best_ask()
        if bb is None or ba is None:
            return None
        (bpx, bsz), (apx, asz) = bb, ba
        tot = bsz + asz
        if tot == 0:
            return None
        return (bpx * asz + apx * bsz) / tot
