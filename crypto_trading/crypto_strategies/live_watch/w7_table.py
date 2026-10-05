"""W7 table-driven prod entries at the non-T-8 bins (user directive 2026-10-02).

Runs every runner tick (10 s). When the current 15M window is at T-9.75,
T-6.75 or T-5.25 (entries are allowed only between T-9.75 and T-5.25), it reads the live book for each coin,
takes the favourite, checks the MAIN band [0.78, 0.98], classifies the scenario
right now and asks the latest table (w7_scenarios.live_plan) whether this bin is
the coin/scenario's top1 or top2. If so it sends through the same prod submit
W7 uses, so every live rule (flow gate, day/night size, NO-after-dump, +1c) still
applies; the table only adds the bin choice and the x1.5/x1/x0.5 multiplier.
The T-8.25 bin is decided inside submit on W7's own T-8 call.

Prod only (no demo mirror). Never touches W7's paper book or its state file.
Each (ticker, bin) is attempted at most once, persisted across restarts.
"""
from __future__ import annotations

import json
import logging
import os
import time

from crypto_trading.crypto_common.config import SIGNALS_DIR
from crypto_trading.crypto_common.execution_events import EventExecutionRouter
from crypto_trading.crypto_strategies.w7_scenarios import inputs, live_plan
from . import common, w7_noisefade as w7

logger = logging.getLogger(__name__)
NAME = "w7_table"
STATE = SIGNALS_DIR / "live_watch" / "w7_table_state.json"
CONTRACTS_PAPER = 25          # the walk size W7 prices its favourite with (same as the tables)


def _load() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {"done": {}}


def _save(st: dict) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st))
    os.replace(tmp, STATE)


def run(cfg: dict | None = None, **_) -> dict:
    cfg = cfg or common.load_cfg()
    if not (EventExecutionRouter.TABLE_LIVE.get(w7.NAME) or {}).get("enabled"):
        return {"strategy": NAME, "status": "OFF"}
    now = time.time()
    live_plan.maybe_refresh_funding(now)
    close = int((now // 900 + 1) * 900)
    rem = (close - now) / 60.0
    b = live_plan.bin_due(rem)
    if b is None or b == live_plan.LIVE_BIN:
        return {"strategy": NAME, "status": "IDLE", "rem_min": round(rem, 2)}
    st = _load()
    done = st.setdefault("done", {})
    armed = bool(cfg.get(w7.NAME, {}).get("live_orders", False))
    report = {"strategy": NAME, "status": "SCANNED", "bin": b, "rem_min": round(rem, 2), "coins": {}}
    for series in w7.MARKETS:
        coin = series[2:].replace("15M", "")
        ticker = inputs.ticker(coin, close)
        key = f"{ticker}|{b}"
        if key in done:
            continue
        try:
            bk = w7.walk_book_both(ticker, CONTRACTS_PAPER)
            if bk is None:
                # not an attempt: the next tick (<= 10 s later, still inside the
                # bin's -36 s tolerance) retries the book read
                report["coins"][coin] = "book_unavailable"
                continue
            done[key] = now                     # one decision per ticker/bin, even if it fails below
            _save(st)
            side = w7.favorite_side((bk["yes_bid"] + bk["yes_ask"]) / 2.0)
            if side is None:
                report["coins"][coin] = "no_favourite"
                continue
            cost = bk[side].get("fill_cost") or bk[side].get("top_cost")
            lo, hi = EventExecutionRouter.PROD_BAND.get(w7.NAME, (w7.MAIN_LO, w7.MAIN_HI))
            if cost is None or not (lo <= cost <= hi):
                report["coins"][coin] = f"out_of_band {side}@{cost}"
                continue
            d = live_plan.decide(coin, close, b, side, now, ticker=ticker)
            entry = {"action": "table_decision", "ticker": ticker, "side": side, "cost": round(cost, 4),
                     "bin": b, "rem_min": round(rem, 2), "decision": d["action"], "reason": d.get("reason"),
                     "scenario": d.get("scenario"), "slot": d.get("slot"), "mult": d.get("mult"),
                     "slots": d.get("slots"), "table_run": d.get("table_run"),
                     # the classification inputs (trend/vol/funding/flow) so every decision can
                     # be replayed from the recordings (user 2026-10-03); None when unclassified
                     "inputs": d.get("inputs"), "missing": d.get("missing")}
            if d["action"] == "trade":
                entry["live"] = common.mirror_async(
                    w7.NAME, EventExecutionRouter(strategy=w7.NAME).submit,
                    _log_action="live_order_result", ticker=ticker, side=side, entry_price=cost,
                    contracts=CONTRACTS_PAPER, armed=armed, size_mult=d["mult"],
                    entry_source=f"table_{d['slot']}_{b}", table_decision=d)
            common.log_line(NAME, entry)
            report["coins"][coin] = f"{d['action']} {d.get('scenario')} {d.get('slot') or ''} x{d.get('mult') or ''}"
        except Exception as e:                                        # noqa: BLE001
            logger.warning("[%s] %s failed: %s", NAME, ticker, str(e)[:160])
            report["coins"][coin] = f"error {type(e).__name__}"
    st["done"] = {k: v for k, v in done.items() if now - v < 3 * 3600}
    _save(st)
    return report
