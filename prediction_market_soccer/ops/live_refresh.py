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

    python -m prediction_market_soccer.ops.live_refresh            # one shot
    python -m prediction_market_soccer.ops.live_refresh --loop 60  # foreground loop (dev)
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from prediction_market_soccer.config import CONFIG


# In-progress statuses (API-Football). A fixture sitting in one of these in OUR DB hasn't been
# finalized to FT/AET/PEN yet — so we must keep polling it.
_LIVE_STATUS = ("1H", "HT", "2H", "ET", "BT", "P", "LIVE", "INT", "SUSP")


def _in_match_window(conn) -> bool:
    """True if any fixture is plausibly live now (kicked off ≤3h ago … +5min ahead), OR our DB
    still holds an in-progress match that hasn't been finalized.

    The second clause matters for knockout ties: extra time + a penalty shootout can push the
    final whistle PAST kickoff+3h. Without it the window closes first, the cycle's
    ``sync_results`` (which flips the match to FT/AET/PEN and clears the live card) never runs,
    and the match stays stuck at its last in-play status (e.g. 'P') showing "进行中" forever.
    The clause self-terminates — once finalized the match leaves the in-progress set — and an 8h
    cap stops any ancient never-finalized row from holding the window open indefinitely."""
    from prediction_market_soccer.config.leagues import active
    lids = tuple(c.api_football_id for c in active())
    lph = ",".join("?" * len(lids))
    now = datetime.now(timezone.utc)
    lo = (now - timedelta(hours=3)).isoformat()
    hi = (now + timedelta(minutes=5)).isoformat()
    if conn.execute(
        f"SELECT COUNT(*) n FROM fixture WHERE league_id IN ({lph}) AND kickoff_ts BETWEEN ? AND ?",
        (*lids, lo, hi)).fetchone()["n"]:
        return True
    ph = ",".join("?" * len(_LIVE_STATUS))
    cap = (now - timedelta(hours=8)).isoformat()
    stuck = conn.execute(
        f"SELECT COUNT(*) n FROM fixture WHERE league_id IN ({lph}) "
        f"AND status_short IN ({ph}) AND kickoff_ts >= ?",
        (*lids, *_LIVE_STATUS, cap)).fetchone()["n"]
    return bool(stuck)


def _write_both(name: str, doc) -> None:
    """Write an export to the canonical output dir AND the served public/data dir."""
    payload = json.dumps(doc, ensure_ascii=False, indent=2)
    for d in (CONFIG.paths.output, CONFIG.paths.frontend_data):
        (d / name).write_text(payload, encoding="utf-8")



_LIVE_OR_DONE = {"1H", "2H", "HT", "ET", "BT", "P", "LIVE", "FT", "AET", "PEN"}


def _read_upcoming() -> list:
    """The board currently on disk, or [] when there is none to build on."""
    try:
        doc = json.loads((CONFIG.paths.output / "upcoming.json").read_text(encoding="utf-8"))
        return list(doc.get("matches") or [])
    except Exception:
        return []


def _started_since(conn, ids) -> set:
    """Of ``ids``, those no longer awaiting kickoff — asked of the DATABASE, not of the
    carried-over rows.

    The stale rows cannot answer this question about themselves: upcoming_export only
    ever emits not-started fixtures, so every archived row says status "NS" forever
    (measured: 128 of 128). Reading that field to decide "has this kicked off since?"
    was a test whose answer was fixed before it was asked, and a match already in play
    would have sat on the board as a pre-match row until the next daily rebuild.
    """
    ids = [i for i in ids if i is not None]
    if not ids:
        return set()
    ph = ",".join("?" * len(ids))
    return {r[0] for r in conn.execute(
        f"SELECT api_id FROM fixture WHERE api_id IN ({ph}) AND status_short <> 'NS'", ids)}


def _merge_upcoming(existing: list, fresh: list, conn=None) -> list:
    """Fresh rows replace their counterparts; everything else survives untouched.

    Ordering follows the fresh slice first (it is the near-term, actionable half) and
    then the remaining calendar in its original order, so the top of the card stays the
    part a desk can act on. A fixture that has kicked off or finished is dropped from
    the carried-over half: it belongs to the live feed or to recent_finished, and
    leaving a stale pre-match row for a match already in play is worse than omitting it.
    """
    by_id = {}
    for m in fresh:
        fid = m.get("fixture_id")
        if fid is not None:
            by_id[fid] = m
    carry = [m for m in existing
             if m.get("fixture_id") is not None and m["fixture_id"] not in by_id]
    gone = (_started_since(conn, [m["fixture_id"] for m in carry]) if conn is not None
            else {m["fixture_id"] for m in carry
                  if str(m.get("status") or "") in _LIVE_OR_DONE})
    return list(fresh) + [m for m in carry if m["fixture_id"] not in gone]


def _finalize_pending(api, conn, si) -> int:
    """Flip just-ended matches to FT/AET/PEN by pulling ONLY their fixture ids.

    The candidate set is small by construction: fixtures our DB still holds in an
    in-progress status within the last 8 hours (the same clause _in_match_window
    uses), plus anything that kicked off in the last 3 hours whatever its status —
    the batch API returns 20 fixtures per request with events embedded, so a busy
    evening costs one or two calls instead of twelve season calendars.
    """
    from prediction_market_soccer.config.leagues import active
    lids = tuple(c.api_football_id for c in active())
    ph = ",".join("?" * len(lids))
    st = ",".join("?" * len(_LIVE_STATUS))
    rows = conn.execute(
        f"SELECT api_id, status_short FROM fixture WHERE league_id IN ({ph}) AND ("
        f"  (status_short IN ({st}) AND kickoff_ts >= datetime('now','-8 hours'))"
        f"  OR kickoff_ts BETWEEN datetime('now','-3 hours') AND datetime('now')"
        f")", (*lids, *_LIVE_STATUS)).fetchall()
    ids = [r["api_id"] for r in rows]
    if not ids:
        return 0
    _FIN = ("FT", "AET", "PEN", "AWD", "WO")
    # The closing-stats pull below must fire on a TRANSITION, not on a state. The
    # recently-kicked-off clause keeps an already-finished match in this candidate set
    # for three hours, and keying on membership alone would have re-bought its stats,
    # lineup and player tables every cycle for all three of them.
    prior = {r["api_id"]: r["status_short"] for r in rows}
    was_unfinished = {fid for fid, st_ in prior.items() if st_ not in _FIN}
    try:
        items = api.fixtures_by_ids(ids)
    except Exception as e:  # noqa: BLE001 — budget/API failure must not kill the cycle
        print(f"[live_refresh] finalize check skipped: {e}")
        return 0
    n = 0
    just_finished: list[int] = []
    # _store_detailed, not _fixture_row: the ids response carries each fixture's events
    # EMBEDDED — already paid for — and the World Cup module's maturity here is exactly
    # that events land the moment a match flips to FT (the smart-exit reconstruction,
    # the milestone capture and the review logs all read them).
    from prediction_market_soccer.ingest.soccer_ingest import (
        _store_detailed, sync_fixture_players, sync_fixture_stats, sync_lineups)
    for it in items:
        _store_detailed(conn, it)
        st = ((it.get("fixture") or {}).get("status") or {}).get("short")
        if st in _FIN and it["fixture"]["id"] in was_unfinished:
            just_finished.append(it["fixture"]["id"])
        n += 1
    if n:
        conn.commit()
    # Storing the events above removes these ids from sync_results' "finished and
    # missing events" set, so its stats/lineups/players pull would silently skip
    # them — the final-whistle versions of exactly the tables squad_index and the
    # in-play study read. Pull them here for the matches that JUST finished, the
    # same close-out the World Cup module does at FT.
    if just_finished:
        try:
            sync_fixture_stats(api, conn, just_finished)
            sync_lineups(api, conn, just_finished)
            sync_fixture_players(api, conn, just_finished)
            print(f"[live_refresh] finalized {len(just_finished)} match(es) with closing stats")
        except Exception as e:  # noqa: BLE001
            print(f"[live_refresh] closing stats skipped: {e}")
    return n

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
            "hedge": mch.get("hedge"),                  # protect-leading hedge suggestion (None when N/A)
            "api_synced": synced,
        }, ensure_ascii=False))
    if lines:
        with path.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def _append_review_log_advance(inplay_adv: dict, synced: int) -> None:
    """SEPARATE per-match, per-cycle review log for the 2-way ADVANCE product (plan 24 §7) —
    parallel to _append_review_log, written to a DIFFERENT file
    (data/logs/inplay_review_advance_<date>.jsonl) so the 3-way and advance records never mix.

    One line per live KNOCKOUT match each cycle: the live ADVANCE model (home/away advance +
    reg/et/pens split), the 2-way opportunities, and the 2-way hedge suggestion."""
    ts = datetime.now(timezone.utc).isoformat()
    day = ts[:10].replace("-", "")
    path = CONFIG.paths.logs / f"inplay_review_advance_{day}.jsonl"
    CONFIG.paths.logs.mkdir(parents=True, exist_ok=True)
    lines = []
    for mch in inplay_adv.get("matches", []):
        lines.append(json.dumps({
            "ts": ts,
            "fixture_id": mch.get("fixture_id"),
            "match": f'{mch.get("home", {}).get("name")} v {mch.get("away", {}).get("name")}',
            "minute": mch.get("minute"),
            "score": mch.get("score"),
            "period": mch.get("period"),
            "reds": mch.get("reds"),
            "xg": mch.get("xg"),
            "advance_model": mch.get("model"),          # live 2-way advance (home/away) + reg/et/pens
            "n_opportunities": len(mch.get("opportunities", [])),
            "opportunities": mch.get("opportunities", []),
            "hedge_advance": mch.get("hedge_advance"),
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
    from prediction_market_soccer.ops import backfill_milestones
    backfill_milestones.backfill(conn)
    return True



# How stale a non-time-critical export may get during a match window. The loop exists to
# keep the IN-PLAY card current; everything after that write serves cards that do not
# change minute to minute (the price track, the upcoming board, the divergence table).
# Measured before this split: 71% of a cycle ran AFTER inplay_live.json was already on
# disk, and 104 of 104 match-window cycles overran the 60s launchd interval — median
# 430s, max 1,593s — so the in-play card the loop is FOR was refreshing every seven
# minutes in order to keep a price-track export a reader was not watching up to the
# second. Milestone CAPTURE stays on every cycle: it has to catch 15/30/45/60/75', and a
# minute skipped there is a snapshot lost for good.
_TAIL_INTERVAL_S = 300.0


def _due(name: str, seconds: float) -> bool:
    """True when ``name``'s last write is older than ``seconds`` (or it never happened).

    Uses the artefact's own mtime as the clock, the same stateless trick
    _maybe_refresh_risk already relies on — no extra state file to fall out of sync.
    """
    import os
    import time
    p = CONFIG.paths.output / name
    if not p.exists():
        return True
    return (time.time() - os.path.getmtime(p)) >= seconds

def _maybe_refresh_risk(conn) -> None:
    """Regenerate risk_report.json (Venues & Gates view) at most every ~10 min — its
    venue balances are live Kalshi/Poly API calls, so we throttle to avoid spamming."""
    import os
    import time
    out = CONFIG.paths.output / "risk_report.json"
    if out.exists() and (time.time() - os.path.getmtime(out)) < 600:
        return
    from dataclasses import asdict
    from prediction_market_soccer.ops import risk_report
    _write_both("risk_report.json", asdict(risk_report.build(conn)))


def _maybe_refresh_champion(conn) -> None:
    """Re-publish soccer_model.json only when the settled-match count has risen
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
    # A match just finished → refresh scorer tallies (TTL-gated per comp, cheap),
    # then re-simulate every league on the new results.
    try:
        from prediction_market_soccer.config.leagues import active as _active
        from prediction_market_soccer.ingest.api_football import ApiFootball
        from prediction_market_soccer.ingest import soccer_ingest as si
        _api = ApiFootball(conn)
        for _c in _active():
            si.sync_topscorers(_api, conn, _c)
    except Exception as e:
        print(f"[live_refresh] topscorers refresh skipped: {e}")
    from prediction_market_soccer.model.run_model import refresh_model
    pl = refresh_model()
    # A match just settled → the bet ledger, the OOS report and the PnL PDF need a
    # refresh — in a DETACHED process, not here. They ran inline for a while, which was
    # fine when they scored with the cached live model; the honest per-day walk-forward
    # made each ~140 strength fits, and this loop holds the single-instance lock, so
    # every settle wave froze the in-play card for the whole rebuild (8-15 minute
    # "cycles"; 2,440 prior rebuilds in one day's log before the date-stamped prior
    # cache landed). The spawn is fire-and-forget: reports land minutes later, the
    # card never stops, and settle_reports' own lock file keeps it single-flight.
    try:
        import subprocess
        import sys
        subprocess.Popen(
            [sys.executable, "-m", "prediction_market_soccer.ops.settle_reports"],
            cwd=str(CONFIG.paths.repo_root) if hasattr(CONFIG.paths, "repo_root") else None,
            stdout=open(CONFIG.paths.logs / "settle_reports.log", "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True)
        print("[live_refresh] settle reports spawned in background")
    except Exception as e:
        print(f"[live_refresh] settle reports spawn failed: {e}")
    # Recent-form (近期状态) also depends on the just-settled result (projected into
    # nt_recent earlier this cycle), so regenerate form.json on the SAME settle event —
    # else the form table shows stale recent results until the next daily refresh.
    try:
        from prediction_market_soccer.ops import form_export
        _write_both("form.json", form_export.build(conn))
    except Exception as e:
        print(f"[live_refresh] form refresh skipped: {e}")
    # Season odds (冠军/前四/降级盘): model season-probs were just re-simulated and the
    # Kalshi season prices move continuously → regenerate on the SAME settle event.
    try:
        from prediction_market_soccer.ops import season_odds_export
        _write_both("season_odds.json", season_odds_export.build(conn))
    except Exception as e:
        print(f"[live_refresh] season_odds refresh skipped: {e}")
    # Schedule (list + bracket views): a just-settled match must show its result — and the
    # NEXT round's now-determined pairing — immediately, not on the next daily refresh.
    try:
        from prediction_market_soccer.ops import schedule_export
        _write_both("schedule.json", schedule_export.build(conn))
    except Exception as e:
        print(f"[live_refresh] schedule refresh skipped: {e}")
    wm.write_text(str(settled))
    print(f"[live_refresh] re-simulated on new result — {len(pl.get('leagues', []))} leagues refreshed")


# ── stale-minute retry ───────────────────────────────────────────────────────
# A single DNS/timeout blip must not freeze the live minute for a whole cycle. After a live sync
# we sanity-check each in-play fixture's fetched minute against the WALL-CLOCK minute implied by
# its kickoff; if the data is clearly behind (a stale or failed pull), we re-sync a few times with
# backoff — a transient blip almost always clears on the next try. (When the network is genuinely
# down for minutes no retry can help; the frontend staleness banner is the backstop for that.)
_STALE_TOL_MIN = 6.0            # minutes the fetched minute may lag the wall-clock estimate
_SYNC_STALE_RETRIES = 3        # extra sync attempts when a lag is detected
_HALFTIME_MIN = 15.0           # nominal break subtracted for 2nd-half / ET wall-clock math
_LIVE_PLAYING = ("1H", "2H", "ET")   # statuses where elapsed tracks the wall clock (HT excluded)


def _expected_elapsed_min(kickoff_iso, status, now):
    """Wall-clock minute a live fixture SHOULD be at, or None when not checkable (bad kickoff,
    or a status like HT/penalties where `elapsed` doesn't track the wall clock)."""
    if status not in _LIVE_PLAYING or not kickoff_iso:
        return None
    try:
        ko = datetime.fromisoformat(str(kickoff_iso).replace("Z", "+00:00"))
    except Exception:
        return None
    wall = (now - ko).total_seconds() / 60.0
    if wall < 0:
        return None
    if status == "2H":
        wall -= _HALFTIME_MIN
    elif status == "ET":
        wall -= _HALFTIME_MIN + 5.0    # halftime + the short break before extra time
    return wall


def _stale_live_fixtures(conn):
    """In-play fixtures whose fetched minute lags the wall-clock estimate by more than the
    tolerance → the last pull was stale. One-directional (only 'behind' counts). Returns
    [(api_id, elapsed, expected), ...]."""
    now = datetime.now(timezone.utc)
    stale = []
    q = "SELECT api_id, kickoff_ts, status_short, elapsed FROM fixture WHERE status_short IN ({})"
    for r in conn.execute(q.format(",".join("?" * len(_LIVE_PLAYING))), _LIVE_PLAYING):
        exp = _expected_elapsed_min(r["kickoff_ts"], r["status_short"], now)
        el = r["elapsed"]
        if exp is not None and el is not None and el < exp - _STALE_TOL_MIN:
            stale.append((r["api_id"], el, round(exp, 1)))
    return stale


def _sync_live_until_fresh(api, conn, si):
    """sync_live once, then retry (with backoff) while any in-play minute is stale vs the wall
    clock — catching a transient blip that returned/failed to old data. Returns the synced count.
    Extra API calls are incurred ONLY when a lag is detected (normal cycles retry zero times)."""
    gov = getattr(si, "_governor", {}) or {}
    synced = si.sync_live(api, conn, **gov)
    for attempt in range(1, _SYNC_STALE_RETRIES + 1):
        stale = _stale_live_fixtures(conn)
        if not stale:
            break
        fx = stale[0]
        print(f"[live_refresh] stale minute (fixture {fx[0]}: data {fx[1]}' vs ~{fx[2]}' expected) "
              f"— re-syncing {attempt}/{_SYNC_STALE_RETRIES}")
        time.sleep(min(2.0, 0.5 * (2 ** (attempt - 1))))    # 0.5s → 1s → 2s backoff
        try:
            synced = si.sync_live(api, conn, **gov)
        except Exception as e:
            print(f"[live_refresh] retry sync failed ({e}); keeping best-effort state")
            break
    return synced


def refresh_once(conn=None) -> dict:
    """One in-play refresh cycle. Returns a small status dict for logging."""
    from prediction_market_soccer.ingest import store

    conn = conn or store.init_db()
    if not _in_match_window(conn):
        return {"window": False, "n_live": 0}

    # 1. Pull live fixture state (status / score / minute / xG) AND finished results.
    #    sync_live only sees CURRENTLY-live fixtures, so a match that just ended would
    #    otherwise stay stuck at its last in-play status; sync_results catches the
    #    FT/AET/PEN transition + final score so the match correctly moves to "finished".
    synced = 0
    try:
        from prediction_market_soccer.ingest.api_football import ApiFootball
        from prediction_market_soccer.ingest import soccer_ingest as si, store as _st
        api = ApiFootball(conn)
        # §6.1 three-band budget governor: shed the heaviest per-fixture extras as the
        # daily API usage climbs (players first, then live odds); pricing inputs stay.
        used = _st.daily_request_count(conn)
        _skip_players = used > 3500 or (used > 2500 and int(time.time() // 60) % 3)
        _skip_odds = used > 5000
        si._governor = {"skip_players": _skip_players, "skip_odds": _skip_odds}
        # Retry the live pull if the fetched minute lags the wall clock (a transient blip that
        # would otherwise freeze the on-screen minute for the whole cycle).
        synced = _sync_live_until_fresh(api, conn, si)
        # Finalize JUST the in-progress fixtures, by id. This line used to be
        # `sync_results(force=True)`, whose first act is a full-season fixture pull for
        # every one of the 12 competitions with the TTL bypassed — 12 season calendars
        # re-downloaded per minute-scale cycle, ~2,500 calls in one busy day, which is
        # how the 6,500/day budget ran dry at 22:00 UTC and the loop went blind: matches
        # sat frozen at 90+2' as "live" while Kalshi had already settled them, because
        # the very sync that would have flipped them to FT was what had spent the budget.
        # Catching the FT transition needs exactly the fixtures that are currently
        # in-progress (or recently kicked off and not yet finalized) — a single batched
        # ids call, ≤20 per request, with events embedded.
        _finalize_pending(api, conn, si)
        # The 14-day detail/backfill machinery still runs, TTL-gated (no force): its
        # full-season pulls then happen at most once per ttl_fixtures, not per cycle.
        si.sync_results(api, conn)
        si.project_wc_results_to_nt_recent(conn)  # keep recent-form current with WC results (0 API)
    except Exception as e:
        print(f"[live_refresh] sync failed (using stored state): {e}")

    # 2. Regenerate the in-play export (live model + venue quotes + arb) and the
    #    upcoming export (a just-started match flips out of NS → into the live feed).
    from prediction_market_soccer.ops import inplay_export, inplay_export_advance, upcoming_export

    inplay = inplay_export.build(conn, with_venues=True)
    # 2-way ADVANCE in-play (plan 24) — built + recorded in PARALLEL to the 3-way above.
    # Separate JSON + separate review-log file; failure-tolerant (never blocks the 3-way path).
    inplay_adv = None
    try:
        inplay_adv = inplay_export_advance.build(conn, with_venues=True)
        _write_both("inplay_live_advance.json", inplay_adv)
        _append_review_log_advance(inplay_adv, synced)
    except Exception as e:
        print(f"[warn] advance in-play export skipped: {e}")
    # …and grafted ONTO the 3-way rows before writing, because that is where the card
    # reads it from: the Advances lens is offered off caps.advance but rendered off
    # m.advance, so without this the live toggle was inert (the standalone advance JSON
    # has no frontend fetcher). Must run before the write below.
    inplay_export.graft_advance(inplay, inplay_adv)
    _write_both("inplay_live.json", inplay)
    _append_review_log(inplay, synced)
    # Record per-milestone price/prob snapshots as live matches cross 15/30/45/60/75',
    # then regenerate the milestone price-track export (PriceTrack / Mark-to-Market view).
    try:
        # CAPTURE every cycle — this is the one piece of the tail that is time-critical,
        # because a milestone minute passed unrecorded cannot be recovered later.
        _capture_milestones(conn, inplay)
    except Exception as e:
        print(f"[live_refresh] milestone capture skipped: {e}")
    if _due("milestone_marks.json", _TAIL_INTERVAL_S):
        try:
            # Fill PRE/FT (+ any milestone missed live) for settled matches from venue
            # history; retries across cycles until the just-ended match's history is up.
            _maybe_backfill_milestones(conn)
            from prediction_market_soccer.ops import milestone_export
            _write_both("milestone_marks.json", milestone_export.build(conn))
        except Exception as e:
            print(f"[live_refresh] milestone backfill/export skipped: {e}")
    if _due("upcoming.json", _TAIL_INTERVAL_S):
      try:
        # 12-hour horizon: the live loop only needs the boards a desk could act on in
        # this cycle. The 07:30 daily refresh prices the whole calendar.
        rows = upcoming_export.build(limit=16, conn=conn, with_venues=True,
                                     horizon_hours=12)
        # MERGED into the existing board, never written over it. This loop rebuilds a
        # NEAR-TERM slice, and writing that slice as the whole file replaced the daily
        # calendar with it — at 12 hours that means a quiet evening published an EMPTY
        # upcoming.json and the two most prominent cards (Today's Predictions, Match
        # Pricing) went blank until the next daily run. Refresh what we re-priced, keep
        # what we did not look at.
        merged = _merge_upcoming(_read_upcoming(), rows, conn)
        _write_both("upcoming.json", {
            "as_of": datetime.now(timezone.utc).isoformat(), "n": len(merged),
            "note": "Real Kalshi + Polymarket US single-match quotes; venue=null only when unlisted.",
            "matches": merged,
            "recent_finished": upcoming_export.recent_finished(conn),
        })
      except Exception as e:
        print(f"[live_refresh] upcoming rebuild failed (kept previous): {e}")

    # Model-vs-market (Divergence view) — not-started fixtures only, so a match that
    # just finished drops out of it instead of lingering.
    if _due("xv_matches.json", _TAIL_INTERVAL_S):
        try:
            from prediction_market_soccer.strategy.xv_monitor import compare_matches
            _write_both("xv_matches.json", compare_matches(limit=12))
        except Exception as e:
            print(f"[live_refresh] xv_matches rebuild skipped: {e}")


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

    from prediction_market_soccer.ops.proc_lock import acquire_or_exit
    acquire_or_exit("live_refresh")   # single-instance guard (R10)

    from prediction_market_soccer.ingest import store
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
