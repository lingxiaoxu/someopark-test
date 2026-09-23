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
  obs_leg    — the FLB tripwire, as a TEST rather than a sign check: it read
               "positive means the structure changed" and duly fired on noise
  mirror     — demo mirror outcomes: 409s and wrong-window maps must be zero
  demo_pos   — the demo account must hold only 15M legs we mirrored; a stray
               market is the wrong-window bug leaving evidence
  recorders  — every live stream's freshness, plus today's 15M tape cadence:
               the tape IS the probe's eyes, and it is irreplaceable
  backup     — how old the newest archive is: the tape had exactly one copy
               for 15 days, and the 15M dataset was born inside that gap
  diskmon    — the disk alarm's own liveness: it was silently dead for 61
               days when the 2026-09-11 exhaustion killed four recorders
  disk       — headroom, because the recorders never stop
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import math
import os
import shutil
import subprocess
import time
from pathlib import Path

from crypto_trading.crypto_common.config import PRICE_DATA, SIGNALS_DIR
from crypto_trading.crypto_strategies.event_binary.research_favorite_no import (
    COST_HI, COST_LO, MAIN_HI, MAIN_LO, MARKETS, OBS_LO, PRIMARY_HI, PRIMARY_LO)
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
HL_MAX_GAP_S = 180.0           # 5s recorder, plus room for its 429 backoff
OKX_HEARTBEAT_MAX_AGE_S = 90.0
OKX_EVENT_QUIET_S = 1800.0    # event-driven: quiet is WARN, not a false disconnect

# The two matched sets. They must stay the same universe: the alt-data half is
# only interpretable against a spot index for the SAME coin, and W8 trades
# BTC/ETH/DOGE/XRP while the index set originally covered BTC/ETH/SOL - so DOGE
# and XRP were flying with no underlying feed at all until 2026-09-14.
INDEX_ASSETS = ("BTC", "ETH", "SOL", "DOGE", "XRP")
HL_STREAMS = tuple(f"{kind}/{coin}" for kind in ("context", "book", "trades")
                   for coin in INDEX_ASSETS) + ("accounts", "twap_fills",
                                                "address_pool")


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


def _json_lines(path: Path):
    for ln in _lines(path):
        if not ln.strip():
            continue
        try:
            yield json.loads(ln)
        except json.JSONDecodeError:
            continue


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
    def _hits(lines):
        return [ln.strip()[:120] for ln in lines
                if "Traceback" in ln or " ERROR " in ln]

    RECENT = 500                # ~1-2h of log at the runner's line rate
    recent, older = _hits(tail[-RECENT:]), _hits(tail[:-RECENT])
    if recent:
        return {"status": FAIL,
                "detail": f"{len(recent)} error line(s) in last {RECENT}",
                "sample": recent[:3]}
    if older:
        # An incident that has STOPPED producing errors is history, not an
        # alarm: the 2026-09-11 ENOSPC tracebacks kept this check red for
        # hours after the disk recovered and every cycle was clean again.
        return {"status": WARN,
                "detail": f"recent {RECENT} lines clean; {len(older)} older "
                          f"error line(s) still in the last {lookback_lines}",
                "sample": older[-2:]}
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
    # MAIN counts only entries opened after its registration stamp — the
    # discovery period must never leak into the evidence it suggested
    reg = st.get("main_registered_at")
    if reg:
        main_n = sum(v["n"] for v in (st.get("windows_main") or {}).values())
        main_t = sum(1 for t in tr if MAIN_LO <= t["cost"] <= MAIN_HI
                     and str(t.get("opened", "")) >= reg)
        if main_n != main_t:
            bad.append(f"main window count {main_n} != post-registration trades in cell {main_t}")

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


def check_obs_leg(st: dict, min_windows: int = 30) -> dict:
    """The observation leg [0.50,0.60) is the FLB's negative print: the rule
    says a POSITIVE leg means the structure changed and everything re-opens.

    As first written that tripwire was "turns positive", which at this leg's
    dispersion (SE ~5.8c) is a coin flip — it fired on 2026-09-03 at +2.27c
    with t +0.39, first half -4.81c and second half +9.36c, i.e. on nothing.
    A tripwire that fires on noise gets ignored, so it is a TEST now:
    significantly positive (t >= 2) over enough independent windows.
    """
    obs = st.get("obs_trades") or []
    if not obs:
        return {"status": PASS, "detail": "no observation-leg trades yet"}
    g = {}
    for t in obs:
        w = g.setdefault(t["ticker"].split("-")[1], [0, 0.0])
        w[0] += 1
        w[1] += t["pnl_c"]
    N = sum(n for n, _ in g.values())
    G = len(g)
    mu = sum(x for _, x in g.values()) / N
    tstat = 0.0
    if G >= 2:
        meat = sum((x - n * mu) ** 2 for n, x in g.values()) * G / (G - 1)
        tstat = mu / (math.sqrt(meat) / N) if meat > 0 else 0.0
    flipped = G >= min_windows and tstat >= 2.0
    return {"status": WARN if flipped else PASS,
            "detail": (("STRUCTURE CHANGED — " if flipped else "")
                       + f"{N} trades / {G} windows {mu:+.2f}c t {tstat:+.2f} "
                       + f"(backtest -7.21c; fires at t>=2 with {min_windows}+ windows)"),
            "mean_c": round(mu, 2), "t": round(tstat, 2), "windows": G}


def check_criteria(st: dict) -> dict:
    from crypto_trading.crypto_strategies.live_watch.w7_noisefade import pooled_stats
    _, n, mu, t = pooled_stats(st.get("windows_primary") or {})
    _, nm, mum, tm = pooled_stats(st.get("windows_main") or {})
    nw, muw, _ = window_stats(st.get("windows") or {})
    if st.get("killed"):
        return {"status": WARN, "detail": f"KILLED — {st.get('killed_reason', '?')}",
                "primary_windows": n, "primary_mean_c": round(mu, 2), "primary_t": round(t, 2)}
    return {"status": PASS,
            "detail": f"MAIN {nm}/300 windows {mum:+.2f}c t {tm:+.2f} | "
                      f"primary {n}/300 {mu:+.2f}c t {t:+.2f} "
                      f"| wide band {nw} windows {muw:+.2f}c",
            "main_windows": nm, "main_mean_c": round(mum, 2), "main_t": round(tm, 2),
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


def check_okx_liquidations(now: float) -> dict:
    """Bounded status read: live connection != successfully recorded events."""
    path = PRICE_DATA / "offshore" / "okx" / "liquidations" / "recorder_status.json"
    try:
        st = json.loads(path.read_text())
        if not isinstance(st, dict) or st.get("schema_version") != 1 or st.get("driver") != "okx":
            raise ValueError("invalid recorder status schema/driver")
        ages = {}
        for field in ("updated_at", "last_message_at", "last_event_at"):
            value = st.get(field)
            if value is None and field != "updated_at":
                ages[field] = None
                continue
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value <= 0 or value > now + 5):
                raise ValueError(f"invalid {field}")
            ages[field] = max(0.0, now - value)
    except (OSError, ValueError, TypeError) as exc:
        return {"status": FAIL, "detail": f"okx liquidation status unreadable: {exc}",
                "connection_state": "unknown", "connected_at": None,
                "last_message_at": None, "last_event_at": None}
    result = {"connection_state": st.get("state"), "subscribed": st.get("subscribed"),
              "connected_at": st.get("connected_at"), "last_message_at": st.get("last_message_at"),
              "updated_at": st.get("updated_at"),
              "heartbeat_age_s": round(ages["updated_at"], 1),
              "message_age_s": None if ages["last_message_at"] is None else round(ages["last_message_at"], 1),
              "event_age_s": None if ages["last_event_at"] is None else round(ages["last_event_at"], 1),
              "last_event_at": st.get("last_event_at"), "events_written": st.get("events_written"),
              "open_files": st.get("open_files"), "max_open_files": st.get("max_open_files")}
    if ages["updated_at"] > OKX_HEARTBEAT_MAX_AGE_S:
        result.update(status=FAIL, detail=f"okx liquidation heartbeat silent {_fmt(ages['updated_at'])}")
    elif st.get("state") != "connected" or st.get("subscribed") is not True:
        result.update(status=FAIL, detail=f"okx liquidation connection {st.get('state')}, subscribed={st.get('subscribed')}")
    elif ages["last_message_at"] is None or ages["last_message_at"] > OKX_HEARTBEAT_MAX_AGE_S:
        result.update(status=FAIL, detail="okx liquidation connection has no recent message/pong")
    elif ages["last_event_at"] is None or ages["last_event_at"] > OKX_EVENT_QUIET_S:
        quiet = "none recorded yet" if ages["last_event_at"] is None else f"last event {_fmt(ages['last_event_at'])} ago"
        result.update(status=WARN, detail=f"okx connected and responsive; event stream quiet ({quiet})")
    else:
        result.update(status=PASS, detail=f"okx connected; last recorded event {_fmt(ages['last_event_at'])} ago")
    return result


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

    # 2026-09-14: DOGE and XRP were added so the spot-index set covers the same
    # universe as the Hyperliquid alt-data set below. They run as a SECOND
    # instance of the same recorder (`--assets DOGE,XRP`) so the original
    # BTC/ETH/SOL process is never interrupted; both write disjoint asset dirs.
    for asset in INDEX_ASSETS:
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

    # The Hyperliquid alt-data set. Unlike prices, none of it is backfillable -
    # the venue serves current state only - so a silent day here is a day of
    # evidence that can never be recovered, which is exactly why it is watched
    # beside the tape rather than trusted to run.
    for stream in HL_STREAMS:
        p = PRICE_DATA / "hyperliquid" / stream / f"{day}.jsonl"
        ts = sorted(j["recv_ts"] for j in _json_lines(p) if j.get("recv_ts"))
        if not ts:
            # trades/accounts/twap_fills are event-driven: quiet is not broken
            # as long as the polled streams are alive, so only context is FAIL.
            (bad if stream.startswith("context/") else warn).append(
                f"hl {stream}: no data today")
            continue
        gaps = [b - a for a, b in zip(ts, ts[1:])] or [0]
        age = now - ts[-1]
        rows.append({"stream": f"hl/{stream}", "ticks": len(ts),
                     "max_gap_s": round(max(gaps)), "age_s": round(age)})
        if stream.startswith(("context/", "book/")) and age > HL_MAX_GAP_S:
            bad.append(f"hl {stream}: silent {_fmt(age)}")

    okx = check_okx_liquidations(now)
    rows.append({"stream": "okx/liquidations", **okx})
    if okx["status"] == FAIL:
        bad.append(okx["detail"])
    elif okx["status"] == WARN:
        warn.append(okx["detail"])

    status = FAIL if bad else (WARN if warn else PASS)
    return {"status": status,
            "detail": "; ".join(bad + warn) or f"{len(rows)} streams fresh",
            "streams": rows}


DISKMON_STATUS = ROOT / "logs" / "disk_monitor_status.json"
DISKMON_MAX_AGE_H = 2.0        # launchd runs it every 30 min


def check_diskmon(now: float) -> dict:
    """The alarm's own liveness. The 2026-09-11 exhaustion killed four
    recorders with no warning because disk_monitor had been silently not
    running for 61 days — scheduled nowhere, watched by nobody. A monitor
    for the monitor is the only fix that survives that failure mode."""
    try:
        st = json.loads(DISKMON_STATUS.read_text())
        age_h = (now - st.get("ts", 0)) / 3600.0
    except (OSError, json.JSONDecodeError) as e:
        return {"status": FAIL, "detail": f"disk alarm status unreadable: {e}"}
    if age_h > DISKMON_MAX_AGE_H:
        return {"status": FAIL,
                "detail": f"disk alarm silent {age_h:.1f}h (launchd "
                          f"com.someopark.crypto.diskmon dead?)"}
    level = st.get("level", "?")
    detail = (f"alarm alive ({age_h*60:.0f}m ago), level {level}, "
              f"free {st.get('free_gb')}G")
    if st.get("alerts"):
        detail += " | " + "; ".join(st["alerts"])[:120]
    return {"status": PASS if level == "ok" else WARN, "detail": detail}


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


def check_demo_positions() -> dict:
    """Everything the demo account holds should be a 15M window we mirrored.

    Before the 2026-09-01 fix the mirror mapped 15-minute windows onto whatever
    demo happened to quote, and one of those maps left 309.58 contracts of a
    KXBTC market expiring 2027-05-24 sitting in the account. It cannot even be
    sold — that market has no bid on either side — so the only defence is
    noticing the next one on the day it appears rather than months later.

    Needs demo credentials; without them this degrades to a note rather than
    failing the whole check (the probe itself does not depend on it).
    """
    try:
        from crypto_trading.crypto_common.kalshi.rest_event import (
            KalshiEventOrderClient)
        r = KalshiEventOrderClient(env="demo")._authed(
            "GET", "/portfolio/positions?limit=200")
        rows = (r.json() or {}).get("market_positions", [])
    except Exception as e:                                   # noqa: BLE001
        return {"status": PASS, "detail": f"skipped (no demo access: {type(e).__name__})"}
    held = [x for x in rows if float(x.get("position_fp") or 0) != 0]
    # The mirror only ever touches crypto series, so a crypto market that is
    # not a 15M window is the wrong-window bug's fingerprint; anything else
    # (sports, politics) is the user's own manual demo trading and none of
    # this check's business (2026-09-06: an EPL bet tripped it).
    crypto = ("KXBTC", "KXETH", "KXSOL", "KXDOGE", "KXXRP")
    stray = [x for x in held
             if x["ticker"].split("-")[0].startswith(crypto)
             and not x["ticker"].split("-")[0].endswith("15M")]
    ours = sum(1 for x in held if x["ticker"].split("-")[0].endswith("15M"))
    if stray:
        worst = sorted(stray, key=lambda x: -abs(float(x.get("market_exposure_dollars") or 0)))
        return {"status": WARN,
                "detail": (f"{len(stray)} non-15M position(s) the mirror should "
                           f"never hold: "
                           + ", ".join(f"{x['ticker']} {x['position_fp']}"
                                       for x in worst[:3])
                           + f" | {ours} live 15M leg(s)")}
    return {"status": PASS, "detail": f"{ours} 15M leg(s), no stray positions"}


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
        "obs_leg": state_err or check_obs_leg(st),
        "mirror": check_mirror(now),
        "demo_pos": check_demo_positions(),
        "recorders": check_recorders(now),
        "backup": check_backup(now),
        "diskmon": check_diskmon(now),
        "disk": check_disk(),
    }
    worst = FAIL if any(c["status"] == FAIL for c in checks.values()) else (
        WARN if any(c["status"] == WARN for c in checks.values()) else PASS)
    return {"overall": worst, "checks": checks}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--contracts", type=int, default=25)
    ap.add_argument("--okx-only", action="store_true",
                    help="read only the liquidation recorder status; no account/API or full tape scan")
    args = ap.parse_args(argv)
    if args.okx_only:
        check = check_okx_liquidations(time.time())
        res = {"overall": check["status"], "checks": {"okx_liquidations": check},
               "checked_at": time.time()}
    else:
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
