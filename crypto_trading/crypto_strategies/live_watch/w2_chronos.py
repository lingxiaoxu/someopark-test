"""W2 — S8 Chronos 4h, live signal (frozen 2026-08-10).

Frozen config: amazon/chronos-bolt-base, spot-composite input, price mode,
ctx512 (5-min grid), horizon 48 steps = 4h, no-trade band 40bps. One position
per market at a time (cooldown = horizon). Cadence: every 5-15 min.

Live mechanics mirror the fill-aware backtest: maker entry at the touch on the
forecast side; exit exactly 4h after entry — passive 2 min then adverse-touch
IOC (the exit the backtest paid for).
"""
from __future__ import annotations

import logging
import os

import numpy as np
import pandas as pd

from crypto_trading.crypto_common import execution
from crypto_trading.crypto_common.loader import load_index_live

from . import common

logger = logging.getLogger(__name__)

NAME = "w2_chronos"
MARKETS = {"KXBTCPERP": ("BTC", 0.0001), "KXETHPERP": ("ETH", 0.0001)}
CTX, HORIZON_STEPS, BAND_BPS = 512, 48, 40.0
HOLD = pd.Timedelta(hours=4)
_PIPE = None


def _pipe():
    global _PIPE
    if _PIPE is None:
        os.environ.setdefault("CHRONOS_MODEL", "amazon/chronos-bolt-base")
        from chronos import BaseChronosPipeline
        _PIPE = BaseChronosPipeline.from_pretrained(
            os.environ["CHRONOS_MODEL"], device_map="cpu")
    return _PIPE


def forecast_bps(asset: str) -> float | None:
    """One-shot forecast of the 4h terminal move (bps) from the latest context.

    Context comes from the ALWAYS-ON 5s live index recorder (ctx512 × 5min ≈
    43h → last 3 days of files), so freshness never depends on a backfill run.
    """
    import torch
    now = pd.Timestamp.now(tz="UTC")
    days = [(now - pd.Timedelta(days=d)).strftime("%Y-%m-%d") for d in (2, 1, 0)]
    live = load_index_live(asset, days=days)
    if live.empty:
        return None
    comp = (live["index"].resample("5min", label="right", closed="right")
            .last().dropna())
    if len(comp) < CTX + 2:
        return None
    age = (now - comp.index[-1]).total_seconds()
    if age > 900:
        logger.warning("%s live index stale %.0fs — is idxrecord running?", asset, age)
        return None
    ctx = torch.tensor(comp.tail(CTX).to_numpy(dtype="float32"))
    q, _ = _pipe().predict_quantiles([ctx], prediction_length=HORIZON_STEPS,
                                     quantile_levels=[0.1, 0.5, 0.9])
    med = float(q[0, -1, 1])
    last = float(comp.iloc[-1])
    return (med / last - 1.0) * 1e4


def run(cfg: dict | None = None) -> dict:
    cfg = (cfg or common.load_cfg())[NAME]
    st = common.load_state(NAME)
    if common.kill_check(NAME, st, cfg):
        return {"strategy": NAME, "status": "KILLED"}
    now = pd.Timestamp.now(tz="UTC")
    positions = st.setdefault("positions", {})
    pendings = st.setdefault("pendings", {})
    probe = st.setdefault("probe", {"posted": 0, "filled": 0, "unfilled": 0})
    rep = {"strategy": NAME, "markets": {}}
    for tk, (asset, tick) in MARKETS.items():
        m: dict = {}
        # fill probe: pending maker order → verified fill or timeout
        pend = pendings.get(tk)
        if pend is not None:
            v = common.verify_maker_fill(tk, "bid" if pend["side"] > 0 else "ask",
                                         pend["limit"], pend["posted"],
                                         timeout_min=15)
            if v["status"] == "filled":
                probe["filled"] += 1
                positions[tk] = {"side": pend["side"], "entry_px": pend["limit"],
                                 "opened": v.get("fill_ts"),
                                 "exit_due": pend["exit_due"]}
                pendings[tk] = None
                common.log_line(NAME, {"action": "virtual_fill", "ticker": tk, **v})
            elif v["status"] == "unfilled":
                probe["unfilled"] += 1
                pendings[tk] = None
                common.log_line(NAME, {"action": "virtual_unfilled", "ticker": tk})
            else:
                rep["markets"][tk] = {"status": "PENDING_FILL"}
                continue
        pos = positions.get(tk)
        if pos is not None:                            # manage exit at +4h
            if now >= pd.Timestamp(pos["exit_due"]):
                bid, ask = common.latest_touch(tk)
                if bid is not None:
                    side = -pos["side"]
                    px = bid if side < 0 else ask
                    o = execution.Order.from_signed(tk, side * cfg["contracts"],
                                                    round(px, 4),
                                                    tif="immediate_or_cancel",
                                                    reduce_only=True,
                                                    subaccount=common.load_cfg()["subaccount"],
                                                    tick_size=tick)
                    m["emit"] = common.emit(NAME, o, enabled=cfg["enabled"],
                                            reason="EXIT 4h horizon")
                    entry_px = pos.get("entry_px")
                    if entry_px:                    # verified-fill paper P&L
                        from crypto_trading.crypto_common.costs import fee_dollars
                        gross = pos["side"] * (px - entry_px) * cfg["contracts"]
                        fees = (fee_dollars(entry_px * cfg["contracts"], role="maker", ticker=tk)
                                + fee_dollars(px * cfg["contracts"], role="taker", ticker=tk))
                        net = gross - fees
                        st["cum_net_usd"] = st.get("cum_net_usd", 0.0) + net
                        st.setdefault("trades", []).append(
                            {"ticker": tk, "entry": entry_px, "exit": px,
                             "net_usd": round(net, 4), "closed": str(now)})
                    positions[tk] = None
                    m["status"] = "EXIT_PLACED"
            else:
                m["status"] = f"HOLDING until {pos['exit_due']}"
        else:
            pred = forecast_bps(asset)
            m["pred_bps"] = None if pred is None else round(pred, 1)
            if pred is not None and abs(pred) >= BAND_BPS:
                bid, ask = common.latest_touch(tk)
                if bid is not None:
                    side = int(np.sign(pred))
                    px = bid if side > 0 else ask       # maker at touch
                    o = execution.Order.from_signed(tk, side * cfg["contracts"],
                                                    round(px, 4), post_only=True,
                                                    subaccount=common.load_cfg()["subaccount"],
                                                    tick_size=tick)
                    m["emit"] = common.emit(NAME, o, enabled=cfg["enabled"],
                                            reason=f"ENTER pred={pred:+.0f}bps")
                    probe["posted"] += 1
                    pendings[tk] = {"side": side, "limit": round(px, 4),
                                    "posted": str(now),
                                    "exit_due": str(now + HOLD)}
                    m["status"] = "ORDER_POSTED_AWAITING_FILL"
            elif "status" not in m:
                m["status"] = "FLAT_NO_SIGNAL"
        rep["markets"][tk] = m
    common.save_state(NAME, st)
    return rep
