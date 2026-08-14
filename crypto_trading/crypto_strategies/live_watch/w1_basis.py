"""W1 — S1 basis-selective, live signal (frozen 2026-08-10).

Frozen config = the tier-study cell: entry_k=3.5, min_abs=10bps, offset=10
ticks behind the touch, hard-abort at 20bps, flow-fading filter ON, OI filter
OFF, BTC only. Effective holding <15 min, signal-driven exits (|z| back below
exit_k). Cadence: run every minute (pipeline watch loop).

Live translation of the backtest mechanics:
  entry  — post_only limit 10 ticks BEHIND the touch on the signal side;
  exit   — when z reverts (|z| < exit_k) or the 20bps hard-abort trips, place
           a reduce-only IOC at the adverse touch (the honest exit the
           backtest was charged for).
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from crypto_trading.crypto_common import execution
from crypto_trading.crypto_common.loader import (load_index_live,
                                                 load_poll_market_stats)
from crypto_trading.crypto_strategies.basis_meanrev.signals.basis import (
    ou_half_life, rolling_zscore)

from . import common

logger = logging.getLogger(__name__)

NAME = "w1_basis"
TICKER, ASSET = "KXBTCPERP", "BTC"
ENTRY_K, EXIT_K = 3.5, 0.5
MIN_ABS_BPS, ABORT_BPS = 10.0, 20.0
OFFSET_TICKS, TICK = 10, 0.0001        # BTC perp tick size (probe-verified)
HL_MAX_MIN = 60.0


def signal_frame(days: list[str]) -> pd.DataFrame:
    st = load_poll_market_stats(TICKER, days=days)
    csize = float(st.contract_size.dropna().median())
    m = (st[["bid", "ask"]].dropna()
         .resample("1min", label="right", closed="right").last().dropna())
    m["mid_u"] = (m.bid + m.ask) / 2 / csize
    # spot from the ALWAYS-ON 5s live recorder (the backfilled composite parquet
    # only advances when a backfill runs — useless for a live signal)
    live = load_index_live(ASSET, days=days)
    spot = (live["index"].resample("1min", label="right", closed="right").last())
    f = m.join(spot.rename("spot"), how="inner").dropna()
    f["b_bps"] = (f.mid_u - f.spot) / f.spot * 1e4
    f["z"] = rolling_zscore(f.b_bps / 1e4, 30)
    hl = (f.b_bps.rolling(240, min_periods=60)
          .apply(lambda w: ou_half_life(pd.Series(w)) or np.nan, raw=False))
    f["hl"] = hl
    return f


def run(cfg: dict | None = None) -> dict:
    cfg = (cfg or common.load_cfg())[NAME]
    st = common.load_state(NAME)
    if common.kill_check(NAME, st, cfg):
        return {"strategy": NAME, "status": "KILLED", "state": st.get("killed_reason")}

    today = pd.Timestamp.now(tz="UTC")
    days = [(today - pd.Timedelta(days=d)).strftime("%Y-%m-%d") for d in (1, 0)]
    f = signal_frame(days)
    if f.empty or (today - f.index[-1]).total_seconds() > 180:
        return {"strategy": NAME, "status": "STALE_DATA"}
    row = f.iloc[-1]
    bid, ask = common.latest_touch(TICKER)
    rep = {"strategy": NAME, "z": round(float(row.z), 2),
           "b_bps": round(float(row.b_bps), 1),
           "hl_min": None if pd.isna(row.hl) else round(float(row.hl), 1),
           "position": st["position"]}
    if bid is None:
        rep["status"] = "NO_TOUCH"
        return rep

    # ── fill probe: a posted maker order becomes a position ONLY when the real
    # trades tape shows it would have filled (the maker analog of W5's capture
    # probe — without it, paper P&L silently assumes a 100% maker fill rate)
    probe = st.setdefault("probe", {"posted": 0, "filled": 0, "unfilled": 0})
    pend = st.get("pending_order")
    if pend is not None:
        v = common.verify_maker_fill(TICKER, "bid" if pend["side"] > 0 else "ask",
                                     pend["limit"], pend["posted"], timeout_min=10)
        if v["status"] == "filled":
            probe["filled"] += 1
            st["position"] = {"side": pend["side"], "entry_px": pend["limit"],
                              "entry_b_bps": pend["entry_b_bps"],
                              "opened": v.get("fill_ts")}
            st["pending_order"] = None
            common.log_line(NAME, {"action": "virtual_fill", **v, **pend})
        elif v["status"] == "unfilled":
            probe["unfilled"] += 1
            st["pending_order"] = None
            common.log_line(NAME, {"action": "virtual_unfilled", **pend})
        else:
            common.save_state(NAME, st)
            rep["status"] = "PENDING_FILL"
            return rep
        common.save_state(NAME, st)

    pos = st["position"]
    if pos is None:
        fadeable = (abs(row.z) >= ENTRY_K and abs(row.b_bps) >= MIN_ABS_BPS
                    and not pd.isna(row.hl) and row.hl <= HL_MAX_MIN)
        if fadeable:
            side = -int(np.sign(row.z))               # fade the dislocation
            limit = (bid - OFFSET_TICKS * TICK) if side > 0 \
                else (ask + OFFSET_TICKS * TICK)
            o = execution.Order.from_signed(TICKER, side * cfg["contracts"],
                                            round(limit, 4), post_only=True,
                                            subaccount=common.load_cfg()["subaccount"],
                                            tick_size=TICK)
            rep["emit"] = common.emit(NAME, o, enabled=cfg["enabled"],
                                      reason=f"ENTER fade z={row.z:+.2f} b={row.b_bps:+.1f}bps")
            probe["posted"] += 1
            st["pending_order"] = {"side": side, "limit": round(limit, 4),
                                   "entry_b_bps": float(row.b_bps),
                                   "posted": str(today)}
            common.save_state(NAME, st)
            rep["status"] = "ORDER_POSTED_AWAITING_FILL"
        else:
            rep["status"] = "FLAT_NO_SIGNAL"
    else:
        reverted = abs(row.z) < EXIT_K
        aborted = abs(row.b_bps) >= abs(pos["entry_b_bps"]) + ABORT_BPS
        if reverted or aborted:
            side = -pos["side"]
            px = bid if side < 0 else ask              # adverse touch, honest
            o = execution.Order.from_signed(TICKER, side * cfg["contracts"],
                                            round(px, 4), tif="immediate_or_cancel",
                                            reduce_only=True,
                                            subaccount=common.load_cfg()["subaccount"],
                                            tick_size=TICK)
            rep["emit"] = common.emit(NAME, o, enabled=cfg["enabled"],
                                      reason="EXIT " + ("revert" if reverted else "ABORT"))
            entry_px = pos.get("entry_px")
            if entry_px:                            # verified-fill paper P&L
                from crypto_trading.crypto_common.costs import fee_dollars
                gross = pos["side"] * (px - entry_px) * cfg["contracts"]
                fees = (fee_dollars(entry_px * cfg["contracts"], role="maker",
                                    ticker=TICKER)
                        + fee_dollars(px * cfg["contracts"], role="taker",
                                      ticker=TICKER))
                net = gross - fees
                st["cum_net_usd"] = st.get("cum_net_usd", 0.0) + net
                st.setdefault("trades", []).append(
                    {"entry": entry_px, "exit": px, "net_usd": round(net, 4),
                     "closed": str(today)})
            st["position"] = None
            common.save_state(NAME, st)
            rep["status"] = "EXIT_PLACED"
        else:
            rep["status"] = "HOLDING"
    return rep
