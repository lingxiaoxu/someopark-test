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


def _append_review_log(inplay: dict, synced: int) -> None:
    """Append a per-match, per-cycle record to a JSONL post-match review log.

    One line per live match each minute — the exact intra-game data that fed the
    model (minute / score / reds / xG), the model's computed live 3-way + remaining
    goals, and the signals it produced. Replaying this file after the match shows
    what we saw and decided, tick by tick. Lives in data/logs/inplay_review_<date>.jsonl.
    """
    ts = datetime.now(timezone.utc).isoformat()
    day = ts[:10].replace("-", "")
    path = CONFIG.paths.logs / f"inplay_review_{day}.jsonl"
    CONFIG.paths.logs.mkdir(parents=True, exist_ok=True)
    lines = []
    for mch in inplay.get("matches", []):
        lines.append(json.dumps({
            "ts": ts,
            "fixture_id": mch.get("fixture_id"),
            "match": f'{mch.get("home", {}).get("name")} v {mch.get("away", {}).get("name")}',
            "minute": mch.get("minute"),
            "score": mch.get("score"),
            "reds": mch.get("reds"),
            "xg": mch.get("xg"),                       # intra-game data fed in
            "model": mch.get("model"),                 # live 3-way + over + remaining goals
            "n_opportunities": len(mch.get("opportunities", [])),
            "opportunities": mch.get("opportunities", []),  # full signal detail (kind/side/venue/edge/reason)
            "api_synced": synced,
        }, ensure_ascii=False))
    if lines:
        with path.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def _maybe_refresh_champion(conn) -> None:
    """Re-publish worldcup_model.json only when the settled-match count has risen
    (a match just finished) — keeps the heavy tournament sim off the per-minute path."""
    settled = conn.execute(
        "SELECT COUNT(*) n FROM fixture WHERE status_short IN ('FT','AET','PEN') AND home_goals IS NOT NULL"
    ).fetchone()["n"]
    wm = CONFIG.paths.output / ".champion_watermark"
    prev = -1
    try:
        prev = int(wm.read_text().strip())
    except Exception:
        pass
    if settled == prev:
        return
    # A match just finished → refresh the WC scorer tallies first so the golden boot
    # reflects the goals just scored (e.g. a hat-trick lifts that player's odds), then
    # re-simulate champion + golden boot on the new results.
    try:
        from prediction_market.ingest.api_football import ApiFootball
        from prediction_market.ingest import soccer_ingest as si
        si.sync_topscorers(ApiFootball(conn), conn, force=True)
    except Exception as e:
        print(f"[live_refresh] topscorers refresh skipped: {e}")
    from prediction_market.model.run_model import refresh_champion
    pl = refresh_champion()
    wm.write_text(str(settled))
    top = pl["champion"][0]
    gb = pl["golden_boot"][0]
    print(f"[live_refresh] re-simulated on new result — champion {top['name']} {top['p_champion']:.1%}, "
          f"golden boot {gb['name']} {gb['p_golden_boot']:.1%}")


def refresh_once(conn=None) -> dict:
    """One in-play refresh cycle. Returns a small status dict for logging."""
    from prediction_market.ingest import store

    conn = conn or store.init_db()
    if not _in_match_window(conn):
        return {"window": False, "n_live": 0}

    # 1. Pull live fixture state (status / score / minute / xG) AND finished results.
    #    sync_live only sees CURRENTLY-live fixtures, so a match that just ended would
    #    otherwise stay stuck at its last in-play status; sync_results catches the
    #    FT/AET/PEN transition + final score so the match correctly moves to "finished".
    synced = 0
    try:
        from prediction_market.ingest.api_football import ApiFootball
        from prediction_market.ingest import soccer_ingest as si
        api = ApiFootball(conn)
        synced = si.sync_live(api, conn)
        # force=True: inside a live window we must catch the FT transition promptly,
        # so bypass the fixtures TTL/watermark (bounded — only runs during the window).
        si.sync_results(api, conn, force=True)   # flip just-ended matches to FT + final score
    except Exception as e:
        print(f"[live_refresh] sync failed (using stored state): {e}")

    # 2. Regenerate the in-play export (live model + venue quotes + arb) and the
    #    upcoming export (a just-started match flips out of NS → into the live feed).
    from prediction_market.ops import inplay_export, upcoming_export

    inplay = inplay_export.build(conn, with_venues=True)
    _write_both("inplay_live.json", inplay)
    _append_review_log(inplay, synced)
    try:
        rows = upcoming_export.build(limit=6, conn=conn, with_venues=True)
        # Same envelope the daily refresh writes (frontend reads `.matches`).
        # recent_finished surfaces just-ended matches (FT + score) so a live match
        # that finishes is marked ended in the top region, not silently dropped.
        _write_both("upcoming.json", {
            "as_of": datetime.now(timezone.utc).isoformat(), "n": len(rows),
            "note": "Real Kalshi + Polymarket US single-match quotes; venue=null only when unlisted.",
            "matches": rows,
            "recent_finished": upcoming_export.recent_finished(conn),
        })
    except Exception as e:
        print(f"[live_refresh] upcoming rebuild failed (kept previous): {e}")

    # Model-vs-market (Divergence view) — not-started fixtures only, so a match that
    # just finished drops out of it instead of lingering.
    try:
        from prediction_market.strategy.xv_monitor import compare_matches
        _write_both("xv_matches.json", compare_matches(limit=12))
    except Exception as e:
        print(f"[live_refresh] xv_matches rebuild skipped: {e}")

    # When a match has just FINISHED (settled count rose), re-simulate the champion +
    # golden boot on the new results (and force eliminated teams to 0% in the knockouts).
    # Guarded by a watermark so the ~4s sim runs only on a result change, not every minute.
    try:
        _maybe_refresh_champion(conn)
    except Exception as e:
        print(f"[live_refresh] champion refresh skipped: {e}")

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
