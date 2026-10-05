"""W13: the W7 PROD router with its one venue call replaced by a simulated RESTING order (maker): the order is
posted at touch - 1c and chased by the observer (see observer._chase); here `_send` only records the resting
price and what a taker (W11) would have got from the same book, for the apples-to-apples ledger.

Inherited from W11's router: the simulated IOC fill against the
book the decision was priced from. Every other rule (prod band, day/night sizes, the
NO-after-dump cut, the +1c limit, the table decision at T-8, the window cap and the
top1/top2 slot ledger, the day flow gate) is the production code itself, unchanged.
The process must point live_plan.SLOTS_FILE at W13's own file before the first submit."""
from __future__ import annotations
import json, time, uuid
from crypto_trading.crypto_common.execution_events import EventExecutionRouter
from crypto_trading.crypto_strategies.live_watch.w7_noisefade import LADDER_FOR_SIDE


def walk_with_limit(ob: dict, side: str, contracts: int, limit: float) -> tuple[float, float]:
    """Simulated IOC: take `contracts` of `side` from the opposite bids at a cost <= limit.
    Returns (filled, average cost per contract on OUR side)."""
    lad = []
    for p_, sz in (ob.get(LADDER_FOR_SIDE[side]) or []):
        try: p_, sz = float(p_), float(sz)
        except (TypeError, ValueError): continue
        if 0 < p_ < 1 and sz > 0 and (1 - p_) <= limit + 1e-9: lad.append((1 - p_, sz))
    lad.sort()
    got = paid = 0.0
    for c_, sz in lad:
        t = min(contracts - got, sz); got += t; paid += t * c_
        if got >= contracts - 1e-9: break
    return (got, round(paid / got, 6)) if got > 0 else (0.0, 0.0)


class PaperProdRouter(EventExecutionRouter):
    """Same rules as production; the send is a book walk. `book` must be set (raw orderbook_fp)
    before each submit by the caller that fetched it."""
    book: dict | None = None

    def gate_status(self) -> dict:                      # paper: the gate is always open
        return {"live_open": True, "paper": True}

    def _journal_user_skips(self, skipped: dict) -> None:
        return None

    def _spawn_user_mirror(self, intent: dict) -> None:
        return None

    def _send(self, ticker: str, side: str, contracts: int, price_dollars: float) -> dict:
        ob = self.book or {}
        got, avg_cost = walk_with_limit(ob, side, int(contracts), float(price_dollars))
        fee = 0.07 * avg_cost * (1 - avg_cost) if got > 0 else 0.0
        yes_px = avg_cost if side == "yes" else (1 - avg_cost if got > 0 else 0.0)   # venue reports the YES-axis price
        resp = {"order_id": f"paper-{uuid.uuid4().hex[:12]}", "fill_count": f"{got:.2f}", "average_fill_price": f"{yes_px:.4f}",
                "average_fee_paid": f"{fee:.4f}", "status": "executed" if got > 0 else "canceled", "paper": True}
        return {"status_code": 201, "response": json.dumps(resp), "paper": True,
                "body_sent": {"ticker": ticker, "side": "bid" if side == "yes" else "ask", "count": int(contracts), "price_dollars": float(price_dollars),
                              "tif": "immediate_or_cancel", "client_order_id": resp["order_id"]},
                "paper_fill": {"filled": got, "cost": avg_cost, "fee": fee, "levels": len(ob.get(LADDER_FOR_SIDE[side]) or [])}}


def top_cost(ob: dict, side: str) -> float | None:
    """Best available cost for `side` (the touch a taker would pay)."""
    got, c = walk_with_limit(ob, side, 1, 0.99)
    return c if got > 0 else None


class MakerPaperRouter(PaperProdRouter):
    """Same production rules; the send places a simulated resting order at touch - IMPROVE instead of taking.
    The response carries fill_count 0 (nothing is bought at send time) plus the resting price and the taker
    counterfactual; the observer chases and fills it from the recorded book stream."""
    improve_c: float = 0.01

    def _send(self, ticker: str, side: str, contracts: int, price_dollars: float) -> dict:
        import json as _json, uuid as _uuid
        ob = self.book or {}
        got_t, cost_t = walk_with_limit(ob, side, int(contracts), float(price_dollars))
        touch = top_cost(ob, side)
        price = None if touch is None else round(min(max(touch - self.improve_c, 0.01), 0.99), 2)
        resp = {"order_id": f"maker-{_uuid.uuid4().hex[:12]}", "fill_count": "0.00", "average_fill_price": "0.0000", "average_fee_paid": "0.0000",
                "status": "resting" if price is not None else "canceled", "paper": True}
        return {"status_code": 201, "response": _json.dumps(resp), "paper": True,
                "body_sent": {"ticker": ticker, "side": "bid" if side == "yes" else "ask", "count": int(contracts), "price_dollars": float(price_dollars),
                              "tif": "resting_until_close-45s", "client_order_id": resp["order_id"]},
                "resting": {"price": price, "touch": touch, "contracts": int(contracts), "taker_filled": got_t, "taker_cost": cost_t,
                            "taker_fee": round(0.07 * cost_t * (1 - cost_t), 6) if got_t > 0 else 0.0, "levels": len(ob.get(LADDER_FOR_SIDE[side]) or [])}}
