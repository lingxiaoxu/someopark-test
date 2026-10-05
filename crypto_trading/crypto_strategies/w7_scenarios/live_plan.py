"""Live W7 entry plan from the 4-hourly scenario tables (user directive 2026-10-02).

For every 15M window and coin, at each entry bin (T-9.75, T-8.25, T-6.75, T-5.25)
the scenario is classified with the SAME definitions and code the tables
are built from, using only data available at that moment, and the latest table
decides whether to buy the favourite there and at what size:

  top1: the coin's own best bin (pooled 5-coin row if the coin's row is empty);
        entries only between T-9.75 and T-5.25 (T-11.25 -> T-9.75, T-3.75 -> T-5.25);
        skipped if the chosen bin loses;
        x1.5 if its mean >= P90 of every coin-table cell, x0.5 if <= P10 of the
        coins' traded picks, else x1.
  top2: the next distinct bin of the same table, traded only if its mean > 0;
        x1, or x0.5 if <= the same P10; never x1.5.
  2026-10-05 (user directive, W7 prod + W11 + W13): no x1.5 sizing - decide() caps the
        multiplier at MAX_SIZE_MULT = 1.0 (x0.5 kept); the table's own value is
        returned as mult_table for the audit.

The live W7 prod rules still apply on top (MAIN band, day flow gate, day/night
sizes, NO-after-dump, +1c limit) - they live in EventExecutionRouter.submit.

Fallback (user): if the table is missing/stale/failed or the scenario cannot be
classified, the T-8.25 bin trades exactly as W7 did before (x1) and the other
bins do nothing. This module never sends orders; it only decides.
"""
from __future__ import annotations

import gzip
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import inputs, scenarios, derived
from . import funding as fundmod
from .config import COINS, OUT, STRIPS, HL, POOLED
from .tables import COLS

BIN_CENTRES = {"T-9.75": 9.75, "T-8.25": 8.25, "T-6.75": 6.75, "T-5.25": 5.25}   # tradeable bins only
LIVE_BIN = "T-8.25"
MAX_SIZE_MULT = 1.0               # user 2026-10-05: no x1.5 in W7 prod / W11 / W13 (x0.5 kept)
BIN_TOLERANCE_MIN = 0.6           # same acceptance window W7 uses around its 8.00 entry
MAX_TABLE_AGE_S = 6 * 3600        # a 4-hourly table older than this counts as stale
FUNDING_REFRESH_AFTER_S = 75 * 60
HISTORY_DAYS = 9                  # 7-day vol median needs >= 2 days; same 9 the pipeline loads

_LOCK = threading.RLock()
_PLAN = {"sig": None, "plan": None}
_FEATS: dict = {}                 # close_ts -> per-coin window features
_FUND = {"df": None, "sig": None, "tried": 0.0}


# ── the table → per coin/scenario slots ────────────────────────────────────
def rules_from(tj: dict) -> dict:
    """{coin: {scenario: [slot, ...]}}; slot = {slot, pick, mult, v}. Both slots come from
    derived.best_times (top1 = pick/mult when action is trade; top2 = its 'top2' field)."""
    dj = derived.best_times(tj)
    R: dict = {}
    for co in COINS:
        R[co] = {}
        for sc, r in dj[co].items():
            if sc.startswith("_"):
                continue
            slots = []
            if r["action"] == "trade":
                slots.append(dict(slot="top1", pick=r["pick"], mult=float(r["mult"]), v=r["pick_cell"]["mean"]))
            t2 = r.get("top2")
            if t2:
                slots.append(dict(slot="top2", pick=t2["pick"], mult=float(t2["mult"]), v=t2["cell"]["mean"]))
            for x in slots:
                if x["pick"] not in derived.ALLOWED:
                    raise ValueError(f"pick outside the allowed bins: {co}/{sc} {x}")
            R[co][sc] = slots
    return R


def load_plan(now: float | None = None) -> dict:
    """{'ok', 'reason', 'built_ts', 'run', 'rules'}; ok=False means 'use the fallback'."""
    now = now or time.time()
    latest = OUT / "latest"
    try:
        sig = tuple((p.stat().st_mtime_ns, p.stat().st_size) for p in
                    (latest / "tables.json", latest / "meta.json", OUT / "status.json"))
    except OSError:
        return {"ok": False, "reason": "table_missing"}
    with _LOCK:
        if _PLAN["sig"] != sig:
            try:
                meta = json.loads((latest / "meta.json").read_text())
                status = json.loads((OUT / "status.json").read_text())
                rules = rules_from(json.loads((latest / "tables.json").read_text()))
                _PLAN["plan"] = {"built_ts": float(meta["built_ts"]), "run": status.get("run"),
                                 "status_ok": bool(status.get("ok")), "rules": rules}
            except Exception as e:                                    # noqa: BLE001
                _PLAN["plan"] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
            _PLAN["sig"] = sig
        p = _PLAN["plan"]
    if "error" in p:
        return {"ok": False, "reason": "table_unreadable", "detail": p["error"]}
    age = now - p["built_ts"]
    if age > MAX_TABLE_AGE_S:
        return {"ok": False, "reason": "table_stale", "age_s": round(age)}
    if not p["status_ok"]:
        # a failed rebuild keeps the previous checked table in latest/; still stale-checked above
        pass
    return {"ok": True, "built_ts": p["built_ts"], "run": p["run"], "age_s": round(age), "rules": p["rules"]}


# ── window features known at window open (trend60, 2h vol ratio, funding) ─────
def _strikes_live(coin: str, day: str) -> list[tuple[int, float]]:
    """Light reader for the live (not yet rotated) strips file: close_ts and strike only."""
    out = []
    src = inputs._source(STRIPS / f"KX{coin}15M" / "orderbook" / day)
    if src is None:
        return out
    opener = gzip.open if src.suffix == ".gz" else open
    with opener(src, "rt") as fh:
        for line in fh:
            try:
                r = json.loads(line)
                ct, k = r.get("close_time"), r.get("strike")
                if ct and k is not None:
                    out.append((int(round(datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp())), float(k)))
            except (ValueError, TypeError):
                continue
    return out


def window_features(close_ts: int, now: float | None = None) -> dict:
    """{coin: {'mkt_trend60', 'mkt_vr2'}} for the window closing at close_ts (cached per window)."""
    now = now or time.time()
    with _LOCK:
        if close_ts in _FEATS:
            return _FEATS[close_ts]
    days = [d.strftime("%Y-%m-%d") for d in pd.date_range(end=pd.Timestamp(now, unit="s").normalize(),
                                                           periods=HISTORY_DAYS + 1)]
    rows = []
    for c in COINS:
        for d in days:
            src = inputs._source(STRIPS / f"KX{c}15M" / "orderbook" / d)
            if src is None:
                continue
            if src.suffix != ".gz":                     # live / not yet rotated: strikes only, no ladders
                rows += [dict(coin=c, close_ts=ct, strike=k) for ct, k in _strikes_live(c, d)]
            else:                                         # rotated: the pipeline's cached parse
                s = inputs.strips_day(c, d)
                if len(s):
                    rows += s.groupby("close_ts", as_index=False)["strike"].first().assign(coin=c).to_dict("records")
    w = pd.DataFrame(rows)
    out = {}
    if len(w):
        w = w[w.close_ts <= close_ts].groupby(["coin", "close_ts"], as_index=False)["strike"].first()
        f = inputs.strike_features(w)
        cur = f[f.close_ts == close_ts]
        out = {r.coin: {"mkt_trend60": r.mkt_trend60, "mkt_vr2": r.mkt_vr2} for r in cur.itertuples()}
    if len(out) == len(COINS):                          # cache only once every coin's strike is in
        with _LOCK:
            _FEATS[close_ts] = out
            for k in [k for k in _FEATS if k < close_ts - 3600]:
                del _FEATS[k]
    return out


def maybe_refresh_funding(now: float | None = None) -> None:
    """Keep the shared HL funding cache current (the table pipeline refreshes it only
    every 4h). The HTTP work runs in a daemon thread: a slow/limited HL API must never
    stall the runner loop (W1-W7 and the scanner share it)."""
    now = now or time.time()
    try:
        df = fundmod.load()
        latest = df.time_ms.max() / 1000 if len(df) else 0
    except Exception:                                                  # noqa: BLE001
        latest = 0
    t = _FUND.get("thread")
    if now - latest < FUNDING_REFRESH_AFTER_S or now - _FUND["tried"] < 600 or (t is not None and t.is_alive()):
        return
    _FUND["tried"] = now

    def _run():
        try:
            fundmod.refresh()
        except Exception:                                              # noqa: BLE001
            pass
    _FUND["thread"] = threading.Thread(target=_run, name="w7-funding-refresh", daemon=True)
    _FUND["thread"].start()


def dev8h_at_open(coin: str, close_ts: int) -> float | None:
    try:
        sig = fundmod.FUNDING_CACHE.stat().st_mtime_ns
        with _LOCK:
            if _FUND["sig"] != sig:
                _FUND["df"], _FUND["sig"] = fundmod.load(), sig
            fu = _FUND["df"]
        open_ts = close_ts - 900
        g = fu[(fu.coin == coin) & (fu.time_ms / 1000.0 <= open_ts)]
        if g.empty or open_ts - g.time_ms.max() / 1000.0 > 2 * 3600:
            return None                                   # no settled hour close enough: cannot classify
        v = inputs.funding_at_open(pd.DataFrame({"coin": [coin], "close_ts": [close_ts]}), fu).iloc[0]
        return None if pd.isna(v) else float(v)
    except Exception:                                                  # noqa: BLE001
        return None


def flow_at(coin: str, now: float) -> dict:
    """The live flow gate's own feature code on the HL recorder tails (night included)."""
    from crypto_trading.crypto_common.execution_events import EventExecutionRouter as R
    from crypto_trading.crypto_strategies.downside_paper.features import compute_features
    day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
    try:
        f = compute_features(R._tail_rows(HL / "context" / coin / f"{day}.jsonl", 96_000),
                             R._tail_rows(HL / "book" / coin / f"{day}.jsonl", 1_500_000),
                             R._tail_rows(HL / "trades" / coin / f"{day}.jsonl", 2_500_000), now)
    except Exception:                                                  # noqa: BLE001
        return {"flow_valid": False, "flow_1m": np.nan, "mom_1m_bp": np.nan}
    return {"flow_valid": bool(f.get("flow_valid")), "flow_1m": f.get("observed_flow_imbalance_1m", np.nan),
            "mom_1m_bp": f.get("momentum_1m_bp", np.nan)}


def classify(coin: str, close_ts: int, side: str, now: float | None = None) -> dict:
    """{'scenario': str|None, 'inputs': {...}}; None = cannot classify (use the fallback)."""
    now = now or time.time()
    wf = window_features(close_ts, now).get(coin) or {}
    row = {"fav": side, "mkt_trend60": wf.get("mkt_trend60"), "mkt_vr2": wf.get("mkt_vr2"),
           "dev8h": dev8h_at_open(coin, close_ts), **flow_at(coin, now)}
    missing = [k for k in ("mkt_trend60", "mkt_vr2", "dev8h") if row[k] is None or pd.isna(row[k])]
    if missing:
        return {"scenario": None, "missing": missing, "inputs": row}
    sc = scenarios.label(pd.DataFrame([row])).iloc[0]
    return {"scenario": sc, "inputs": {k: (None if isinstance(v, float) and np.isnan(v) else v) for k, v in row.items()}}


def close_ts_from_ticker(ticker: str, now: float | None = None) -> int:
    """KX<COIN>15M-26OCT021945-45 -> close time (the date/time part is US Eastern).
    In the DST fall-back hour the stamp is ambiguous: take the instant nearest to now."""
    from zoneinfo import ZoneInfo
    now = now or time.time()
    stamp = ticker.split("-")[1]                                  # 26OCT021945
    naive = datetime.strptime(stamp, "%y%b%d%H%M")
    cands = {int(naive.replace(tzinfo=ZoneInfo("America/New_York"), fold=f).timestamp()) for f in (0, 1)}
    return min(cands, key=lambda t: abs(t - now))


# ── one top1 and one top2 per coin-window (same as the backtest) ──────────────
# The backtest marks a slot used once it trades and never reuses it in that
# window, even if the scenario (and with it the table row) changes between
# bins. Shared by the T-8 call (W7's thread) and the scanner, persisted so a
# restart cannot re-fire a slot.
SLOTS_FILE = Path(__file__).resolve().parents[2] / "trading_signals" / "live_watch" / "w7_table_slots.json"


_LEDGER: dict = {"rows": None}          # in-process copy; authoritative once loaded (file = persistence)


def _parse(raw: dict) -> dict:
    out = {}
    for tk, v in raw.items():
        if isinstance(v, dict) and "slots" in v:
            out[tk] = v
        elif isinstance(v, dict):                                  # pre-2026-10-02T22 layout {slot: ts}
            out[tk] = {"slots": dict(v), "filled": 0.0, "ts": max(v.values()) if v else 0.0}
    return out


def _rows() -> dict:
    """Load the ledger file once per process. A missing file is an empty ledger; a file
    that exists but cannot be read is logged and the (empty) in-process copy is used from
    then on - the file never silently resets what this process already knows."""
    if _LEDGER["rows"] is None:
        rows = {}
        if SLOTS_FILE.exists():
            try:
                rows = _parse(json.loads(SLOTS_FILE.read_text()))
            except (OSError, ValueError, AttributeError) as e:
                import logging
                logging.getLogger(__name__).warning("w7 table ledger unreadable (%s); starting empty in memory", str(e)[:80])
        _LEDGER["rows"] = rows
    return _LEDGER["rows"]


def _persist(now: float) -> None:
    """Best effort; a failed write (full disk) leaves the in-process ledger in force."""
    d = _LEDGER["rows"]
    for k in [k for k, v in d.items() if now - float(v.get("ts") or 0) >= 3 * 3600]:
        del d[k]
    try:
        tmp = SLOTS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(d))
        tmp.replace(SLOTS_FILE)
    except OSError as e:
        import logging
        logging.getLogger(__name__).warning("w7 table ledger not persisted (%s); in-memory copy stays in force", str(e)[:80])


def reset_ledger() -> None:
    """Forget the in-process copy (tests, or after the file is replaced by hand)."""
    with _LOCK:
        _LEDGER["rows"] = None


def _row(ticker: str, now: float) -> dict:
    return _rows().setdefault(ticker, {"slots": {}, "filled": 0.0, "ts": now})


def used_slots(ticker: str) -> set:
    with _LOCK:
        return set(_rows().get(ticker, {}).get("slots", {}))


def claim_slot(ticker: str, slot: str, now: float | None = None) -> bool:
    """Atomically mark `slot` used for this ticker; False if it already was."""
    now = now or time.time()
    with _LOCK:
        row = _row(ticker, now)
        if slot in row["slots"]:
            return False
        row["slots"][slot] = now
        row["ts"] = now
        _persist(now)
        return True


def exposure(ticker: str) -> float:
    """Contracts bought or currently reserved on this ticker this window."""
    with _LOCK:
        return float(_rows().get(ticker, {}).get("filled", 0.0))


def reserve(ticker: str, want: int, cap: int, now: float | None = None) -> tuple[int, float]:
    """Atomically reserve up to `want` contracts under `cap` for this ticker.
    Returns (granted, already_before). The reservation counts as exposure until
    `settle` replaces it with what was actually bought (top1 + top2 <= cap rule)."""
    now = now or time.time()
    with _LOCK:
        row = _row(ticker, now)
        already = float(row.get("filled", 0.0))
        granted = int(max(0.0, min(float(want), cap - already)))
        if granted > 0:
            row["filled"] = already + granted
            row["ts"] = now
            _persist(now)
        return granted, already


def settle(ticker: str, reserved: int, bought: float, now: float | None = None) -> float:
    """Replace a reservation with the real fill (bought may be 0 for a rejected order).
    Returns the ticker's exposure after settlement."""
    now = now or time.time()
    with _LOCK:
        row = _row(ticker, now)
        row["filled"] = max(0.0, float(row.get("filled", 0.0)) - float(reserved) + float(bought))
        row["ts"] = now
        _persist(now)
        return row["filled"]


def record_exposure(ticker: str, contracts: float, now: float | None = None) -> float:
    """Add contracts bought outside the reserve/settle pair; returns the new total."""
    return settle(ticker, 0, contracts, now)


BIN_EARLY_MIN = 0.1               # scanner fires on the first tick at <= centre + 6 s (ticks are ~10 s apart)


def bin_due(rem_min: float) -> str | None:
    """Scanner trigger: the first tick at or just before the bin centre, so live
    entries sit within ~+-6 s of it like the backtest's nearest-to-centre snapshot
    (2026-10-02: the +-0.6 window fired ~30 s early). A tick delayed past the
    centre may still fire up to 0.6 min late."""
    for b, c in BIN_CENTRES.items():
        if c - BIN_TOLERANCE_MIN <= rem_min <= c + BIN_EARLY_MIN:
            return b
    return None


def bin_for(rem_min: float) -> str | None:
    for b, c in BIN_CENTRES.items():
        if abs(rem_min - c) <= BIN_TOLERANCE_MIN:
            return b
    return None


def decide(coin: str, close_ts: int, bin_label: str, side: str, now: float | None = None,
           ticker: str | None = None) -> dict:
    """What the table says for this coin/window/bin/favourite right now.

    action: 'trade' (with mult and slot) | 'skip' (table says not here) | 'fallback'.
    """
    now = now or time.time()
    plan = load_plan(now)
    audit = {"bin": bin_label}
    if not plan["ok"]:
        return {"action": "fallback", "reason": plan["reason"], **audit}
    audit.update(table_run=plan["run"], table_age_s=plan["age_s"])
    cls = classify(coin, close_ts, side, now)
    if cls["scenario"] is None:
        return {"action": "fallback", "reason": "scenario_unclassified", "missing": cls["missing"], **audit}
    slots = plan["rules"].get(coin, {}).get(cls["scenario"], [])
    audit.update(scenario=cls["scenario"], slots=[f"{s['slot']}:{s['pick']}x{s['mult']:g}" for s in slots],
                 inputs=cls["inputs"])
    s = next((s for s in slots if s["pick"] == bin_label), None)
    if s is None:
        return {"action": "skip", "reason": "table_not_this_bin", **audit}
    if ticker and s["slot"] in used_slots(ticker):
        return {"action": "skip", "reason": f"{s['slot']}_already_used_this_window", "slot": s["slot"], **audit}
    return {"action": "trade", "slot": s["slot"], "mult": min(float(s["mult"]), MAX_SIZE_MULT),
            "mult_table": float(s["mult"]), "cell_mean_c": s["v"], **audit}
