"""W7 — up-side premium fade on 15-minute up/down binaries (re-frozen 2026-08-27).

W7's slot was re-registered (user decision, 2026-08-27) from the hourly
noise-deficit rule to the 15-minute FAVORITE-NO rule after a full calibration
scan showed the earlier family had us on the paying side of the mispricing:
buying the knocked (usually YES) side loses at every entry point, while its
mirror — buying the expensive NO — is positive at 6/6 entry points and in 5/5
coins. The hourly z≤0.1 research module (research_noisefade.py) is retained as
an archived finding; this probe no longer trades it.

Frozen rule (constants imported from the canonical module so the two cannot
drift; structural test enforces it):
  * markets: KXBTC15M / KXETH15M / KXSOL15M / KXDOGE15M / KXXRP15M
  * entry when ~8 minutes of the window remain (±1 min)
  * buy the FAVORITE's NO — only when NO is the expensive side
  * cost band [0.50, 0.98]; taker; hold to settlement
  * settlement = the venue's OFFICIAL result, never a model (W5's lesson)

Registered comparand (scan cell): n=1,621, hit 78.8% @ cost 0.750,
+2.62c/contract, NW-t 2.36, 135 trades/day. LIVE criteria (watchlist):
post-registration n ≥ 600 AND mean > 0 AND NW-t ≥ 2.5 — the bar is above the
usual 2.0 because this cell surfaced from an exploratory scan (24 cells,
Bonferroni |t| ≥ 3.08, which it did NOT clear on the scan's own data).

PAPER + demo mirror. Cadence 60s: 15-minute windows close 4×/hour per market,
and the entry point is a one-minute-wide target, so the probe must look often.
"""
from __future__ import annotations

import json
import logging
import os

import pandas as pd

from crypto_trading.crypto_common.config import PRICE_DATA
from crypto_trading.crypto_strategies.event_binary.research_favorite_no import (
    COST_HI, COST_LO, MARKETS, REM_MIN_TARGET, REM_TOLERANCE, favorite_is_no,
    fee, no_cost)

from . import common

logger = logging.getLogger(__name__)

NAME = "w7_noisefade"           # slot name kept: probe state/history continuity
STRIPS_ROOT = PRICE_DATA / "kalshi" / "event_strips" / "prod"


def _tail_lines(path, max_bytes: int = 1_500_000) -> list[str]:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - max_bytes))
            chunk = fh.read().decode("utf-8", errors="ignore")
        lines = chunk.splitlines()
        return lines[1:] if size > max_bytes else lines
    except OSError:
        return []


def latest_snapshot(series: str) -> dict | None:
    day = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
    for line in reversed(_tail_lines(STRIPS_ROOT / series / "markets" / f"{day}.jsonl")):
        line = line.strip()
        if line:
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


def official_result(ticker: str) -> str | None:
    """The venue's own settlement — the only accepted truth."""
    import requests

    from crypto_trading.crypto_common.kalshi.enums import rest_base
    try:
        r = requests.get(rest_base("prod") + f"/markets/{ticker}", timeout=8,
                         headers={"User-Agent": "someopark-crypto/0.1"})
        if r.status_code == 200:
            res = r.json().get("market", {}).get("result")
            return res if res in ("yes", "no") else None
    except requests.RequestException:
        pass
    return None


def quote(m: dict) -> tuple[float, float] | None:
    """(yes_bid, yes_ask) from a snapshot market row (dollars-as-strings)."""
    try:
        yb = float(m.get("yes_bid_dollars"))
        ya = float(m.get("yes_ask_dollars"))
    except (TypeError, ValueError):
        return None
    return (yb, ya) if 0.0 < yb < 1.0 and 0.0 < ya < 1.0 else None


def run(cfg: dict | None = None, **_) -> dict:
    cfg = (cfg or common.load_cfg()).get(NAME, {})
    st = common.load_state(NAME)
    if common.kill_check(NAME, st, cfg):
        return {"strategy": NAME, "status": "KILLED"}
    now = pd.Timestamp.now(tz="UTC")
    vpos = st.setdefault("positions", {})
    seen = st.setdefault("seen_tickers", [])
    # setdefault alone is NOT enough across a re-freeze: an existing state
    # carries the PREVIOUS rule's counter keys and `probe["looked"] += 1`
    # then raises KeyError mid-loop (measured 2026-08-27). Backfill keys.
    probe = st.setdefault("probe", {})
    for k in ("looked", "entered", "unresolved"):
        probe.setdefault(k, 0)
    contracts = int(cfg.get("contracts", 25))
    rep: dict = {"strategy": NAME}

    # ── settle matured positions with the official result ──
    settled = []
    for tkr, p in list(vpos.items()):
        ct = pd.Timestamp(p["close"])
        if now < ct + pd.Timedelta(minutes=3):
            continue
        res = official_result(tkr)
        if res is None:
            if now > ct + pd.Timedelta(minutes=45):
                probe["unresolved"] += 1
                vpos.pop(tkr)
                common.log_line(NAME, {"action": "unresolved_drop", "ticker": tkr})
            continue
        win = (res == "no")                      # we always buy NO
        pnl_c = ((1.0 if win else 0.0) - p["cost"] - fee(p["cost"])) * 100
        st["cum_net_usd"] = st.get("cum_net_usd", 0.0) + pnl_c / 100 * contracts
        # per-series books: the ONLY dimension that reproduced out-of-sample
        # (small caps +2.6~7.7c vs BTC −0.18c). If the ordering holds forward,
        # the thing to change is the COIN UNIVERSE, not the entry clock.
        sk = st.setdefault("by_series", {})
        b = sk.setdefault(p.get("series", "?"), {"n": 0, "wins": 0, "sum_c": 0.0})
        b["n"] += 1
        b["wins"] += int(win)
        b["sum_c"] = round(b["sum_c"] + pnl_c, 2)
        st.setdefault("trades", []).append(
            {"ticker": tkr, "series": p.get("series"), "cost": p["cost"],
             "win": win, "pnl_c": round(pnl_c, 2), "closed": str(now)})
        settled.append({"ticker": tkr, "win": win, "pnl_c": round(pnl_c, 2)})
        vpos.pop(tkr)

    # ── scan each market for a window at the entry point ──
    entries, per_series = [], {}
    for series in MARKETS:
        snap = latest_snapshot(series)
        if snap is None:
            per_series[series] = "NO_TAPE"
            continue
        age = now.timestamp() - snap["recv_ts"]
        if age > 180:
            per_series[series] = f"STALE {age:.0f}s"
            continue
        per_series[series] = "scanned"
        rts = snap["recv_ts"]
        for m in snap.get("markets", []):
            tkr, ct = m.get("ticker"), m.get("close_time")
            if not tkr or not ct or tkr in vpos or tkr in seen:
                continue
            rem_min = (pd.Timestamp(ct).timestamp() - rts) / 60.0
            if abs(rem_min - REM_MIN_TARGET) > REM_TOLERANCE:
                continue
            probe["looked"] += 1
            q = quote(m)
            if q is None:
                continue
            yb, ya = q
            if not favorite_is_no((yb + ya) / 2.0):
                continue                          # only when NO is the favorite
            cost = no_cost(yb)
            if not (COST_LO <= cost <= COST_HI):
                continue
            probe["entered"] += 1
            seen.append(tkr)
            vpos[tkr] = {"cost": round(cost, 4), "close": ct, "series": series,
                         "rem_min": round(rem_min, 2), "opened": str(now)}
            entry = {"action": "paper_entry", "series": series, "ticker": tkr,
                     "side": "no", "cost": round(cost, 4),
                     "rem_min": round(rem_min, 2), "contracts": contracts}
            if cfg.get("demo_mirror", False):
                from crypto_trading.crypto_common.execution_events import (
                    EventExecutionRouter)
                entry["demo_mirror"] = common.mirror_async(
                    NAME, EventExecutionRouter(strategy=NAME).mirror_demo,
                    side="no", close_time=ct, entry_price=cost,
                    contracts=contracts, series=(series, "KXBTC"))
            entries.append(entry)
            common.log_line(NAME, entry)

    if len(seen) > 3000:
        st["seen_tickers"] = seen[-1500:]
    common.save_state(NAME, st)
    return {**rep, "entries": entries, "settled": settled,
            "markets": per_series, "open_virtual": len(vpos), "probe": probe,
            "by_series": st.get("by_series", {}),
            "paper_cum_usd": round(st.get("cum_net_usd", 0.0), 3),
            "status": "OK"}
