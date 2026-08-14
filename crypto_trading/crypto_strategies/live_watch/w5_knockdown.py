"""W5 — knockdown live probe (frozen 2026-08-11).

Purpose is DUAL, and the first half matters more than the second right now:
  1. CAPTURE-RATE PROBE — the backtest's one untestable assumption is that the
     resting depth our (single) recorder saw was still there to hit in real
     time. Every run re-evaluates the frozen trigger on the LATEST snapshot
     and immediately checks the LATEST L2 book: signal fired AND depth≥50
     present now = capturable. The running capture ratio is the number the
     whole strategy is waiting for.
  2. VIRTUAL POSITIONS — capturable signals open paper positions at the ask;
     settlements (next runs, via the venue's own spot_est at close) accumulate
     a forward paper P&L to compare against the backtest's +25.6c/contract.

Order emission: event contracts trade on Kalshi's EVENTS API, which is NOT
wired into the perps ExecutionRouter — deliberately. Until the capture probe
passes (see watchlist criteria), this module only logs the exact intended
order. Wiring the events API is the arming step, a separate human decision.

Cadence: every 90s (the strips recorder's own rhythm; runner loop handles it).
"""
from __future__ import annotations

import gzip
import json
import logging
import os

import pandas as pd

from crypto_trading.crypto_common.config import PRICE_DATA

from . import common
from crypto_trading.crypto_strategies.event_binary.research_knockdown import (
    MIN_DEPTH, MONEYNESS_BPS, KNIFE_BPS, LOOKBACK_SNAPS, PRIMARY, ZONE, fee,
    knockdown_trigger, settle_outcome)

logger = logging.getLogger(__name__)

NAME = "w5_knockdown"
SERIES = "KXBTC"
STRIPS = PRICE_DATA / "kalshi" / "event_strips" / "prod" / SERIES


def _tail_lines(path, max_bytes: int = 2_500_000) -> list[str]:
    """Last lines of a (possibly huge) jsonl file without reading it all."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - max_bytes))
            chunk = fh.read().decode("utf-8", errors="ignore")
        lines = chunk.splitlines()
        return lines[1:] if size > max_bytes else lines
    except OSError:
        return []


def latest_snapshot() -> dict | None:
    day = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
    f = STRIPS / "markets" / f"{day}.jsonl"
    for line in reversed(_tail_lines(f, 3_000_000)):
        line = line.strip()
        if line:
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


def latest_books() -> dict:
    """ticker → (recv_ts, {no_px:sz}, {yes_px:sz}) from today's L2 tail."""
    day = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
    out: dict = {}
    for line in _tail_lines(STRIPS / "orderbook" / f"{day}.jsonl", 4_000_000):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        ob = (d.get("ob") or {}).get("orderbook_fp") or {}
        out[d.get("ticker")] = (
            d["recv_ts"],
            {round(float(p), 2): float(s) for p, s in (ob.get("no_dollars") or [])},
            {round(float(p), 2): float(s) for p, s in (ob.get("yes_dollars") or [])})
    return out


def run(cfg: dict | None = None, **_) -> dict:
    cfg = (cfg or common.load_cfg()).get(NAME, {})
    st = common.load_state(NAME)
    snap = latest_snapshot()
    if snap is None:
        return {"strategy": NAME, "status": "NO_SNAPSHOT"}
    now = pd.Timestamp.now(tz="UTC")
    snap_age = now.timestamp() - snap["recv_ts"]
    if snap_age > 300:
        return {"strategy": NAME, "status": f"STALE_SNAPSHOT {snap_age:.0f}s"}
    spot = snap.get("spot_est")
    books = latest_books()
    hist = st.setdefault("hist", {})
    vpos = st.setdefault("virtual_positions", {})
    probe = st.setdefault("probe", {"signals": 0, "capturable": 0})

    # ── settle virtual positions whose close has passed ──
    settled = []
    for key, p in list(vpos.items()):
        ct = pd.Timestamp(p["close"])
        if now < ct + pd.Timedelta(minutes=2):
            continue
        s_spot = p.get("last_spot")
        if s_spot is None:
            continue
        if abs(s_spot - p["k"]) / s_spot * 1e4 < KNIFE_BPS:
            vpos.pop(key)
            continue
        win = settle_outcome(p["side"], p["k"], s_spot)
        pnl = ((1.0 if win else 0.0) - p["px"] - fee(p["px"])) * 100
        st["cum_net_usd"] = st.get("cum_net_usd", 0.0) + pnl / 100 * p["contracts"]
        st.setdefault("trades", []).append(
            {"key": key, "px": p["px"], "win": win, "pnl_c": round(pnl, 2)})
        settled.append({"key": key, "win": win, "pnl_c": round(pnl, 2)})
        vpos.pop(key)

    # ── evaluate the frozen trigger on the latest snapshot ──
    new_signals = []
    for m in snap.get("markets", []):
        try:
            k = float(m.get("floor_strike") or 0)
            tkr = m.get("ticker") or ""
            ct = m.get("close_time")
            tte = (pd.Timestamp(ct).timestamp() - snap["recv_ts"]) / 60
            ya = float(m.get("yes_ask_dollars") or 0)
            na = float(m.get("no_ask_dollars") or 0)
        except Exception:
            continue
        if k <= 0 or not spot or tte < -1 or tte > 90:
            continue
        # keep every open position's settlement spot fresh
        for key, p in vpos.items():
            if p["close"] == ct:
                p["last_spot"] = spot
        if abs(k - spot) / spot * 1e4 > MONEYNESS_BPS:
            hist.pop(tkr, None)
            continue
        h = hist.setdefault(tkr, [])
        h.append((ya, na))
        if len(h) > LOOKBACK_SNAPS + 1:
            h.pop(0)
        if tkr in vpos or not (PRIMARY["tte_lo"] <= tte <= PRIMARY["tte_hi"]):
            continue
        side = knockdown_trigger(h[:-1], ya, na, PRIMARY["dip_c"])
        if side is None:
            continue
        ask = ya if side == "yes" else na
        probe["signals"] += 1
        # ── the capture probe: is real depth present RIGHT NOW? ──
        depth = 0.0
        bk = books.get(tkr)
        if bk and abs(bk[0] - snap["recv_ts"]) <= 180:
            opp = bk[1] if side == "yes" else bk[2]
            tgt = round(1 - ask, 2)
            depth = sum(s for p_, s in opp.items() if abs(p_ - tgt) <= 0.015)
        capturable = depth >= MIN_DEPTH
        if capturable:
            probe["capturable"] += 1
            contracts = int(cfg.get("contracts", 25))
            vpos[tkr] = {"side": side, "k": k, "close": ct, "px": ask,
                         "contracts": contracts, "opened": str(now),
                         "last_spot": spot}
            common.log_line(NAME, {
                "action": "intended_event_order", "ticker": tkr, "side": side,
                "price": ask, "contracts": contracts, "depth_seen": depth,
                "note": "events API not wired until capture probe passes — "
                        "virtual position opened"})
        new_signals.append({"ticker": tkr, "side": side, "ask": ask,
                            "depth": depth, "capturable": capturable})

    common.save_state(NAME, st)
    cap_rate = (probe["capturable"] / probe["signals"]
                if probe["signals"] else None)
    rep = {"strategy": NAME, "snapshot_age_s": round(snap_age),
           "new_signals": new_signals, "settled": settled,
           "open_virtual": len(vpos),
           "probe": {**probe, "capture_rate":
                     None if cap_rate is None else round(cap_rate, 3)},
           "paper_cum_usd": round(st.get("cum_net_usd", 0.0), 2)}
    common.log_line(NAME, {"action": "heartbeat", **{k: rep[k] for k in
                           ("open_virtual", "probe", "paper_cum_usd")}})
    return rep
