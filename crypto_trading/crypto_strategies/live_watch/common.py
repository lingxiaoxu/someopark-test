"""Shared plumbing for the live_watch strategy modules.

Design contract:
  * every module returns a plain dict "report" and calls ``emit()`` for each
    order it WOULD place;
  * ``emit()`` always logs; it submits only when the strategy is enabled in
    config.yaml AND the global execution gates pass — so a disarmed run is a
    complete rehearsal with zero live risk;
  * per-strategy state (open position, realized P&L, kill status) persists in
    SIGNALS_DIR/live_watch/<name>_state.json and survives restarts;
  * the kill switch is evaluated on REALIZED fills only and, once tripped,
    stays tripped until a human deletes the flag from the state file.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import yaml

from crypto_trading.crypto_common import execution
from crypto_trading.crypto_common.config import SIGNALS_DIR

logger = logging.getLogger(__name__)

CFG_PATH = Path(__file__).parent / "config.yaml"
STATE_DIR = SIGNALS_DIR / "live_watch"


def load_cfg() -> dict:
    return yaml.safe_load(CFG_PATH.read_text())


def state_path(name: str) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / f"{name}_state.json"


def load_state(name: str) -> dict:
    p = state_path(name)
    if p.exists():
        return json.loads(p.read_text())
    return {"position": None, "trades": [], "cum_net_usd": 0.0, "killed": False}


def save_state(name: str, st: dict) -> None:
    state_path(name).write_text(json.dumps(st, indent=1, default=str))


def log_line(name: str, payload: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with open(STATE_DIR / f"log_{day}.jsonl", "a") as fh:
        fh.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                             "strategy": name, **payload}, default=str) + "\n")


def kill_check(name: str, st: dict, cfg: dict) -> bool:
    """Realized-P&L kill switch. Returns True if the strategy is (now) killed."""
    if st.get("killed"):
        return True
    n = len(st.get("trades", []))
    if (n >= cfg.get("min_trades_for_kill", 10 ** 9)
            and st.get("cum_net_usd", 0.0) < -abs(cfg.get("max_cum_loss_usd", 1e9))):
        st["killed"] = True
        st["killed_reason"] = (f"cum_net {st['cum_net_usd']:.2f} < "
                               f"-{cfg['max_cum_loss_usd']} after {n} trades")
        logger.error("[%s] KILL SWITCH TRIPPED: %s", name, st["killed_reason"])
        save_state(name, st)
        return True
    return False


def emit(name: str, order: "execution.Order", *, enabled: bool,
         reason: str) -> dict:
    """Log the intended order; submit only if armed at BOTH levels.

    Level 1 = strategy ``enabled`` in config.yaml. Level 2 = the router's own
    hard gates (prod env + ALLOW_LIVE_ORDERS=1 + margin + dedicated key) —
    ``ExecutionRouter.submit(live=True)`` raises unless ALL pass, and we only
    ask for live when level 1 is on. A disarmed run is a full rehearsal.
    """
    router = execution.ExecutionRouter(strategy=name)
    gates = router.gate_status()
    all_open = all(bool(v) for v in gates.values())
    rec = {"action": "order", "reason": reason,
           "enabled": bool(enabled), "gates": gates,
           "order": {"ticker": order.ticker, "side": order.side,
                     "count": order.count, "price": order.price,
                     "post_only": order.post_only, "tif": order.tif,
                     "subaccount": order.subaccount}}
    if enabled and all_open:
        try:
            resp = router.submit(order, live=True)
            rec["submitted"] = True
            rec["response"] = str(resp)[:200]
        except execution.LiveOrderRefused as e:
            rec["submitted"] = False
            rec["error"] = f"refused: {e}"[:200]
        except Exception as e:                              # noqa: BLE001
            rec["submitted"] = False
            rec["error"] = str(e)[:200]
            logger.error("[%s] submit failed: %s", name, e)
    else:
        rec["submitted"] = False
        rec["note"] = "DRY-RUN (strategy disabled or global gates closed)"
    log_line(name, rec)
    return rec


def latest_touch(ticker: str) -> tuple[float | None, float | None]:
    """Freshest bid/ask from today's poll tape (None if stale > 120s)."""
    import time

    import pandas as pd

    from crypto_trading.crypto_common.loader import load_poll_market_stats
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    st = load_poll_market_stats(ticker, days=[day])
    if st.empty:
        return None, None
    last = st.dropna(subset=["bid", "ask"]).iloc[-1:]
    if last.empty:
        return None, None
    age = time.time() - last.index[0].timestamp()
    if age > 120:
        logger.warning("%s touch stale %.0fs", ticker, age)
        return None, None
    return float(last.bid.iloc[0]), float(last.ask.iloc[0])


def verify_maker_fill(ticker: str, side: str, limit: float,
                      post_ts, timeout_min: float,
                      queue_frac: float = 1.0) -> dict:
    """Forward fill-verification for a virtual maker order (the maker analog of
    W5's capture probe). Replays the REAL trades tape recorded since post_ts
    through the same queue model the backtests used:
      returns {"status": "filled"|"unfilled"|"pending", "fill_ts": ...}
    "pending" = timeout not reached yet and no fill so far — check again later.
    Without this, a paper position opened at emit time silently assumes a 100%
    maker fill rate, which this project has repeatedly measured to be false.
    """
    import pandas as pd

    from crypto_trading.crypto_common.backtest.fill_model import simulate_maker_fill
    from crypto_trading.crypto_common.loader import load_poll_trades

    post_ts = pd.Timestamp(post_ts)
    now = pd.Timestamp.now(tz="UTC")
    days = sorted({post_ts.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")})
    try:
        trades = load_poll_trades(ticker, days=days).sort_index()
    except Exception:                                       # noqa: BLE001
        return {"status": "pending", "note": "no tape"}
    w = trades.loc[post_ts - pd.Timedelta(minutes=5):post_ts, "count"]
    queue = queue_frac * (float(w.median()) if len(w) else 1.0)
    fr = simulate_maker_fill(limit, side, post_ts, trades,
                             timeout=pd.Timedelta(minutes=timeout_min),
                             queue_ahead=queue)
    if fr.filled:
        return {"status": "filled", "fill_ts": str(fr.fill_ts)}
    deadline = post_ts + pd.Timedelta(minutes=timeout_min)
    return {"status": "unfilled" if now > deadline + pd.Timedelta(minutes=2)
            else "pending"}
