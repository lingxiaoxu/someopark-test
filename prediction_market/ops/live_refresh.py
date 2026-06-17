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


# In-play milestone minute thresholds (FT/PRE are filled by backfill_milestones).
_MILESTONE_MIN = [("T15", 15), ("T30", 30), ("HT", 45), ("T60", 60), ("T75", 75)]


def _capture_milestones(conn, inplay: dict) -> int:
    """Record a milestone_snapshot row the first time a live match crosses each
    minute threshold (plan 18 §2.3) — captures the live model + Kalshi/Poly book at
    that instant. Idempotent (INSERT OR IGNORE on (fixture, milestone)). PRE/FT are
    reconstructed by ops.backfill_milestones from venue history."""
    n = 0
    for m in inplay.get("matches", []):
        fid = m.get("fixture_id")
        minute = m.get("minute") or 0
        st = m.get("status")
        try:
            gh, ga = (int(x) for x in str(m.get("score", "0-0")).split("-"))
        except Exception:
            gh, ga = None, None
        model = m.get("model") or {}
        prices = m.get("prices") or {}
        kq, pq = prices.get("kalshi") or {}, prices.get("poly_us") or {}

        def ab(q, side):  # (ask, bid) from a {side:{ask,bid}} venue block
            s = (q or {}).get(side) or {}
            return s.get("ask"), s.get("bid")

        for code, thr in _MILESTONE_MIN:
            # Capture only when the match is NEAR the threshold (within GRACE minutes),
            # not whenever minute >= thr — otherwise a late start (e.g. first poll at 64')
            # would back-stamp T15/T30/… with stale current data. Milestones we miss live
            # are filled accurately from venue price history by backfill_milestones.
            _GRACE = 8
            reached = (st == "HT") if code == "HT" else (thr <= minute <= thr + _GRACE)
            if not reached:
                continue
            if conn.execute("SELECT 1 FROM milestone_snapshot WHERE fixture_api_id=? AND milestone=?",
                            (fid, code)).fetchone():
                continue
            kh, khb = ab(kq, "home"); kd, kdb = ab(kq, "draw"); ka, kab = ab(kq, "away")
            ph, phb = ab(pq, "home"); pd_, pdb = ab(pq, "draw"); pa, pab = ab(pq, "away")
            conn.execute(
                "INSERT OR IGNORE INTO milestone_snapshot "
                "(fixture_api_id, milestone, ts, elapsed, status_short, home_goals, away_goals, "
                " p_model_home, p_model_draw, p_model_away, "
                " kalshi_home_ask, kalshi_home_bid, kalshi_draw_ask, kalshi_draw_bid, kalshi_away_ask, kalshi_away_bid, "
                " poly_home_ask, poly_home_bid, poly_draw_ask, poly_draw_bid, poly_away_ask, poly_away_bid, "
                " price_source) VALUES (?,?,?,?,?,?,?, ?,?,?, ?,?,?,?,?,?, ?,?,?,?,?,?, ?)",
                (fid, code, datetime.now(timezone.utc).isoformat(), minute, st, gh, ga,
                 model.get("home"), model.get("draw"), model.get("away"),
                 kh, khb, kd, kdb, ka, kab, ph, phb, pd_, pdb, pa, pab, "live"))
            n += 1
    if n:
        conn.commit()
    return n


def _maybe_backfill_milestones(conn) -> bool:
    """Backfill PRE→FT price tracks for any SETTLED match still missing its FT
    milestone row, retrying each cycle until the venue's price history is available.

    Decoupled from the champion watermark on purpose: a just-finished match can lag
    in Polymarket's catalog for a while, so a single on-settle attempt often finds
    nothing. This cheap DB guard (only runs when a settled match lacks an FT row)
    keeps retrying until the history shows up, then naturally goes quiet.
    """
    missing = conn.execute(
        "SELECT COUNT(*) n FROM fixture f "
        "WHERE f.status_short IN ('FT','AET','PEN') AND f.home_goals IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM milestone_snapshot m "
        "                WHERE m.fixture_api_id=f.api_id AND m.milestone='FT')"
    ).fetchone()["n"]
    if not missing:
        return False
    from prediction_market.ops import backfill_milestones
    backfill_milestones.backfill(conn)
    return True


def _maybe_refresh_risk(conn) -> None:
    """Regenerate risk_report.json (Venues & Gates view) at most every ~10 min — its
    venue balances are live Kalshi/Poly API calls, so we throttle to avoid spamming."""
    import os
    import time
    out = CONFIG.paths.output / "risk_report.json"
    if out.exists() and (time.time() - os.path.getmtime(out)) < 600:
        return
    from dataclasses import asdict
    from prediction_market.ops import risk_report
    _write_both("risk_report.json", asdict(risk_report.build(conn)))


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
    # A match just settled → regenerate the performance/PnL report too, so the
    # Accuracy & P&L view (and the PDF) update on the SAME settle event as the price
    # track (both derive win/loss from performance_report.match_pick → they reconcile).
    try:
        from dataclasses import asdict
        from prediction_market.ops import performance_report
        rep = performance_report.build(conn)
        _write_both("performance_report.json", asdict(rep))
        # Re-render the PnL report PDF too (the "下载报告" view) from the SAME report,
        # so the JSON views and the PDF never disagree after a match settles.
        try:
            import shutil
            pdf = CONFIG.paths.output / "performance_report.pdf"
            performance_report.build_pdf(rep, str(pdf))
            shutil.copyfile(pdf, CONFIG.paths.frontend_data / "performance_report.pdf")
        except Exception as e:
            print(f"[live_refresh] performance PDF refresh skipped: {e}")
    except Exception as e:
        print(f"[live_refresh] performance_report refresh skipped: {e}")
    # Recent-form (近期状态) also depends on the just-settled result (projected into
    # nt_recent earlier this cycle), so regenerate form.json on the SAME settle event —
    # else the form table shows stale recent results until the next daily refresh.
    try:
        from prediction_market.ops import form_export
        _write_both("form.json", form_export.build(conn))
    except Exception as e:
        print(f"[live_refresh] form refresh skipped: {e}")
    # Reach-round (晋级盘): model round-probs were just re-simulated (refresh_champion →
    # worldcup_model.json), and the Kalshi reach-round prices move continuously, so
    # regenerate it on the SAME settle event.
    try:
        from prediction_market.ops import reach_round_export
        _write_both("reach_round.json", reach_round_export.build(conn))
    except Exception as e:
        print(f"[live_refresh] reach_round refresh skipped: {e}")
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
        si.project_wc_results_to_nt_recent(conn)  # keep recent-form current with WC results (0 API)
    except Exception as e:
        print(f"[live_refresh] sync failed (using stored state): {e}")

    # 2. Regenerate the in-play export (live model + venue quotes + arb) and the
    #    upcoming export (a just-started match flips out of NS → into the live feed).
    from prediction_market.ops import inplay_export, upcoming_export

    inplay = inplay_export.build(conn, with_venues=True)
    _write_both("inplay_live.json", inplay)
    _append_review_log(inplay, synced)
    # Record per-milestone price/prob snapshots as live matches cross 15/30/45/60/75',
    # then regenerate the milestone price-track export (PriceTrack / Mark-to-Market view).
    try:
        _capture_milestones(conn, inplay)
        # Fill PRE/FT (+ any milestone missed live) for settled matches from venue
        # history; retries across cycles until the just-ended match's history is up.
        _maybe_backfill_milestones(conn)
        from prediction_market.ops import milestone_export
        _write_both("milestone_marks.json", milestone_export.build(conn))
    except Exception as e:
        print(f"[live_refresh] milestone capture/export skipped: {e}")
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

    # OOS calibration (Calibration view) — grows as matches finish (was stuck at 15).
    try:
        from dataclasses import asdict
        from prediction_market.model import oos_eval
        _write_both("oos_report.json", asdict(oos_eval.evaluate(conn=conn)))
    except Exception as e:
        print(f"[live_refresh] oos_report rebuild skipped: {e}")

    # Risk / Venues & Gates view — its venue balances are LIVE Kalshi/Poly API calls,
    # so refresh it on a ~10-min throttle (not every minute) to keep the balance current
    # without hammering the venues.
    try:
        _maybe_refresh_risk(conn)
    except Exception as e:
        print(f"[live_refresh] risk_report refresh skipped: {e}")

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
