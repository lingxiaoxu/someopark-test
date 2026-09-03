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
    evaluated ONCE at 300 independent primary windows — pooled per-contract
    mean > 0 AND window-cluster-robust t ≥ 2.5. Other buckets are a MAP —
    judging any of them requires a new pre-registration (wide-band mean
    would need ~14,700 windows for t 2.5; it cannot be the bet).

v3.1 AMENDMENT (2026-09-02, after a 32-agent audit; books reset, v3.0
archived). Three defects made the accumulated sample incomparable to the
comparand, so it could not simply carry over:
  * the verdict statistic equal-weighted WINDOWS, but window size is
    informative (a five-coin window is one big macro move, and those lose):
    it read +2.24c where the book earned +0.48c per contract. Decisions now
    use pooled per-contract P&L with a window-cluster-robust SE — the money.
  * entry took the FIRST snapshot inside rem 8±1 rather than the CLOSEST to
    8.00 as the frozen rule and the comparand both specify, parking the
    forward book at a systematic rem≈8.65 the comparand never observed.
  * the drift gate DISCARDED any window whose price moved out of the guessed
    leg between signal and order — 19% of windows, excluded on information
    that postdates the signal and predicts the outcome. The fill price is now
    authoritative and the trade is booked in the leg it implies.

  * the ENTRY CLOCK was the tape's, and 90s divides the 900s window exactly,
    so the recorder's phase was locked: every entry landed at rem 7.69 during
    the backtest and 8.58 live. The edge decays across that gap (+2.28c at rem
    7.5-8.0 vs +0.64c at 8.5-9.0), so a recorder artifact was choosing how good
    the strategy looked. Timing now runs off the probe's own drifting ~60s
    cycle at 8.00±0.6, and side/price/leg all come from ONE live orderbook call
    at order time — which also retires the stale list quote the audit measured
    diverging >5c from the book in 23% of samples (worst 46c).

Registered comparand, re-derived under BOTH corrections from the same tape
(8/27-31, one observation per window, rem 8.00±0.6, no re-selection):
PRIMARY pooled +2.19c/contract, window-cluster-robust t +1.61 (535 trades /
216 windows). Wide band +1.19c t +0.89. For the record, the same data read the
old way — equal-weight windows at the tape's locked phase — said +2.99c t 2.36;
neither framing ever cleared t 2.5 in-sample. Obs leg −7.21c. Maker −10.58c
t −5.30 on ~50% fills.

CAVEAT kept in view: the comparand is still drawn from tape snapshots whose
phase sat at 7.64 median, while the live probe now precesses across the whole
±0.6 window. If the decay within that narrow band matters, the live book will
read slightly below the comparand for reasons that are sampling, not edge.

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


_CADENCE_CACHE: dict = {}
# The probe's own cycle is ~60s and drifts, so a +-0.6 acceptance window fires
# exactly once per 15-minute window (seen_tickers dedups the rare double) and
# centres entries on the registered 8.00 instead of the tape's locked phase.
REM_ENTRY_TOLERANCE = 0.6
# The tape only has to tell us a window EXISTS and when it closes; both are
# static, so staleness here is a recorder-health signal, not a pricing risk.
DISCOVERY_MAX_AGE_S = 420.0


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


def fetch_orderbook(ticker: str) -> dict | None:
    """READ-ONLY prod orderbook, one attempt plus one retry: book-unavailable
    moments are not random (v2 marked 109/972 entries at a stale touch because
    a single fetch hiccuped, and those averaged -6.75c vs -1.4c)."""
    import requests

    from crypto_trading.crypto_common.kalshi.enums import rest_base
    for _ in range(2):
        try:
            r = requests.get(rest_base("prod") + f"/markets/{ticker}/orderbook",
                             timeout=6,
                             headers={"User-Agent": "someopark-crypto/0.1"})
            if r.status_code == 200:
                return (r.json() or {}).get("orderbook_fp") or {}
        except requests.RequestException:
            continue
    return None


def walk_ladder(ob: dict, side: str, contracts: int) -> dict | None:
    """Size-weighted cost of taking ``contracts`` of ``side`` from ``ob``."""
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


def walk_book_both(ticker: str, contracts: int) -> dict | None:
    """One read-only orderbook fetch -> the touch on both sides AND the
    size-weighted cost of taking ``contracts`` on each.

    Returns {"yes_bid", "yes_ask", "no": {...}, "yes": {...}} where each side
    dict is the walk_book payload for buying that side. Doing both sides from
    ONE payload is what lets the entry decision — which side is the favorite,
    what it costs, which leg it lands in — be made entirely from live prices
    at order time, with no stale-quote guess to re-validate afterwards.
    """
    ob = fetch_orderbook(ticker)
    if ob is None:
        return None
    out = {}
    for side in ("no", "yes"):
        walked = walk_ladder(ob, side, contracts)
        if walked is None:
            return None
        out[side] = walked
    # best resting YES bid, and the YES ask implied by the best NO bid
    yb = round(1.0 - out["no"]["top_cost"], 4)
    ya = round(out["yes"]["top_cost"], 4)
    if not (0.0 < yb < 1.0 and 0.0 < ya < 1.0):
        return None
    out["yes_bid"], out["yes_ask"] = yb, ya
    return out


def window_stats(windows: dict) -> tuple[int, float, float]:
    """(n_windows, mean_c, t) equal-weighting each INDEPENDENT window.

    Kept because the v3 comparand was registered on it — but it is NOT the
    money: it answers "what does the average window do", and window size is
    informative here (a window where all five coins qualify is one big macro
    move, and those lose). Measured 2026-09-02 on the live primary book:
    equal-weight +2.24c vs pooled per-contract +0.48c, a 4.7x wedge. Use
    pooled_stats for any decision; show this one for continuity.
    """
    import math
    wm = [v["sum_c"] / v["n"] for v in windows.values() if v.get("n")]
    n = len(wm)
    if n < 2:
        return n, 0.0, 0.0
    mu = sum(wm) / n
    var = sum((x - mu) ** 2 for x in wm) / (n - 1)
    se = math.sqrt(var / n) if var > 0 else float("inf")
    return n, mu, (mu / se if se > 0 else 0.0)


def pooled_stats(windows: dict) -> tuple[int, int, float, float]:
    """(n_trades, n_windows, mean_c_per_contract, t) — THE MONEY, with a
    window-cluster-robust standard error.

    mean is P&L per contract actually booked; the SE clusters on close_time
    because the five 15M markets settle one macro move (nominal trades
    overstate evidence 3-5x, measured 2026-08-27). CRVE for a mean:
        mu = sum_g S_g / N,  SE = sqrt(G/(G-1) * sum_g (S_g - n_g*mu)^2) / N
    """
    import math
    ws = [(v["n"], v["sum_c"]) for v in windows.values() if v.get("n")]
    G = len(ws)
    N = sum(n for n, _ in ws)
    if G < 2 or N == 0:
        return N, G, 0.0, 0.0
    mu = sum(s for _, s in ws) / N
    meat = sum((s - n * mu) ** 2 for n, s in ws) * G / (G - 1)
    se = math.sqrt(meat) / N if meat > 0 else float("inf")
    return N, G, mu, (mu / se if se > 0 else 0.0)


def always_valid_bound(n_windows: int, rho: float = 300.0,
                       alpha: float = 0.05) -> float:
    """|t| threshold that stays valid under CONTINUOUS monitoring.

    The kill is re-tested every 60s cycle (~1,440 looks/day), so a fixed-n
    |t| >= 2 bar is not a 2.3% rule — bootstrapping the live book measured a
    6.2% false-stop rate under the null. This is the one-sided normal-mixture
    boundary (Howard et al., "Time-uniform Chernoff bounds"), whose crossing
    probability is <= alpha over ALL sample sizes at once:
        t_bound(n) = sqrt( ((n+rho)/n) * (2*ln(1/alpha) + ln((n+rho)/rho)) )
    rho is tuned to the sample size we expect to decide at (300 windows), so
    the bound is tightest there: ~3.66 at n=300, ~4.6 at n=30. Being stricter
    than the old -2 only means a dead probe runs a little longer — the safe
    direction for a kill, and the price of watching continuously.
    """
    import math
    if n_windows < 2:
        return float("inf")
    return math.sqrt(((n_windows + rho) / n_windows)
                     * (2 * math.log(1 / alpha) + math.log((n_windows + rho) / rho)))


def evidence_kill(st: dict, min_windows: int = 30) -> bool:
    """Stop only when the data actively REFUTES the edge, judged on the
    PRE-REGISTERED PRIMARY CELL with the money statistic (pooled per-contract,
    window-cluster-robust) against a continuous-monitoring boundary.
    Sticky — a human clears it. Paper probes burn no money, so a dollar stop
    only ever fires on noise; it did, twice, before this replaced it."""
    if st.get("killed"):
        return True
    n_tr, n_w, mu, t = pooled_stats(st.get("windows_primary") or {})
    if n_w >= min_windows and t <= -always_valid_bound(n_w):
        st["killed"] = True
        st["killed_reason"] = (
            f"evidence: PRIMARY pooled t={t:.2f} <= -{always_valid_bound(n_w):.2f} "
            f"(mean {mu:+.2f}c/contract, {n_tr} trades in {n_w} windows) "
            f"— data refutes the edge")
        return True
    return False


VERDICT_WINDOWS = 300           # pre-registered sample size


def latch_verdict(st: dict) -> dict | None:
    """Evaluate the pre-registered gate ONCE, at n = VERDICT_WINDOWS, and
    freeze the answer into the state.

    A fixed-n rule read continuously is not a fixed-n rule: the gate was
    being recomputed every 60s cycle and shown on demand, which turns
    "t >= 2.5" into an optional-stopping rule with a measured ~6.2% type-I
    rate instead of the advertised 0.6%. Latching at the pre-registered size
    restores the guarantee — after the latch the numbers keep updating for
    the record, but THE decision never re-opens.
    """
    if st.get("verdict"):
        return st["verdict"]
    n_tr, n_w, mu, t = pooled_stats(st.get("windows_primary") or {})
    if n_w < VERDICT_WINDOWS:
        return None
    passed = mu > 0 and t >= 2.5
    n_eq, mu_eq, t_eq = window_stats(st.get("windows_primary") or {})
    st["verdict"] = {
        "passed": passed,
        "decided_at_windows": n_w,
        "trades": n_tr,
        "pooled_mean_c": round(mu, 3),
        "pooled_t_clustered": round(t, 3),
        "equalweight_mean_c": round(mu_eq, 3),
        "equalweight_t": round(t_eq, 3),
        "rule": (f"pooled per-contract mean > 0 AND window-clustered "
                 f"t >= 2.5 at {VERDICT_WINDOWS} primary windows"),
    }
    return st["verdict"]


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
    for k in ("looked", "entered", "unresolved", "obs_entered",
              "touch_fallbacks", "book_unavailable"):
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
        # The tape is used only to DISCOVER which windows exist and when they
        # close — both static facts — so minutes of age are harmless here and a
        # 429 hole no longer blocks entries. The DECISION price comes from a
        # live orderbook call at order time (below): batch /markets quotes
        # diverged from the book by >5c in 23% of samples, worst 46c, so they
        # were never fit to price a trade (2026-09-02 audit).
        if age > DISCOVERY_MAX_AGE_S:
            per_series[series] = f"STALE {age:.0f}s"
            continue
        per_series[series] = "scanned"
        for m in snap.get("markets", []):
            tkr, ct = m.get("ticker"), m.get("close_time")
            if not tkr or not ct or tkr in vpos or tkr in seen:
                continue
            # OUR OWN CLOCK, not the tape's. 90s divides the 900s window
            # exactly, so the recorder's phase is locked and every entry landed
            # at one offset: 7.69 min through the backtest, 8.58 min live. The
            # edge decays across that gap (+2.28c at rem 7.5-8.0 vs +0.64c at
            # 8.5-9.0, measured 2026-09-02), so the phase was silently choosing
            # how good the strategy looked. Timing off the probe's own drifting
            # ~60s cycle centres entries on the registered 8.00 and lets the
            # residual phase precess instead of sticking.
            rem_min = (pd.Timestamp(ct).timestamp() - now.timestamp()) / 60.0
            if abs(rem_min - REM_MIN_TARGET) > REM_ENTRY_TOLERANCE:
                continue
            probe["looked"] += 1
            # One call, both ladders: price AND side come from the live book,
            # so there is no stale guess left to re-validate — and with it goes
            # the drift gate that used to discard 19% of windows on a price
            # move that postdates the signal.
            bk_full = walk_book_both(tkr, contracts)
            if bk_full is None:
                probe["book_unavailable"] += 1
                continue
            yb, ya = bk_full["yes_bid"], bk_full["yes_ask"]
            side = favorite_side((yb + ya) / 2.0)
            if side is None:
                continue                      # dead even — no favorite
            bk = bk_full[side]
            cost = bk.get("fill_cost") or bk.get("top_cost")
            if cost is None or cost < OBS_LO or cost > COST_HI:
                continue
            leg = "band" if cost >= COST_LO else "obs"
            if not bk.get("fill_cost") or bk.get("shortfall"):
                # 25 lots do not fit at any price: the mark falls back to the
                # touch, which is what the registered backtest priced at.
                # Counted, because book-thin moments are not random.
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
                         "entry_rts": now.timestamp(),
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
    verdict = latch_verdict(st)
    common.save_state(NAME, st)
    npr, mpr, tpr = window_stats(st.get("windows_primary") or {})
    ntr, nw, mp, tp = pooled_stats(st.get("windows_primary") or {})
    return {**rep, "entries": entries, "settled": settled,
            "markets": per_series, "open_virtual": len(vpos), "probe": probe,
            "by_series": st.get("by_series", {}),
            "independent_windows": len(st.get("windows", {})),
            "primary_windows": npr,
            "primary_mean_c": round(mp, 2), "primary_t": round(tp, 2),
            "primary_equalweight_mean_c": round(mpr, 2),
            "primary_equalweight_t": round(tpr, 2),
            "verdict": verdict,
            "paper_cum_usd": round(st.get("cum_net_usd", 0.0), 3),
            "status": "OK"}
