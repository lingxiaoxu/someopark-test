"""W6 — residual jump lead-lag, live probe (frozen 2026-08-20).

Frozen rule (identical constants imported from the canonical backtest, so the
two can never drift): a 30s index jump ≥25bps where the perp has NOT yet
followed (residual > 7.0bps) and the book is tight (spread ≤ 2.1bps) → taker
entry at the adverse touch in the jump direction, hold 1 minute, taker exit.

What this probe measures that tape cannot: the strategy reacts to a 5s index
stream but our quote tape is 10s, so live entry may be a few seconds later
than the backtest assumes. Each fired signal therefore records BOTH the quote
we would have hit and the quote actually available on the next poll — the gap
between them is the live slippage the backtest cannot see. Virtual positions
settle 1 minute later from the same tape.

Cadence: 10s (matches the poll recorder; the runner gates it).
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from crypto_trading.crypto_common import execution
from crypto_trading.crypto_common.loader import (load_index_live,
                                                 load_poll_market_stats)
from crypto_trading.crypto_strategies.jump_leadlag.research_residual import (
    HOLD_MIN, JUMP_BPS, JUMP_LOOKBACK, MARKETS, RESIDUAL_MIN, SPREAD_MAX_BPS,
    residual_bps)

from . import common

logger = logging.getLogger(__name__)

NAME = "w6_residual"
TICK = 0.0001
STALE_MAX_S = 30.0


def evaluate(ticker: str, asset: str) -> dict | None:
    """Frozen trigger on the freshest data. Returns None when nothing fires."""
    now = pd.Timestamp.now(tz="UTC")
    days = [(now - pd.Timedelta(days=d)).strftime("%Y-%m-%d") for d in (1, 0)]
    live = load_index_live(asset, days=days)["index"].sort_index()
    if len(live) < JUMP_LOOKBACK + 2:
        return None
    if (now - live.index[-1]).total_seconds() > 60:
        return {"status": "STALE_INDEX"}
    j = float((live.iloc[-1] / live.iloc[-1 - JUMP_LOOKBACK] - 1) * 1e4)
    if abs(j) < JUMP_BPS:
        return {"status": "NO_JUMP", "jump_bps": round(j, 1)}

    st = load_poll_market_stats(ticker, days=days)
    q = st.dropna(subset=["bid", "ask"])
    if q.empty or (now - q.index[-1]).total_seconds() > STALE_MAX_S:
        return {"status": "STALE_QUOTE"}
    bid, ask = float(q.bid.iloc[-1]), float(q.ask.iloc[-1])
    mid = (bid + ask) / 2.0
    spread = (ask - bid) / mid * 1e4
    prev = q[q.index <= q.index[-1] - pd.Timedelta(seconds=30)]
    if prev.empty:
        return {"status": "NO_PRIOR_QUOTE"}
    prev_mid = float((prev.bid.iloc[-1] + prev.ask.iloc[-1]) / 2.0)
    perp_move = (mid / prev_mid - 1) * 1e4
    resid = residual_bps(j, perp_move)
    rec = {"jump_bps": round(j, 1), "perp_move_bps": round(perp_move, 1),
           "residual_bps": round(resid, 1), "spread_bps": round(spread, 2),
           "bid": bid, "ask": ask}
    if resid <= RESIDUAL_MIN or spread > SPREAD_MAX_BPS:
        rec["status"] = "JUMP_FILTERED_OUT"
        return rec
    rec.update({"status": "FIRE", "side": int(np.sign(j)),
                "entry": ask if j > 0 else bid})
    return rec


def run(cfg: dict | None = None, **_) -> dict:
    cfg = (cfg or common.load_cfg()).get(NAME, {})
    st = common.load_state(NAME)
    if common.kill_check(NAME, st, cfg):
        return {"strategy": NAME, "status": "KILLED"}
    now = pd.Timestamp.now(tz="UTC")
    vpos = st.setdefault("positions", {})
    probe = st.setdefault("probe", {"jumps": 0, "fired": 0})
    rep = {"strategy": NAME, "markets": {}}

    for tk, asset in MARKETS.items():
        m: dict = {}
        pos = vpos.get(tk)
        if pos is not None:                      # settle after the 1-min hold
            if now >= pd.Timestamp(pos["exit_due"]):
                q = load_poll_market_stats(
                    tk, days=[now.strftime("%Y-%m-%d")]).dropna(subset=["bid", "ask"])
                if not q.empty:
                    bid, ask = float(q.bid.iloc[-1]), float(q.ask.iloc[-1])
                    side = pos["side"]
                    exitp = bid if side > 0 else ask          # adverse, taker
                    gross = side * (exitp - pos["entry"]) / pos["entry"] * 1e4
                    # Report BOTH fee regimes: tier 0 is what this account pays
                    # today, tier 4 is the capital-plan anchor the backtest
                    # quotes. Conflating them cost a full 10bps of confusion on
                    # the first live day.
                    from crypto_trading.crypto_common.costs import (FEE_TIERS_BPS,
                                                                    load_fee_rates)
                    _, taker_now = load_fee_rates(tk)
                    net_now = gross - 2 * taker_now * 1e4
                    net_t4 = gross - 2 * FEE_TIERS_BPS[4][1]
                    usd = net_now / 1e4 * pos["entry"] * cfg.get("contracts", 10)
                    st["cum_net_usd"] = st.get("cum_net_usd", 0.0) + usd
                    st["cum_net_usd_t4"] = st.get("cum_net_usd_t4", 0.0) + \
                        net_t4 / 1e4 * pos["entry"] * cfg.get("contracts", 10)
                    st.setdefault("trades", []).append(
                        {"ticker": tk, "entry": pos["entry"], "exit": exitp,
                         "gross_bps": round(gross, 2), "net_bps": round(net_now, 2),
                         "net_bps_t4": round(net_t4, 2),
                         "net_usd": round(usd, 4), "closed": str(now)})
                    o = execution.Order.from_signed(
                        tk, -side * cfg.get("contracts", 10), round(exitp, 4),
                        tif="immediate_or_cancel", reduce_only=True,
                        subaccount=common.load_cfg()["subaccount"], tick_size=TICK)
                    m["emit"] = common.emit(NAME, o, enabled=cfg.get("enabled", False),
                                            reason="EXIT 1min hold")
                    vpos[tk] = None
                    m["status"] = "EXIT"
                    m["net_bps"] = round(net_now, 2)
                    m["net_bps_t4"] = round(net_t4, 2)
            else:
                m["status"] = "HOLDING"
        else:
            ev = evaluate(tk, asset)
            if ev is None:
                m["status"] = "NO_DATA"
            elif ev["status"] == "FIRE":
                probe["jumps"] += 1
                probe["fired"] += 1
                side = ev["side"]
                o = execution.Order.from_signed(
                    tk, side * cfg.get("contracts", 10), round(ev["entry"], 4),
                    tif="immediate_or_cancel",           # taker: cross now
                    subaccount=common.load_cfg()["subaccount"], tick_size=TICK)
                m["emit"] = common.emit(
                    NAME, o, enabled=cfg.get("enabled", False),
                    reason=f"FIRE jump={ev['jump_bps']:+.0f} resid={ev['residual_bps']:+.0f}")
                vpos[tk] = {"side": side, "entry": ev["entry"],
                            "opened": str(now),
                            "exit_due": str(now + pd.Timedelta(minutes=HOLD_MIN))}
                m.update({k: ev[k] for k in
                          ("jump_bps", "residual_bps", "spread_bps")})
                m["status"] = "ENTERED"
                common.log_line(NAME, {"action": "fire", "ticker": tk, **ev})
            else:
                if ev["status"] == "JUMP_FILTERED_OUT":
                    probe["jumps"] += 1
                m.update({k: v for k, v in ev.items() if k != "status"})
                m["status"] = ev["status"]
        rep["markets"][tk] = m

    common.save_state(NAME, st)
    j = probe["jumps"]
    rep["probe"] = {**probe,
                    "fire_rate": round(probe["fired"] / j, 3) if j else None}
    rep["paper_cum_usd"] = round(st.get("cum_net_usd", 0.0), 3)
    rep["paper_cum_usd_t4"] = round(st.get("cum_net_usd_t4", 0.0), 3)
    tr = st.get("trades", [])
    if tr:
        rep["mean_gross_bps"] = round(
            float(np.mean([t["gross_bps"] for t in tr])), 2)
        rep["mean_net_bps_t4"] = round(
            float(np.mean([t.get("net_bps_t4", np.nan) for t in tr])), 2)
    return rep
