"""W7 v3 — symmetric-favorite (FLB) probe on 15-minute up/down binaries.

Re-frozen 2026-08-31 (user decision; slot modified in place, v2 books
archived). v2 — favorite-NO only, band [0.50,0.98] — closed at 259 windows,
−2.76c, t −1.29, and the canonical rescan of the same forward days showed the
research method itself at −0.43c: the "up-side premium" tilt did not persist.
What persisted across BOTH study periods is the favourite-longshot bias, so
v3 buys WHICHEVER side is the favorite:
  * entry when ~8 minutes of the window remain (±1 min) — unchanged
  * band [0.60, 0.98] paid for the favorite; [0.50, 0.60) recorded as an
    OBSERVATION leg outside the books (the FLB's negative print — if that
    leg turns positive, the structure changed and everything re-opens)
  * TAKER book (the money truth): real prod book-walk VWAP + fee
  * MAKER parallel book (a measurement, not a mode): post at touch − 1c,
    zero fee, filled iff the tape shows the opposite side crossing the
    posted level before close − 45s. The 2026-08-31 backtest killed maker
    (−10.6c, t −5.3 — fills are adversely selected); this book is the
    forward confirmation, kept because it is free.
  * settlement = the venue's OFFICIAL result, never a model (W5's lesson)
  * VERDICT pre-registered on the PRIMARY CELL only: cost ∈ [0.85, 0.98],
    ≥ 300 independent primary windows AND mean > 0 AND window-clustered
    t ≥ 2.5. Other buckets are a MAP — judging any of them requires a new
    pre-registration (wide-band mean would need ~14,700 windows for t 2.5;
    it cannot be the bet).

Registered comparands (tape backtest 8/27-31): primary +2.99c t +2.36
(263 windows, ~61/day → verdict in ~5 days); wide band +0.54c t +0.42;
obs leg −7.21c; maker −10.58c t −5.30 on ~50% fills.

PAPER + demo mirror on ALL band entries (user 2026-08-31). Cadence 60s.
"""
from __future__ import annotations

import gzip
import json
import logging
import os

import pandas as pd

from crypto_trading.crypto_common.config import PRICE_DATA
from crypto_trading.crypto_strategies.event_binary.research_favorite_no import (
    COST_HI, COST_LO, MAKER_IMPROVE, MARKETS, OBS_LO, PRIMARY_HI, PRIMARY_LO,
    REM_MIN_TARGET, REM_TOLERANCE, favorite_side, fee, no_cost, yes_cost)

from . import common

logger = logging.getLogger(__name__)

NAME = "w7_noisefade"           # slot name kept: probe state/history continuity
STRIPS_ROOT = PRICE_DATA / "kalshi" / "event_strips" / "prod"

# Buying one side consumes the OTHER side's resting bids (single book):
# a resting YES bid at p is an offer of NO at 1−p, and vice versa.
LADDER_FOR_SIDE = {"no": "yes_dollars", "yes": "no_dollars"}


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


def tape_quotes(series: str, ticker: str, t0: float, t1: float) -> list[tuple]:
    """(recv_ts, yes_bid, yes_ask) rows for ``ticker`` in [t0, t1] from OUR
    recorded tape. Reads the UTC day files covering the range; yesterday's
    file may already be gzipped by the nightly compressor."""
    out = []
    days = {pd.Timestamp(t, unit="s", tz="UTC").strftime("%Y-%m-%d")
            for t in (t0, t1)}
    for day in sorted(days):
        base = STRIPS_ROOT / series / "markets" / f"{day}.jsonl"
        path = base if base.exists() else base.with_suffix(".jsonl.gz")
        try:
            if path.suffix == ".gz":
                with gzip.open(path, "rt", errors="ignore") as fh:
                    lines = fh.readlines()
            else:
                lines = _tail_lines(path, max_bytes=8_000_000)
        except OSError:
            continue
        for line in lines:
            try:
                snap = json.loads(line)
            except json.JSONDecodeError:
                continue
            rts = snap.get("recv_ts")
            if rts is None or not (t0 <= rts <= t1):
                continue
            for m in snap.get("markets", []):
                if m.get("ticker") != ticker:
                    continue
                try:
                    yb = float(m["yes_bid_dollars"])
                    ya = float(m["yes_ask_dollars"])
                except (KeyError, TypeError, ValueError):
                    continue
                if 0.0 < yb < 1.0 and 0.0 < ya < 1.0:
                    out.append((rts, yb, ya))
    return sorted(out)


def maker_filled(side: str, posted_yes_level: float, quotes: list[tuple]) -> bool:
    """Conservative fill proxy (same as the 2026-08-31 backtest, kept
    IDENTICAL so live and backtest numbers stay comparable): the resting
    order counts as filled only when the opposite side CROSSES the posted
    level — a NO-buyer's posted YES ask is crossed when a later yes_bid
    reaches it; a YES-buyer's posted YES bid is crossed when a later
    yes_ask falls to it. Quote-through without a cross is NOT a fill."""
    eps = 1e-9
    if side == "no":
        return any(yb >= posted_yes_level - eps for _, yb, _ in quotes)
    return any(ya <= posted_yes_level + eps for _, _, ya in quotes)


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


def walk_book(ticker: str, contracts: int, side: str) -> dict | None:
    """PROD book walk for a taker buying ``side`` — READ ONLY, honest fill.

    Kalshi runs ONE book. Buying NO consumes the ``yes_dollars`` ladder
    (resting YES bids) at cost 1 − p, best (highest) bid first; buying YES
    consumes the ``no_dollars`` ladder (resting NO bids) the same way. The
    v2 bug this generalises from: reading your OWN side's ladder prices you
    against your competition, not your counterparty (fixed 2026-08-27).
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
    for p_, sz in (ob.get(LADDER_FOR_SIDE[side]) or []):
        try:
            p_, sz = float(p_), float(sz)
        except (TypeError, ValueError):
            continue
        if 0.0 < p_ < 1.0 and sz > 0:
            ladder.append((p_, sz))
    if not ladder:
        return None
    ladder.sort(key=lambda x: -x[0])          # best (highest) opposite bid first
    need, paid, taken = float(contracts), 0.0, 0.0
    for p_, sz in ladder:
        take = min(need, sz)
        paid += take * (1.0 - p_)             # our side costs 1 − opposite bid
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
    actively REFUTING the edge — judged on the PRE-REGISTERED PRIMARY CELL:
    window-clustered t ≤ −2 with enough independent primary windows.
    Sticky like the old kill — a human clears it."""
    if st.get("killed"):
        return True
    n, mu, t = window_stats(st.get("windows_primary") or {})
    if n >= min_windows and t <= -2.0:
        st["killed"] = True
        st["killed_reason"] = (f"evidence: PRIMARY window t={t:.2f} "
                               f"(mean {mu:+.2f}c, n={n}) — data refutes the edge")
        return True
    return False


def _book_window(windows: dict, close: str, pnl_c: float) -> None:
    wc = windows.setdefault(close, {"n": 0, "wins": 0, "sum_c": 0.0})
    wc["n"] += 1
    wc["wins"] += int(pnl_c > 0)
    wc["sum_c"] = round(wc["sum_c"] + pnl_c, 2)


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
    for k in ("looked", "entered", "unresolved", "drift_skips", "obs_entered",
              "touch_fallbacks"):
        probe.setdefault(k, 0)
    st.setdefault("version", "v3_2026-08-31")
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
        side = p.get("side", "no")
        win = (res == side)
        pnl_c = ((1.0 if win else 0.0) - p["cost"] - fee(p["cost"])) * 100
        # MAKER parallel book: settle from the tape (posted at touch − 1c,
        # zero fee — maker-fee schedule unverified 2026-08-31, kalshi.com
        # 429'd; the comparison is dominated by selection, not the fee).
        mk_fill = None
        mk_pnl = None
        posted = p.get("maker_posted")
        # Fill window starts at the ORDER's wall-clock time (``opened``), NOT
        # the snapshot's recv_ts: the snapshot is up to 90s older than the
        # posting moment, and in a trending window the pre-entry quotes sit
        # on the wrong side of the posted level — caught live 2026-09-01
        # (ETH-2230: tape never crossed 0.73 after entry, yet the pre-entry
        # ask did → spurious "fill"). An order cannot fill before it exists.
        t0 = None
        if p.get("opened"):
            try:
                t0 = pd.Timestamp(p["opened"]).timestamp()
            except (ValueError, TypeError):
                t0 = None
        if t0 is None:
            t0 = p.get("entry_rts")
        if posted is not None and t0:
            qs = tape_quotes(p["series"], tkr, t0, ct.timestamp() - 45)
            mk_fill = maker_filled(side, posted, qs)
            if mk_fill:
                mk_cost = (1.0 - posted) if side == "no" else posted
                mk_pnl = round(((1.0 if win else 0.0) - mk_cost) * 100, 2)
        leg = p.get("leg", "band")
        row = {"ticker": tkr, "series": p.get("series"), "side": side,
               "leg": leg, "cost": p["cost"], "opened": p.get("opened"),
               "snap_age_s": p.get("snap_age_s"), "rem_min": p.get("rem_min"),
               "depth": p.get("depth"), "maker_fill": mk_fill,
               "maker_pnl_c": mk_pnl,
               "win": win, "pnl_c": round(pnl_c, 2), "closed": str(now)}
        if leg == "obs":
            # observation leg: recorded, never booked — the FLB's negative
            # print. If this leg's mean turns POSITIVE the structure changed.
            st.setdefault("obs_trades", []).append(row)
        else:
            st["cum_net_usd"] = st.get("cum_net_usd", 0.0) + pnl_c / 100 * contracts
            # INDEPENDENT WINDOWS (distinct close_time) are the honest
            # denominator — the five 15M markets settle one macro move
            # (measured 2026-08-27: nominal trades overstate 3-5x).
            _book_window(st.setdefault("windows", {}), p["close"], pnl_c)
            if PRIMARY_LO <= p["cost"] <= PRIMARY_HI:
                _book_window(st.setdefault("windows_primary", {}),
                             p["close"], pnl_c)
            sk = st.setdefault("by_series", {})
            b = sk.setdefault(p.get("series", "?"), {"n": 0, "wins": 0, "sum_c": 0.0})
            b["n"] += 1
            b["wins"] += int(win)
            b["sum_c"] = round(b["sum_c"] + pnl_c, 2)
            st.setdefault("trades", []).append(row)
        settled.append({"ticker": tkr, "leg": leg, "win": win,
                        "pnl_c": round(pnl_c, 2)})
        vpos.pop(tkr)

    # ── scan each market for a window at the entry point ──
    entries, per_series = [], {}
    for series in MARKETS:
        snap = latest_snapshot(series)
        if snap is None:
            per_series[series] = "NO_TAPE"
            continue
        age = now.timestamp() - snap["recv_ts"]
        # 90s = one tape cycle. Entering on an older quote is adverse
        # selection, not a signal (v2 lesson, recorded per entry).
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
            side = favorite_side((yb + ya) / 2.0)
            if side is None:
                continue                      # dead even — no favorite
            cost = no_cost(yb) if side == "no" else yes_cost(ya)
            if cost < OBS_LO or cost > COST_HI:
                continue
            leg = "band" if cost >= COST_LO else "obs"
            # read-only prod book; one retry — v2 marked 109/972 entries at
            # the snapshot touch because a single book fetch hiccuped, and
            # those averaged −6.75c vs −1.4c: book-unavailable moments are
            # NOT random. The touch fallback stays (it is what the registered
            # backtest priced at) but is counted so its share stays visible.
            bk = walk_book(tkr, contracts, side) or walk_book(tkr, contracts, side)
            # Mark the paper trade at the price a real 25-lot would actually
            # pay (book-walk VWAP), falling back to the touch when the book
            # is unavailable. Touch-price marking flatters thin books.
            if bk and bk.get("fill_cost") and not bk.get("shortfall"):
                # FAITHFULNESS GATE (2026-08-31 audit): the leg was chosen on
                # a snapshot up to ~90s old; the live book can have drifted
                # out of the frozen leg — v2 recorded 69/972 out-of-band
                # trades at −15.8c avg that were never the registered
                # strategy. Re-validate on the ACTUAL fill price and walk
                # away if it drifted out — what a real limit order would do.
                fc = bk["fill_cost"]
                fleg = ("band" if COST_LO <= fc <= COST_HI
                        else ("obs" if OBS_LO <= fc < COST_LO else None))
                if fleg != leg:
                    seen.append(tkr)               # one look per window
                    probe["drift_skips"] += 1
                    common.log_line(NAME, {"action": "drift_skip",
                                           "ticker": tkr, "side": side,
                                           "snap_cost": round(cost, 4),
                                           "book_cost": fc})
                    continue
                cost = fc
            else:
                probe["touch_fallbacks"] += 1
            # maker parallel book: post one cent inside the CURRENT touch —
            # the BOOK's touch when we have the book (the same quote that
            # priced the entry; the snapshot can be up to 90s older and the
            # market moves — caught live 2026-09-01: snapshot-posted levels
            # sat 16-21c away from the real touch). Snapshot only as fallback.
            if bk and bk.get("top_cost"):
                posted = ((1.0 - bk["top_cost"]) + MAKER_IMPROVE if side == "no"
                          else bk["top_cost"] - MAKER_IMPROVE)
            else:
                posted = (yb + MAKER_IMPROVE) if side == "no" else (ya - MAKER_IMPROVE)
            probe["entered" if leg == "band" else "obs_entered"] += 1
            seen.append(tkr)
            vpos[tkr] = {"cost": round(cost, 4), "close": ct, "series": series,
                         "side": side, "leg": leg,
                         "maker_posted": round(posted, 4),
                         "entry_rts": rts,
                         "rem_min": round(rem_min, 2), "snap_age_s": round(age),
                         "depth": bk, "opened": str(now)}
            entry = {"action": "paper_entry", "series": series, "ticker": tkr,
                     "side": side, "leg": leg, "cost": round(cost, 4),
                     "rem_min": round(rem_min, 2), "contracts": contracts,
                     "depth": bk}
            # demo mirror: ALL band entries, favorite side as-is (user
            # 2026-08-31: unified band, no demo special-casing).
            if leg == "band" and cfg.get("demo_mirror", False):
                from crypto_trading.crypto_common.execution_events import (
                    EventExecutionRouter)
                entry["demo_mirror"] = common.mirror_async(
                    NAME, EventExecutionRouter(strategy=NAME).mirror_demo,
                    side=side, close_time=ct, entry_price=cost,
                    contracts=contracts, series=(series, "KXBTC"))
            entries.append(entry)
            common.log_line(NAME, entry)

    if len(seen) > 3000:
        st["seen_tickers"] = seen[-1500:]
    common.save_state(NAME, st)
    npr, mpr, tpr = window_stats(st.get("windows_primary") or {})
    return {**rep, "entries": entries, "settled": settled,
            "markets": per_series, "open_virtual": len(vpos), "probe": probe,
            "by_series": st.get("by_series", {}),
            "independent_windows": len(st.get("windows", {})),
            "primary_windows": npr, "primary_mean_c": round(mpr, 2),
            "primary_t": round(tpr, 2),
            "paper_cum_usd": round(st.get("cum_net_usd", 0.0), 3),
            "status": "OK"}
