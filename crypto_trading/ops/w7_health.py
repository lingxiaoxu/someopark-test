"""One-command health check for the W7 v3 probe and the recorders it depends on.

    ./pipeline.sh w7health          # human summary, exit 1 if anything FAILed
    ./pipeline.sh w7health --json   # machine form, for cron/alerting

WHY THIS EXISTS. During v3's first 24 hours four separate defects were found by
hand-running ad-hoc checks (out-of-band fills, a maker level derived from a
stale quote, a fill window that started before the order existed, and a demo
mirror that mapped 15-minute windows onto other markets). Every one of them was
caught by an invariant that was retyped from scratch each time. Retyped
invariants rot; this file is those checks, frozen, so "is anything wrong" is one
command and the answer is a number instead of an impression.

WHAT IT DOES NOT DO. It never writes, never restarts anything, never orders.
It is a read-only opinion about a running system, safe to run at any moment.

The checks, and what each one would have caught:
  daemon     — probe alive and cycling (a dead loop looks exactly like a quiet
               market from the state file alone)
  errors     — tracebacks since the last restart, sliced by LINE not timestamp
               (log lines without timestamps once fooled a timestamp filter)
  books      — the accounting identities: paper cum == sum of trades, window
               books == trade books, primary membership == the frozen cell,
               observation leg never touches the books, no trade outside its
               leg's band (this is the one that catches a drift-gate regression)
  criteria   — progress toward the pre-registered verdict, and whether the
               evidence kill has fired
  mirror     — demo mirror outcomes: 409s and wrong-window maps must be zero
  recorders  — every live stream's freshness, plus today's 15M tape cadence:
               the tape IS the probe's eyes, and it is irreplaceable
  backup     — how old the newest archive is: the tape had exactly one copy
               for 15 days, and the 15M dataset was born inside that gap
  disk       — headroom, because the recorders never stop
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from crypto_trading.crypto_common.config import PRICE_DATA, SIGNALS_DIR
from crypto_trading.crypto_strategies.event_binary.research_favorite_no import (
    COST_HI, COST_LO, MARKETS, OBS_LO, PRIMARY_HI, PRIMARY_LO)
from crypto_trading.crypto_strategies.live_watch.w7_noisefade import window_stats

ROOT = Path(__file__).resolve().parents[1]
WATCH_LOG = ROOT / "logs" / "watch.log"
STATE = SIGNALS_DIR / "live_watch" / "w7_noisefade_state.json"
LOG_DIR = SIGNALS_DIR / "live_watch"
STRIPS = PRICE_DATA / "kalshi" / "event_strips" / "prod"

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

# Cadences the runner is configured with; a stream quiet for much longer than
# its own period is the signal, so each gets its own tolerance.
TAPE_MAX_GAP_MIN = 4.0         # 90s recorder: 2-3 missed cycles. A hole inside
                               # the ~7-min maker span is a measurement outage,
                               # and 429 storms cluster (2026-09-02 audit), so a
                               # 15-min bar was too slack to see them.
INDEX_MAX_GAP_S = 120.0        # 5s recorder
CYCLE_MAX_AGE_S = 300.0        # 60s cadence


def _fmt(age_s: float) -> str:
    return f"{age_s/60:.1f}m" if age_s >= 60 else f"{age_s:.0f}s"


def _lines(path: Path):
    """Yield lines from a .jsonl or its rotated .jsonl.gz twin."""
    p = path if path.exists() else path.with_suffix(".jsonl.gz")
    if not p.exists():
        return
    if p.suffix == ".gz":
        with gzip.open(p, "rt", errors="ignore") as fh:
            yield from fh
    else:
        with open(p, errors="ignore") as fh:
            yield from fh


def check_daemon(now: float) -> dict:
    try:
        out = subprocess.run(["pgrep", "-f", "live_watch.runner"],
                             capture_output=True, text=True, timeout=10).stdout
        pids = [x for x in out.split() if x]
    except (OSError, subprocess.SubprocessError):
        pids = []
    last_age = None
    try:
        size = WATCH_LOG.stat().st_size
        with open(WATCH_LOG, "rb") as fh:
            fh.seek(max(0, size - 200_000))
            tail = fh.read().decode("utf-8", errors="ignore").splitlines()
        for line in reversed(tail):
            if "[w7]" in line:
                ts = time.mktime(time.strptime(line[:19], "%Y-%m-%d %H:%M:%S"))
                last_age = now - ts
                break
    except (OSError, ValueError):
        pass
    if not pids:
        return {"status": FAIL, "detail": "no live_watch.runner process"}
    if last_age is None:
        return {"status": FAIL, "detail": "no [w7] cycle found in log tail"}
    if last_age > CYCLE_MAX_AGE_S:
        return {"status": FAIL,
                "detail": f"last w7 cycle {_fmt(last_age)} ago (>{CYCLE_MAX_AGE_S:.0f}s)"}
    return {"status": PASS,
            "detail": f"{len(pids)} pid(s), last cycle {_fmt(last_age)} ago"}


def check_errors(lookback_lines: int = 4000) -> dict:
    """Tracebacks in the recent log. Sliced by LINE COUNT on purpose: log
    output includes untimestamped traceback bodies, and a timestamp filter
    silently mixes old exceptions into a 'recent' window (measured 2026-08-31,
    which briefly resurrected a 4-day-old KeyError as if it were live)."""
    try:
        with open(WATCH_LOG, errors="ignore") as fh:
            tail = fh.readlines()[-lookback_lines:]
    except OSError as e:
        return {"status": WARN, "detail": f"log unreadable: {e}"}
    hits = [ln.strip()[:120] for ln in tail
            if "Traceback" in ln or " ERROR " in ln]
    if hits:
        return {"status": FAIL,
                "detail": f"{len(hits)} error line(s) in last {lookback_lines}",
                "sample": hits[:3]}
    return {"status": PASS, "detail": f"0 errors in last {lookback_lines} lines"}


def check_books(st: dict, contracts: int = 25) -> dict:
    """The accounting identities. Any mismatch means a number somewhere on the
    scoreboard is not what the trades say — which is the only way a wrong
    verdict could ever be reached."""
    tr = st.get("trades") or []
    obs = st.get("obs_trades") or []
    pos = st.get("positions") or {}
    bad = []

    booked = sum(t["pnl_c"] for t in tr) * contracts / 100.0
    if abs(st.get("cum_net_usd", 0.0) - booked) > 0.05:
        bad.append(f"cum_net_usd {st.get('cum_net_usd', 0.0):.2f} != trades {booked:.2f}")

    wsum = sum(v["sum_c"] for v in (st.get("windows") or {}).values())
    tsum = sum(t["pnl_c"] for t in tr)
    if abs(wsum - tsum) > 0.5:
        bad.append(f"window book {wsum:.1f} != trade book {tsum:.1f}")

    prim_n = sum(v["n"] for v in (st.get("windows_primary") or {}).values())
    prim_t = sum(1 for t in tr if PRIMARY_LO <= t["cost"] <= PRIMARY_HI)
    if prim_n != prim_t:
        bad.append(f"primary window count {prim_n} != trades in cell {prim_t}")

    out_of_band = [t["ticker"] for t in tr
                   if not (COST_LO <= t["cost"] <= COST_HI)]
    if out_of_band:
        bad.append(f"{len(out_of_band)} booked trade(s) outside [{COST_LO},{COST_HI}]: "
                   f"{out_of_band[:3]}")

    out_of_obs = [t["ticker"] for t in obs
                  if not (OBS_LO <= t["cost"] < COST_LO)]
    if out_of_obs:
        bad.append(f"{len(out_of_obs)} observation trade(s) outside [{OBS_LO},{COST_LO})")

    obs_tickers = {t["ticker"] for t in obs}
    leaked = obs_tickers & {t["ticker"] for t in tr}
    if leaked:
        bad.append(f"observation leg leaked into the books: {sorted(leaked)[:3]}")

    seen, dupes = set(), []
    for t in tr + obs:
        if t["ticker"] in seen:
            dupes.append(t["ticker"])
        seen.add(t["ticker"])
    if dupes:
        bad.append(f"{len(dupes)} duplicate settled ticker(s): {dupes[:3]}")
    still_open = seen & set(pos)
    if still_open:
        bad.append(f"ticker(s) both settled and open: {sorted(still_open)[:3]}")

    if bad:
        return {"status": FAIL, "detail": "; ".join(bad)}
    return {"status": PASS,
            "detail": f"{len(tr)} booked + {len(obs)} observation trades, "
                      f"{len(pos)} open — all identities hold"}


def check_criteria(st: dict) -> dict:
    n, mu, t = window_stats(st.get("windows_primary") or {})
    nw, muw, _ = window_stats(st.get("windows") or {})
    if st.get("killed"):
        return {"status": WARN, "detail": f"KILLED — {st.get('killed_reason', '?')}",
                "primary_windows": n, "primary_mean_c": round(mu, 2), "primary_t": round(t, 2)}
    return {"status": PASS,
            "detail": f"primary {n}/300 windows, mean {mu:+.2f}c, t {t:+.2f} "
                      f"| wide band {nw} windows {muw:+.2f}c",
            "primary_windows": n, "primary_mean_c": round(mu, 2),
            "primary_t": round(t, 2), "wide_windows": nw}


def check_mirror(now: float, hours: float = 6.0) -> dict:
    """Demo mirror outcomes. 409 (market closed) and a mapped close that is not
    the prod close both mean the mirror bet on a DIFFERENT market than the paper
    book — the defect fixed 2026-09-01. Both must stay at zero."""
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - hours * 3600))
    counts, codes, wrong_window = {}, {}, 0
    for f in sorted(glob.glob(str(LOG_DIR / "log_*.jsonl")))[-3:]:
        for line in _lines(Path(f)):
            try:
                j = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (j.get("strategy") != "w7_noisefade"
                    or j.get("action") != "demo_mirror_result"
                    or j.get("ts", "") < cutoff):
                continue
            counts[j.get("status")] = counts.get(j.get("status"), 0) + 1
            if j.get("status") == "sent":
                c = j.get("status_code")
                codes[c] = codes.get(c, 0) + 1
                if j.get("mapped_close") and j.get("mapped_close") != j.get("prod_close"):
                    wrong_window += 1
    if not counts:
        return {"status": WARN, "detail": f"no mirror attempts in {hours:.0f}h"}
    # Two different things wear the same red light unless we separate them:
    # a 409/400/404 or a mapped_close that is not ours means WE aimed at the
    # wrong market (the defect fixed 2026-09-01, must stay at zero), while a
    # 5xx/429 is the venue having a moment and says nothing about our code.
    # Calling a single transient FAIL trains the reader to ignore the check.
    ours = {k: v for k, v in codes.items() if k in (400, 403, 404, 409)}
    transient = {k: v for k, v in codes.items()
                 if k not in (200, 201) and k not in ours}
    sent = sum(codes.values()) or 1
    bad, warn = [], []
    if ours:
        bad += [f"{v}x HTTP {k} (our request)" for k, v in ours.items()]
    if wrong_window:
        bad.append(f"{wrong_window} mapped to a different close")
    if transient:
        share = sum(transient.values()) / sent
        line = (f"{sum(transient.values())}x venue transient "
                f"{sorted(transient)} = {share:.0%} of sends")
        # a steady drip is no longer "a moment" — it is an outage we are
        # papering over, so escalate on share rather than on presence
        (bad if share > 0.20 else warn).append(line)
    status = FAIL if bad else (WARN if warn else PASS)
    return {"status": status,
            "detail": ("; ".join(bad + warn) + " | " if (bad or warn) else "")
                      + f"{hours:.0f}h: {counts}, codes {codes}"}


def check_recorders(now: float) -> dict:
    """Freshness of every live stream, then the 15M tape's actual cadence.
    File mtime alone is not enough: a recorder can hold a file open and append
    nothing useful, so the tape check reads the recv_ts timeline itself."""
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    rows, bad, warn = [], [], []

    for series in MARKETS:
        p = STRIPS / series / "markets" / f"{day}.jsonl"
        ts = sorted({json.loads(ln).get("recv_ts", 0)
                     for ln in _lines(p) if ln.strip()} - {0})
        if not ts:
            bad.append(f"{series}: no tape today")
            continue
        gaps = [b - a for a, b in zip(ts, ts[1:])] or [0]
        age = now - ts[-1]
        biggest = max(gaps) / 60.0
        rows.append({"stream": series, "ticks": len(ts),
                     "max_gap_min": round(biggest, 1), "age_s": round(age)})
        if age > TAPE_MAX_GAP_MIN * 60:
            bad.append(f"{series}: silent {_fmt(age)}")
        elif biggest > TAPE_MAX_GAP_MIN:
            warn.append(f"{series}: {biggest:.0f}m gap today")

    for asset in ("BTC", "ETH", "SOL"):
        p = PRICE_DATA / "index_proxy" / "live" / asset / f"{day}.jsonl"
        ts = []
        for ln in _lines(p):
            try:
                j = json.loads(ln)
            except json.JSONDecodeError:
                continue
            v = j.get("ts") or j.get("recv_ts")
            if v:
                ts.append(v / 1000.0 if v > 1e12 else v)
        if not ts:
            bad.append(f"index {asset}: no data today")
            continue
        ts.sort()
        gaps = [b - a for a, b in zip(ts, ts[1:])] or [0]
        age = now - ts[-1]
        rows.append({"stream": f"index/{asset}", "ticks": len(ts),
                     "max_gap_s": round(max(gaps)), "age_s": round(age)})
        if age > INDEX_MAX_GAP_S:
            bad.append(f"index {asset}: silent {_fmt(age)}")
        elif max(gaps) > INDEX_MAX_GAP_S:
            warn.append(f"index {asset}: {max(gaps):.0f}s gap today")

    status = FAIL if bad else (WARN if warn else PASS)
    return {"status": status,
            "detail": "; ".join(bad + warn) or f"{len(rows)} streams fresh",
            "streams": rows}


BACKUP_DIR = Path.home() / "crypto_data_backup"
BACKUP_MAX_AGE_H = 96.0        # the prior cadence was every 4-5 days


def check_backup(now: float) -> dict:
    """The recorded tape exists in exactly one place until this runs.

    Kalshi serves ~10 days of settled 15M history and never L2 depth, so the
    tape carrying W7's verdict cannot be re-fetched. The backup silently
    stopped for 15 days (2026-08-18 -> 2026-09-02) because `pipeline.sh daily`
    has no schedule, and the entire 15M dataset was born inside that gap —
    zero copies. Staleness is therefore a FAIL, not a note.
    """
    try:
        arcs = sorted(BACKUP_DIR.glob("crypto_recorded_*.tar.gz"),
                      key=lambda p: p.stat().st_mtime)
    except OSError as e:
        return {"status": FAIL, "detail": f"backup dir unreadable: {e}"}
    if not arcs:
        return {"status": FAIL, "detail": f"no backup archive in {BACKUP_DIR}"}
    newest = arcs[-1]
    age_h = (now - newest.stat().st_mtime) / 3600.0
    size_gb = newest.stat().st_size / 1e9
    status = FAIL if age_h > BACKUP_MAX_AGE_H else (WARN if age_h > 48 else PASS)
    return {"status": status,
            "detail": f"newest {newest.name} {age_h:.0f}h old ({size_gb:.1f}G), "
                      f"{len(arcs)} kept",
            "age_h": round(age_h, 1)}


def check_disk() -> dict:
    du = shutil.disk_usage(str(PRICE_DATA))
    free_gb = du.free / 1e9
    size_gb = sum(f.stat().st_size for f in PRICE_DATA.rglob("*") if f.is_file()) / 1e9
    log_mb = WATCH_LOG.stat().st_size / 1e6 if WATCH_LOG.exists() else 0.0
    status = FAIL if free_gb < 10 else (WARN if free_gb < 30 or log_mb > 500 else PASS)
    return {"status": status,
            "detail": f"free {free_gb:.0f}G, price_data {size_gb:.1f}G, watch.log {log_mb:.0f}MB",
            "free_gb": round(free_gb, 1), "price_data_gb": round(size_gb, 2),
            "watch_log_mb": round(log_mb, 1)}


def run(now: float | None = None, contracts: int = 25) -> dict:
    now = time.time() if now is None else now
    try:
        st = json.loads(STATE.read_text())
    except (OSError, json.JSONDecodeError) as e:
        st = {}
        state_err = {"status": FAIL, "detail": f"state unreadable: {e}"}
    else:
        state_err = None
    checks = {
        "daemon": check_daemon(now),
        "errors": check_errors(),
        "books": state_err or check_books(st, contracts),
        "criteria": state_err or check_criteria(st),
        "mirror": check_mirror(now),
        "recorders": check_recorders(now),
        "backup": check_backup(now),
        "disk": check_disk(),
    }
    worst = FAIL if any(c["status"] == FAIL for c in checks.values()) else (
        WARN if any(c["status"] == WARN for c in checks.values()) else PASS)
    return {"overall": worst, "checks": checks}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--contracts", type=int, default=25)
    args = ap.parse_args(argv)
    res = run(contracts=args.contracts)
    if args.json:
        print(json.dumps(res, indent=1, default=str))
    else:
        mark = {PASS: "OK  ", WARN: "WARN", FAIL: "FAIL"}
        print("=" * 78)
        print(f"W7 v3 HEALTH — {res['overall']}")
        print("=" * 78)
        for name, c in res["checks"].items():
            print(f"  [{mark[c['status']]}] {name:10} {c['detail']}")
            for s in c.get("sample", []):
                print(f"              {s}")
    return 1 if res["overall"] == FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
