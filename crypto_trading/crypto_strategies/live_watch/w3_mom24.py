"""W3 — S9 24h momentum, live signal (frozen 2026-08-10).

Frozen rule: uniform CONTINUATION direction on mom_24h_volscaled, trailing-z
(168h window) |z| >= 1, hold exactly 24h, one position per market. Majors only
for live (config tickers) — the watchlist's statistical pooling stays on all
13, but live depth favors the majors. Cadence: hourly.

Live mechanics: maker entry at the touch on the momentum side; exit 24h later
via adverse-touch IOC. Hourly bars come straight from the poll tape so the
module has no dependency on candle backfill freshness.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from crypto_trading.crypto_common import execution
from crypto_trading.crypto_common.loader import load_poll_market_stats

from . import common

logger = logging.getLogger(__name__)

NAME = "w3_mom24"
ZWIN, Q = 168, 1.0
HOLD = pd.Timedelta(hours=24)
TICK = 0.0001


def latest_z(ticker: str) -> float | None:
    """Trailing-z of vol-scaled 24h momentum from ~9 days of poll-tape hours."""
    today = pd.Timestamp.now(tz="UTC")
    days = [(today - pd.Timedelta(days=d)).strftime("%Y-%m-%d") for d in range(9, -1, -1)]
    st = load_poll_market_stats(ticker, days=days)
    if st.empty:
        return None
    px = ((st.bid + st.ask) / 2).dropna().resample(
        "1h", label="right", closed="right").last().dropna()
    if len(px) < ZWIN + 26:
        return None
    mom = px.pct_change(24)
    sigma = px.pct_change().rolling(ZWIN, min_periods=48).std(ddof=0)
    sig = mom / (sigma * np.sqrt(24)).replace(0, np.nan)
    mu = sig.rolling(ZWIN, min_periods=48).mean()
    sd = sig.rolling(ZWIN, min_periods=48).std(ddof=0)
    z = ((sig - mu) / sd.replace(0.0, np.nan)).iloc[-1]
    return None if pd.isna(z) else float(z)


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
    for tk in cfg.get("tickers", []):
        m: dict = {}
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
        if pos is not None:
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
                                                    tick_size=TICK)
                    m["emit"] = common.emit(NAME, o, enabled=cfg["enabled"],
                                            reason="EXIT 24h horizon")
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
            z = latest_z(tk)
            m["z"] = None if z is None else round(z, 2)
            if z is not None and abs(z) >= Q:
                bid, ask = common.latest_touch(tk)
                if bid is not None:
                    side = int(np.sign(z))              # uniform continuation
                    px = bid if side > 0 else ask
                    o = execution.Order.from_signed(tk, side * cfg["contracts"],
                                                    round(px, 4), post_only=True,
                                                    subaccount=common.load_cfg()["subaccount"],
                                                    tick_size=TICK)
                    m["emit"] = common.emit(NAME, o, enabled=cfg["enabled"],
                                            reason=f"ENTER continuation z={z:+.2f}")
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
