"""ops/live_refresh.py — per-minute in-play refresh during match windows.

Run every ~60s (launchd). Cheap no-op outside match windows; inside a window it
pulls live fixture state (API-Football: status / score / minute / xG) and regenerates
the in-play + upcoming exports so the dashboard updates every minute. It writes the
JSON to BOTH the canonical output dir AND the frontend's public/data dir, which the
local Express server serves over the Cloudflare tunnel — so the live site refreshes
WITHOUT a Firebase redeploy (the frontend polls inplay_live.json every 30s).

Match window = any fixture kicked off in the last ~3h or starting in the next ~5min
(covers 90' + stoppage + half-time + a pre-kickoff warm-up). Outside it: 1 DB query,
no API calls, no writes.

    python -m prediction_market.ops.live_refresh            # one shot
    python -m prediction_market.ops.live_refresh --loop 60  # foreground loop (dev)
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from prediction_market.config import CONFIG


def _in_match_window(conn) -> bool:
    """True if any fixture is plausibly live now (kicked off ≤3h ago … +5min ahead)."""
    now = datetime.now(timezone.utc)
    lo = (now - timedelta(hours=3)).isoformat()
    hi = (now + timedelta(minutes=5)).isoformat()
    n = conn.execute(
        "SELECT COUNT(*) n FROM fixture WHERE kickoff_ts BETWEEN ? AND ?", (lo, hi)
    ).fetchone()["n"]
    return bool(n)


def _write_both(name: str, doc) -> None:
    """Write an export to the canonical output dir AND the served public/data dir."""
    payload = json.dumps(doc, ensure_ascii=False, indent=2)
    for d in (CONFIG.paths.output, CONFIG.paths.frontend_data):
        (d / name).write_text(payload, encoding="utf-8")


def refresh_once(conn=None) -> dict:
    """One in-play refresh cycle. Returns a small status dict for logging."""
    from prediction_market.ingest import store

    conn = conn or store.init_db()
    if not _in_match_window(conn):
        return {"window": False, "n_live": 0}

    # 1. Pull live fixture state (status / score / minute / xG). Resilient: if the
    #    API hiccups we still regenerate from the last stored state.
    synced = 0
    try:
        from prediction_market.ingest.api_football import ApiFootball
        from prediction_market.ingest import soccer_ingest as si
        api = ApiFootball(conn)
        synced = si.sync_live(api, conn)
    except Exception as e:
        print(f"[live_refresh] sync_live failed (using stored state): {e}")

    # 2. Regenerate the in-play export (live model + venue quotes + arb) and the
    #    upcoming export (a just-started match flips out of NS → into the live feed).
    from prediction_market.ops import inplay_export, upcoming_export

    inplay = inplay_export.build(conn, with_venues=True)
    _write_both("inplay_live.json", inplay)
    try:
        rows = upcoming_export.build(limit=6, conn=conn, with_venues=True)
        # Same envelope the daily refresh writes (frontend reads `.matches`).
        _write_both("upcoming.json", {
            "as_of": datetime.now(timezone.utc).isoformat(), "n": len(rows),
            "note": "Real Kalshi + Polymarket US single-match quotes; venue=null only when unlisted.",
            "matches": rows,
        })
    except Exception as e:
        print(f"[live_refresh] upcoming rebuild failed (kept previous): {e}")

    n_opp = sum(len(m.get("opportunities", [])) for m in inplay["matches"])
    return {"window": True, "synced": synced, "n_live": inplay["n_live"], "n_opp": n_opp}


def main() -> None:
    ap = argparse.ArgumentParser(description="In-play refresh during match windows")
    ap.add_argument("--loop", type=int, default=0, help="seconds between cycles (0 = one shot)")
    args = ap.parse_args()

    from prediction_market.ingest import store
    conn = store.init_db()

    def _go():
        st = refresh_once(conn)
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        if st["window"]:
            print(f"[live_refresh {ts}] live={st['n_live']} opps={st.get('n_opp', 0)} synced={st.get('synced', 0)}")
        else:
            print(f"[live_refresh {ts}] no match window — skip")
        return st

    if args.loop:
        while True:
            try:
                _go()
            except Exception as e:
                print(f"[live_refresh] cycle error: {e}")
            time.sleep(args.loop)
    else:
        _go()


if __name__ == "__main__":
    main()
