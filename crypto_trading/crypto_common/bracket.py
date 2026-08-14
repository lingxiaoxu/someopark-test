"""Take-profit / stop-loss brackets for perp positions (client-side triggers).

IMPORTANT — verified against docs.kalshi.com (2026-07-12): the Kalshi MARGIN
API has **no native stop / trigger / OCO / TP-SL order type** (order-groups are
a rolling-window contract-limit risk tool, not conditional orders). So TP/SL is
implemented the same way the Kalshi app does it: a CLIENT-SIDE watcher holds the
target prices, and when the mark crosses a level it submits a marketable
reduce_only close order.

Consequence (be honest about it): a client-side bracket only fires while the
watcher process is alive. It is NOT an exchange-resting stop that survives your
process dying. For an always-on backstop you must either (a) set TP/SL in the
Kalshi app itself, or (b) run this watcher under a supervised always-on daemon.
This module gives the strategy loop programmatic TP/SL; it does not change the
fact that the exchange has no native primitive.

Price basis: brackets are in CONTRACT price (what an order uses, e.g. 6.4015 for
KXBTCPERP). ``underlying_to_contract`` converts a BTC-dollar level (like the app
shows, $60,595) using the market ``contract_size`` (0.0001).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from crypto_trading.crypto_common.execution import Order

logger = logging.getLogger(__name__)


def underlying_to_contract(underlying_price: float, contract_size: float) -> float:
    """BTC-dollar level ($60,595) → contract price (6.0595) via contract_size."""
    return underlying_price * contract_size


def contract_to_underlying(contract_price: float, contract_size: float) -> float:
    return contract_price / contract_size


@dataclass(frozen=True)
class Bracket:
    """A take-profit / stop-loss pair attached to one open position.

    ``side`` is the POSITION side: "bid" = long, "ask" = short (Kalshi enum).
    Prices are CONTRACT prices. Either level may be None (one-sided bracket).
    Validity: for a long, take_profit > entry > stop_loss; for a short the
    inequality flips. Constructing an inverted bracket raises.
    """
    ticker: str
    side: str                       # "bid" (long) | "ask" (short)
    contracts: float
    take_profit: float | None = None
    stop_loss: float | None = None
    entry_price: float | None = None
    subaccount: int = 0             # MUST match where the position lives (real acct = 64)
                                    # else the close hits an empty subaccount and no-ops

    def __post_init__(self):
        if self.side not in ("bid", "ask"):
            raise ValueError(f"side must be 'bid'(long)/'ask'(short), got {self.side!r}")
        if self.contracts <= 0:
            raise ValueError("contracts must be > 0")
        if self.take_profit is None and self.stop_loss is None:
            raise ValueError("bracket needs at least a take_profit or a stop_loss")
        long = self.side == "bid"
        # sanity vs entry, if given: TP is the favorable side, SL the adverse side
        if self.entry_price is not None:
            if self.take_profit is not None:
                if long and self.take_profit <= self.entry_price:
                    raise ValueError("long take_profit must be ABOVE entry")
                if not long and self.take_profit >= self.entry_price:
                    raise ValueError("short take_profit must be BELOW entry")
            if self.stop_loss is not None:
                if long and self.stop_loss >= self.entry_price:
                    raise ValueError("long stop_loss must be BELOW entry")
                if not long and self.stop_loss <= self.entry_price:
                    raise ValueError("short stop_loss must be ABOVE entry")
        # even without an entry, TP and SL must be on the correct sides of each
        # other (long: TP > SL; short: TP < SL) — else triggered() fires wrongly
        if self.take_profit is not None and self.stop_loss is not None:
            if long and self.take_profit <= self.stop_loss:
                raise ValueError("long bracket needs take_profit > stop_loss")
            if not long and self.take_profit >= self.stop_loss:
                raise ValueError("short bracket needs take_profit < stop_loss")

    def triggered(self, mark: float) -> str | None:
        """Return 'take_profit' | 'stop_loss' | None for the current mark."""
        long = self.side == "bid"
        tp, sl = self.take_profit, self.stop_loss
        if long:
            if tp is not None and mark >= tp:
                return "take_profit"
            if sl is not None and mark <= sl:
                return "stop_loss"
        else:  # short profits as price falls
            if tp is not None and mark <= tp:
                return "take_profit"
            if sl is not None and mark >= sl:
                return "stop_loss"
        return None

    def close_order(self, fill_price: float, *, subaccount: int | None = None,
                    client_order_id: str | None = None) -> Order:
        """Marketable reduce_only IOC order that flattens the position.

        Opposite side of the position; IOC + reduce_only so it only ever
        REDUCES (official: reduce_only requires IOC/FOK). ``fill_price`` should
        be a marketable price (cross the book) so the close actually fills.
        Defaults to the bracket's OWN subaccount (where the position lives) —
        closing to the wrong subaccount silently no-ops.
        """
        close_side = "ask" if self.side == "bid" else "bid"
        kw = {"tif": "immediate_or_cancel", "reduce_only": True,
              "subaccount": self.subaccount if subaccount is None else subaccount}
        if client_order_id is not None:
            kw["client_order_id"] = client_order_id
        return Order(self.ticker, close_side, self.contracts, fill_price, **kw)


def _bracket_to_dict(b: Bracket) -> dict:
    return {"ticker": b.ticker, "side": b.side, "contracts": b.contracts,
            "take_profit": b.take_profit, "stop_loss": b.stop_loss,
            "entry_price": b.entry_price, "subaccount": b.subaccount}


def _bracket_from_dict(d: dict) -> Bracket:
    return Bracket(d["ticker"], d["side"], d["contracts"],
                   take_profit=d.get("take_profit"), stop_loss=d.get("stop_loss"),
                   entry_price=d.get("entry_price"), subaccount=int(d.get("subaccount", 0)))


class BracketMonitor:
    """Holds active brackets and fires closes when triggered.

    The strategy/watcher loop calls ``on_mark(ticker, mark, fill_price)`` on
    each price update. On a trigger it submits the reduce_only close through the
    given ExecutionRouter (which enforces the demo-first live gate — dry-run
    until the operator opens it) and drops the bracket.

    Brackets can be PERSISTED to a JSON state file (``state_path``) so an armed
    TP/SL survives a process restart — critical because a client-side bracket
    only protects while a watcher is alive; persistence lets the watcher daemon
    re-arm on restart instead of silently forgetting an open position's stops.
    """

    def __init__(self, router, *, live: bool = False, state_path=None):
        self.router = router
        self.live = live
        self.state_path = state_path
        self._brackets: dict[str, Bracket] = {}   # one active bracket per ticker
        self.events: list[dict] = []
        if state_path is not None:
            self.load()

    def arm(self, bracket: Bracket) -> None:
        self._brackets[bracket.ticker] = bracket
        self._persist()

    def disarm(self, ticker: str) -> None:
        self._brackets.pop(ticker, None)
        self._persist()

    def active(self) -> dict[str, Bracket]:
        return dict(self._brackets)

    def _persist(self) -> None:
        if self.state_path is None:
            return
        from pathlib import Path
        p = Path(self.state_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(
            {t: _bracket_to_dict(b) for t, b in self._brackets.items()}, indent=1))

    def load(self) -> None:
        from pathlib import Path
        p = Path(self.state_path)
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text())
            self._brackets = {t: _bracket_from_dict(d) for t, d in data.items()}
        except Exception:
            # A corrupt state file is the WORST failure for a safety feature —
            # silently loading zero brackets leaves open positions unprotected.
            # Loud error + quarantine the bad file so the operator sees it.
            logger.error("CORRUPT bracket state %s — open positions may be "
                         "UNPROTECTED. Quarantining to .corrupt", self.state_path,
                         exc_info=True)
            try:
                Path(str(self.state_path) + ".corrupt").write_text(p.read_text())
            except Exception:
                pass

    def on_mark(self, ticker: str, mark: float, fill_price: float,
                *, subaccount: int | None = None) -> dict | None:
        """Check the ticker's bracket against the mark; close + report if fired.
        subaccount=None (default) uses the bracket's own subaccount — do NOT
        pass 0 here or you'll close to the wrong (empty) subaccount."""
        b = self._brackets.get(ticker)
        if b is None:
            return None
        kind = b.triggered(mark)
        if kind is None:
            return None
        order = b.close_order(fill_price, subaccount=subaccount)
        result = self.router.submit(order, live=self.live)
        status = result.get("status")
        # Only consume the bracket if the close actually went through (or was a
        # dry-run in paper mode). A "duplicate" means the close DIDN'T execute →
        # keep the bracket armed so the position isn't silently left unprotected.
        consumed = status != "duplicate"
        if consumed:
            self.disarm(ticker)
        event = {"ticker": ticker, "trigger": kind, "mark": mark,
                 "close_price": fill_price, "contracts": b.contracts,
                 "order_status": status, "bracket_consumed": consumed}
        self.events.append(event)
        return event
