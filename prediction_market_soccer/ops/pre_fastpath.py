"""PRE fast path: stage imminent pre-kickoff observations and decide immediately.

The live cycle stages PRE observations in its upcoming tail and decides in its
paper phase; on heavy matchdays the cycle stretches past the 120-second
observation-freshness guard and every pre decision dies as
``waiting_data/missing_fresh_observation`` (observed 2026-09-12: five EPL
kickoffs lost in a row). This wrapper closes the gap by running the SAME two
pinned functions back-to-back, seconds apart:

    upcoming_export.build(horizon_hours=..., with_venues=True)   # quotes + _stash_pre
    paper_trading.run_cycle(conn)                                # decide now

No new decision logic, no relaxed guard: every certified check still runs,
the observation is simply fresh when it is checked. One pass per invocation;
``--loop`` repeats until --until (UTC ISO) for temporary matchday use. The
structural fix (decide right after staging inside the live cycle) is pinned
and rides the next forward-method epoch.
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone


def one_pass(horizon_hours: float) -> dict:
    from prediction_market_soccer.ingest import store
    from prediction_market_soccer.ops import upcoming_export, paper_trading
    conn = store.init_db()
    rows = upcoming_export.build(conn=conn, with_venues=True, horizon_hours=horizon_hours)
    out = paper_trading.run_cycle(conn)
    keep = {"state": out.get("state"), "entries": out.get("entries"),
            "exits": out.get("exits"), "errors": out.get("errors")}
    pre = [d for d in out.get("data_states", []) if d.get("track") == "pre"]
    return {"staged_candidates": len(rows), "paper": keep, "pre_states": pre}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon-hours", type=float, default=0.4)
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=90)
    ap.add_argument("--until", help="UTC ISO time to stop the loop")
    args = ap.parse_args()
    while True:
        t0 = time.time()
        try:
            res = one_pass(args.horizon_hours)
            pre = res["pre_states"]
            summary = " ".join(f"{d['fixture_api_id']}:{d['state']}/{d.get('reason','')[:28]}" for d in pre) or "-"
            print(f"[pre_fastpath] {datetime.now(timezone.utc).isoformat()[11:19]} "
                  f"cand={res['staged_candidates']} paper={res['paper']['state']} "
                  f"entries={res['paper']['entries']} pre=[{summary}] ({time.time()-t0:.0f}s)", flush=True)
        except Exception as exc:  # noqa: BLE001 — a transient failure must not end the loop
            print(f"[pre_fastpath] ERROR {type(exc).__name__}: {str(exc)[:140]}", flush=True)
        if not args.loop:
            break
        if args.until and datetime.now(timezone.utc) >= datetime.fromisoformat(args.until):
            print("[pre_fastpath] until reached — stop", flush=True)
            break
        time.sleep(max(10, args.interval - (time.time() - t0)))


if __name__ == "__main__":
    main()
