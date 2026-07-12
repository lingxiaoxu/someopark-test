"""Event-driven intraday simulator (Plan 00 §6 `backtest/intraday_sim.py`).

For tick/book strategies (Plans 01, 04) and multi-leg (Plan 02 later). NOT a
bar backtester: it replays a TAPE of events and the strategy reacts.

Tape events (dicts, chronological):
    {"type": "book",    "ts": epoch_s, "bids": [[px,sz]…], "asks": [[px,sz]…]}
    {"type": "trade",   "ts": …, "price": …, "count": …, "taker_side": …}   (optional)
    {"type": "index",   "ts": …, "index": …}                                (optional)
    {"type": "funding", "ts": …, "rate": …}      (settlements, PIT-correct)

Fill model (honest about its assumptions, Plan 00: "walk historical depth,
never assume mid-fill"):
  * IOC/aggressive orders fill IMMEDIATELY by walking the CURRENT book's
    opposite side (costs.walk_book); partial fills happen when depth runs out.
  * post_only resting orders fill when a later book shows the opposite best
    CROSSING the limit price (conservative: fills at the LIMIT price, size
    capped by the crossing level's size).
  * Fees per fill via costs.fee_dollars (scenario zero|projected); funding via
    costs.funding_payment at each funding event.

Outputs: fills, equity curve (per event ts), daily returns (365-day world),
end stats. The WF driver (crypto_common.walk_forward) consumes ``run()``'s
returns series.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd

from crypto_trading.crypto_common.costs import fee_dollars, funding_payment, walk_book

logger = logging.getLogger(__name__)


@dataclass
class SimConfig:
    fee_scenario: str = "projected"        # zero | projected
    ticker: str = "KXBTCPERP"
    initial_cash: float = 1000.0
    mark_to: str = "mid"                   # equity marking
    force_flat_at_end: bool = True


@dataclass
class SimOrder:
    side: str                              # buy | sell
    qty: float
    order_type: str = "ioc"                # ioc | post_only
    limit_price: float | None = None       # required for post_only
    tag: str = ""


@dataclass
class Fill:
    ts: float
    side: str
    qty: float
    price: float
    fee: float
    role: str                              # taker | maker
    tag: str = ""


@dataclass
class SimState:
    cash: float = 0.0
    position: float = 0.0                  # signed contracts
    avg_price: float = 0.0
    fees_paid: float = 0.0
    funding_pnl: float = 0.0
    realized_pnl: float = 0.0
    fills: list[Fill] = field(default_factory=list)
    resting: list[SimOrder] = field(default_factory=list)

    def mark_equity(self, mid: float | None) -> float | None:
        if mid is None:
            return None
        return self.cash + self.position * mid


class IntradaySim:
    """Replay a tape against a strategy callback.

    ``strategy(event, ctx) -> list[SimOrder]`` where ctx exposes the live
    book, last index value, position, and mid. The strategy sees events in
    order and never the future (PIT by construction).
    """

    def __init__(self, cfg: SimConfig):
        self.cfg = cfg
        self.state = SimState(cash=cfg.initial_cash)
        self.bids: list = []
        self.asks: list = []
        self.last_index: float | None = None
        self.equity_points: list[tuple[float, float]] = []

    # ── views ──────────────────────────────────────────────────────────────
    @property
    def best_bid(self) -> float | None:
        return max((p for p, s in self.bids if s > 0), default=None)

    @property
    def best_ask(self) -> float | None:
        return min((p for p, s in self.asks if s > 0), default=None)

    @property
    def mid(self) -> float | None:
        bb, ba = self.best_bid, self.best_ask
        return (bb + ba) / 2 if bb is not None and ba is not None else None

    # ── fills ──────────────────────────────────────────────────────────────
    def _apply_fill(self, ts: float, side: str, qty: float, price: float,
                    role: str, tag: str) -> None:
        s = self.state
        fee = fee_dollars(qty * price, role=role, scenario=self.cfg.fee_scenario,
                          ticker=self.cfg.ticker)
        signed = qty if side == "buy" else -qty
        # realized P&L on the reducing portion
        if s.position * signed < 0:
            reduce_qty = min(abs(signed), abs(s.position))
            pnl_per = (price - s.avg_price) if s.position > 0 else (s.avg_price - price)
            s.realized_pnl += pnl_per * reduce_qty
        new_pos = s.position + signed
        if new_pos != 0 and s.position * signed >= 0:          # adding/opening
            s.avg_price = ((abs(s.position) * s.avg_price + qty * price)
                           / (abs(s.position) + qty))
        elif new_pos != 0 and abs(signed) > abs(s.position):   # flipped through flat
            s.avg_price = price
        elif new_pos == 0:
            s.avg_price = 0.0
        s.position = new_pos
        s.cash -= signed * price
        s.cash -= fee
        s.fees_paid += fee
        s.fills.append(Fill(ts, side, qty, price, fee, role, tag))

    def _exec_ioc(self, ts: float, order: SimOrder) -> None:
        levels = self.asks if order.side == "buy" else self.bids
        w = walk_book(levels, order.qty, side=order.side)
        if w.filled > 0 and w.avg_price is not None:
            self._apply_fill(ts, order.side, w.filled, w.avg_price, "taker", order.tag)
        if w.exhausted:
            logger.debug("IOC partial: %s %.0f/%.0f", order.side, w.filled, order.qty)

    def _match_resting(self, ts: float) -> None:
        still: list[SimOrder] = []
        for o in self.state.resting:
            opp = self.best_ask if o.side == "buy" else self.best_bid
            crossed = (opp is not None and o.limit_price is not None and
                       (opp <= o.limit_price if o.side == "buy" else opp >= o.limit_price))
            if crossed:
                # conservative: fill at OUR limit, capped by the crossing level size
                levels = self.asks if o.side == "buy" else self.bids
                cross_sz = sum(s for p, s in levels
                               if (p <= o.limit_price if o.side == "buy" else p >= o.limit_price))
                qty = min(o.qty, cross_sz) if cross_sz > 0 else 0.0
                if qty > 0:
                    self._apply_fill(ts, o.side, qty, o.limit_price, "maker", o.tag)
                    if qty < o.qty:
                        o.qty -= qty
                        still.append(o)
                    continue
            still.append(o)
        self.state.resting = still

    # ── main loop ──────────────────────────────────────────────────────────
    def run(self, tape, strategy) -> "SimResult":
        for ev in tape:
            ts = float(ev["ts"])
            et = ev.get("type")
            if et == "book":
                self.bids = [(float(p), float(s)) for p, s in ev.get("bids") or []]
                self.asks = [(float(p), float(s)) for p, s in ev.get("asks") or []]
                self._match_resting(ts)
            elif et == "index":
                self.last_index = float(ev["index"])
            elif et == "funding":
                m = self.mid
                if m is not None and self.state.position != 0:
                    pay = funding_payment(self.state.position, m, float(ev["rate"]))
                    self.state.cash += pay
                    self.state.funding_pnl += pay
            orders = strategy(ev, self) or []
            for o in orders:
                if o.order_type == "ioc":
                    self._exec_ioc(ts, o)
                elif o.order_type == "post_only":
                    if o.limit_price is None:
                        raise ValueError("post_only requires limit_price")
                    self.state.resting.append(o)
                else:
                    raise ValueError(f"unknown order_type {o.order_type!r}")
            eq = self.state.mark_equity(self.mid)
            if eq is not None:
                self.equity_points.append((ts, eq))

        if self.cfg.force_flat_at_end and self.state.position != 0:
            m = self.mid
            if m is not None:
                side = "sell" if self.state.position > 0 else "buy"
                self._exec_ioc(self.equity_points[-1][0] if self.equity_points else 0.0,
                               SimOrder(side, abs(self.state.position), tag="force_flat"))
        return SimResult(self)


class SimResult:
    def __init__(self, sim: IntradaySim):
        self.cfg = sim.cfg
        self.state = sim.state
        eq = pd.Series(dict(sim.equity_points), dtype=float)
        eq.index = pd.to_datetime(eq.index, unit="s", utc=True)
        self.equity = eq[~eq.index.duplicated(keep="last")].sort_index()

    @property
    def daily_returns(self) -> pd.Series:
        if self.equity.empty:
            return pd.Series(dtype=float)
        daily = self.equity.resample("1D").last().dropna()
        return daily.pct_change().dropna()

    def summary(self) -> dict:
        s = self.state
        final = float(self.equity.iloc[-1]) if len(self.equity) else s.cash
        return {
            "final_equity": final,
            "net_pnl": final - self.cfg.initial_cash,
            "realized_pnl": s.realized_pnl,
            "fees_paid": s.fees_paid,
            "funding_pnl": s.funding_pnl,
            "n_fills": len(s.fills),
            "maker_share": (sum(1 for f in s.fills if f.role == "maker")
                            / len(s.fills)) if s.fills else 0.0,
            "end_position": s.position,
        }
