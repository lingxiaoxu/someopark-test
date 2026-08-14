"""W4 — S3-improved same-asset hedged carry, live module (frozen 2026-08-10).

Frozen "always30" rule: short KXBTCPERP + long spot BTC, always on; exit only
when the trailing 30-day settled-funding sum turns negative. Cadence: daily.

The SPOT LEG IS EXTERNAL (Coinbase/Kraken). This module never pretends to
control it: it emits the Kalshi short-leg order plus an explicit spot-leg
instruction, and it will not consider the position OPEN until the human
confirms the spot fill with ``--confirm-spot`` (runner flag). Daily runs then
monitor: trailing-30d funding (the stop), the perp-spot basis (hedge health),
and accrued funding income.
"""
from __future__ import annotations

import logging

import pandas as pd

from crypto_trading.crypto_common import execution
from crypto_trading.crypto_common.loader import (load_funding,
                                                 load_index_live,
                                                 load_poll_market_stats)

from . import common

logger = logging.getLogger(__name__)

NAME = "w4_carry"
TICKER, ASSET = "KXBTCPERP", "BTC"
TICK = 0.0001
BASIS_ALERT_BPS = 150.0        # hedge-health alert (sample range was −41..+105)


def funding_state() -> dict:
    f = load_funding(TICKER)["funding_rate"].sort_index()
    now = pd.Timestamp.now(tz="UTC")
    trail30 = float(f[f.index >= now - pd.Timedelta(days=30)].sum())
    age_h = (now - f.index.max()).total_seconds() / 3600 if len(f) else 1e9
    return {"trail30_sum": trail30,
            "trail30_ann_pct": round(trail30 / 30 * 365 * 100, 2),
            "last_settlement_age_h": round(age_h, 1),
            "stale": age_h > 16}       # >2 cycles behind → run kalshi.backfill


def basis_bps() -> float | None:
    bid, ask = common.latest_touch(TICKER)
    if bid is None:
        return None
    st = load_poll_market_stats(
        TICKER, days=[pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")])
    csize = float(st.contract_size.dropna().median())
    live = load_index_live(ASSET)
    if live.empty:
        return None
    spot = float(live["index"].iloc[-1])
    mid_u = (bid + ask) / 2 / csize
    return (mid_u - spot) / spot * 1e4


def run(cfg: dict | None = None, *, confirm_spot: bool = False) -> dict:
    cfg = (cfg or common.load_cfg())[NAME]
    st = common.load_state(NAME)
    fs = funding_state()
    b = basis_bps()
    rep = {"strategy": NAME, "funding": fs,
           "basis_bps": None if b is None else round(b, 1),
           "position": st.get("position")}
    if fs["stale"]:
        rep["status"] = "FUNDING_DATA_STALE — run crypto_common.kalshi.backfill"
        common.log_line(NAME, rep)
        return rep
    if b is not None and abs(b) > BASIS_ALERT_BPS:
        rep["alert"] = f"basis {b:+.0f}bps outside sample range — hedge health check"
        logger.warning("[%s] %s", NAME, rep["alert"])

    pos = st.get("position")
    stop = fs["trail30_sum"] < 0
    if pos is None and not stop:
        bid, ask = common.latest_touch(TICKER)
        if bid is None:
            rep["status"] = "NO_TOUCH"
        else:
            o = execution.Order.from_signed(TICKER, -cfg["contracts"],
                                            round(ask, 4), post_only=True,
                                            subaccount=common.load_cfg()["subaccount"],
                                            tick_size=TICK)
            rep["emit"] = common.emit(NAME, o, enabled=cfg["enabled"],
                                      reason=f"ENTER short leg (funding trail30 "
                                             f"{fs['trail30_ann_pct']:+.2f}%/yr)")
            st_notional = cfg["contracts"] * ask
            rep["spot_leg_instruction"] = (
                f"BUY spot {ASSET} ≈ ${st_notional:.0f} notional on Coinbase/Kraken "
                f"(maker limit preferred), then re-run with --confirm-spot")
            if confirm_spot or rep["emit"].get("submitted"):
                st["position"] = {"opened": str(pd.Timestamp.now(tz='UTC')),
                                  "contracts": cfg["contracts"],
                                  "spot_confirmed": bool(confirm_spot)}
                common.save_state(NAME, st)
            rep["status"] = "ENTRY_EMITTED"
    elif pos is not None and stop:
        bid, ask = common.latest_touch(TICKER)
        if bid is not None:
            o = execution.Order.from_signed(TICKER, +pos["contracts"],
                                            round(bid, 4),
                                            tif="immediate_or_cancel",
                                            reduce_only=True,
                                            subaccount=common.load_cfg()["subaccount"],
                                            tick_size=TICK)
            rep["emit"] = common.emit(NAME, o, enabled=cfg["enabled"],
                                      reason="EXIT: trailing-30d funding sum < 0 (frozen stop)")
            rep["spot_leg_instruction"] = f"SELL the spot {ASSET} hedge leg now"
            st["position"] = None
            common.save_state(NAME, st)
            rep["status"] = "STOP_EXIT_EMITTED"
    else:
        rep["status"] = "ON — collecting" if pos is not None else "OFF (stop active)"
    common.log_line(NAME, rep)
    return rep
