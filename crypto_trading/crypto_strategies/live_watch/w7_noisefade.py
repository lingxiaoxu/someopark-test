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
post-registration INDEPENDENT WINDOWS ≥ 300 (raised from 200 on 2026-08-29:
the realised window σ of 33.7c makes the 200-window test unreachable even
for a true +3.3c edge — power correction using σ only, per user approval)
AND mean > 0 AND window-clustered t ≥ 2.5. The
bar exceeds the usual 2.0 because the cell surfaced from an exploratory scan
(24 cells, Bonferroni |t| ≥ 3.08, which it did NOT clear on scan data).

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


def walk_book_for_no(ticker: str, contracts: int) -> dict | None:
    """PROD book walk for a NO buyer — READ ONLY, and the honest fill price.

    Kalshi runs ONE book: a resting YES bid at p IS an offer of NO at 1−p.
    So buying NO consumes the ``yes_dollars`` ladder from the HIGHEST yes bid
    downward (highest yes bid = cheapest NO). The first version of this
    helper read ``no_dollars`` — that ladder is other NO *buyers*, i.e. our
    competition, not our counterparty. Fixed 2026-08-27.

    Returns top-of-book cost, the size-weighted cost of actually filling
    ``contracts``, and how deep we had to reach — so paper P&L can be marked
    at a fill price a real order would get instead of the touch.
    """
    import requests

    from crypto_trading.crypto_common.kalshi.enums import rest_base
    try:
        r = requests.get(rest_base("prod") + f"/markets/{ticker}/orderbook",
                         timeout=6, headers={"User-Agent": "someopark-crypto/0.1"})
        if r.status_code != 200:
            return None
        ob = (r.json() or {}).get("orderbook_fp") or {}
    except requests.RequestException:
        return None
    ladder = []
    for p_, sz in (ob.get("yes_dollars") or []):
        try:
            p_, sz = float(p_), float(sz)
        except (TypeError, ValueError):
            continue
        if 0.0 < p_ < 1.0 and sz > 0:
            ladder.append((p_, sz))
    if not ladder:
        return None
    ladder.sort(key=lambda x: -x[0])          # best (highest) yes bid first
    need, paid, taken = float(contracts), 0.0, 0.0
    for p_, sz in ladder:
        take = min(need, sz)
        paid += take * (1.0 - p_)             # NO costs 1 − yes price
        taken += take
        need -= take
        if need <= 0:
            break
    top_cost = round(1.0 - ladder[0][0], 4)
    if taken <= 0:
        return {"top_cost": top_cost, "filled": 0, "levels": len(ladder)}
    return {"top_cost": top_cost,
            "fill_cost": round(paid / taken, 4),      # size-weighted, realistic
            "filled": round(taken),
            "shortfall": round(max(need, 0.0)),       # unfillable at any price
            "slippage_c": round((paid / taken - (1.0 - ladder[0][0])) * 100, 2),
            "levels": len(ladder),
            "top_size": round(ladder[0][1])}


def quote(m: dict) -> tuple[float, float] | None:
    """(yes_bid, yes_ask) from a snapshot market row (dollars-as-strings)."""
    try:
        yb = float(m.get("yes_bid_dollars"))
        ya = float(m.get("yes_ask_dollars"))
    except (TypeError, ValueError):
        return None
    return (yb, ya) if 0.0 < yb < 1.0 and 0.0 < ya < 1.0 else None


def window_stats(windows: dict) -> tuple[int, float, float]:
    """(n, mean_c, t) over INDEPENDENT windows — the strategy's honest stats."""
    import math
    wm = [v["sum_c"] / v["n"] for v in windows.values() if v.get("n")]
    n = len(wm)
    if n < 2:
        return n, 0.0, 0.0
    mu = sum(wm) / n
    var = sum((x - mu) ** 2 for x in wm) / (n - 1)
    se = math.sqrt(var / n) if var > 0 else float("inf")
    return n, mu, (mu / se if se > 0 else 0.0)


def evidence_kill(st: dict, min_windows: int = 30) -> bool:
    """Paper probes burn no money, so a dollar stop only ever fires on noise
    (it did, twice). The only honest reason to stop a paper probe is the data
    actively REFUTING the edge: window-clustered t ≤ −2 with enough
    independent windows. Sticky like the old kill — a human clears it."""
    if st.get("killed"):
        return True
    n, mu, t = window_stats(st.get("windows") or {})
    if n >= min_windows and t <= -2.0:
        st["killed"] = True
        st["killed_reason"] = (f"evidence: window t={t:.2f} (mean {mu:+.2f}c, "
                               f"n={n}) — data refutes the edge")
        return True
    return False


def run(cfg: dict | None = None, **_) -> dict:
    cfg = (cfg or common.load_cfg()).get(NAME, {})
    st = common.load_state(NAME)
    if evidence_kill(st):
        common.save_state(NAME, st)
        return {"strategy": NAME, "status": "KILLED",
                "reason": st.get("killed_reason")}
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
        # CROSS-COIN CORRELATION (measured 2026-08-27): the five 15M markets
        # settle the same macro move — one 12:30 window produced three
        # identical "yes" results. Nominal trade count therefore overstates
        # evidence by 3-5x; track INDEPENDENT WINDOWS (distinct close_time)
        # as the honest denominator for any t-statistic.
        wins_by_close = st.setdefault("windows", {})
        wc = wins_by_close.setdefault(p["close"], {"n": 0, "wins": 0, "sum_c": 0.0})
        wc["n"] += 1
        wc["wins"] += int(win)
        wc["sum_c"] = round(wc["sum_c"] + pnl_c, 2)
        sk = st.setdefault("by_series", {})
        b = sk.setdefault(p.get("series", "?"), {"n": 0, "wins": 0, "sum_c": 0.0})
        b["n"] += 1
        b["wins"] += int(win)
        b["sum_c"] = round(b["sum_c"] + pnl_c, 2)
        st.setdefault("trades", []).append(
            {"ticker": tkr, "series": p.get("series"), "cost": p["cost"],
             "snap_age_s": p.get("snap_age_s"), "rem_min": p.get("rem_min"),
             "depth": p.get("depth"),
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
        # 90s = one tape cycle. The old 180s allowed entering on a quote up to
        # 20% of the window old — that is adverse selection, not a signal: we
        # would only "enter" when the stale view still looked favourable while
        # the live market had already moved. Recorded per entry so the effect
        # stays measurable.
        if age > 90:
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
            bk = walk_book_for_no(tkr, contracts)    # read-only, prod book
            # Mark the paper trade at the price a real 25-lot would actually
            # pay (book-walk VWAP), falling back to the touch when the book
            # is unavailable. Touch-price marking flatters thin books.
            if bk and bk.get("fill_cost") and not bk.get("shortfall"):
                cost = bk["fill_cost"]
            depth = bk
            vpos[tkr] = {"cost": round(cost, 4), "close": ct, "series": series,
                         "rem_min": round(rem_min, 2), "snap_age_s": round(age),
                         "depth": depth, "opened": str(now)}
            entry = {"action": "paper_entry", "series": series, "ticker": tkr,
                     "side": "no", "cost": round(cost, 4),
                     "rem_min": round(rem_min, 2), "contracts": contracts,
                     "depth": depth}
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
            "independent_windows": len(st.get("windows", {})),
            "paper_cum_usd": round(st.get("cum_net_usd", 0.0), 3),
            "status": "OK"}
