"""jobs/tick.py — the minute executor of materialised runs (PLAN §8.2-2) + the
event-window densifier (§8.1: T-2h 5-min snapshots, ±10min 1-min fast polling).

launchd restarts an idle tick every 60s. A tick lock excludes manual duplicates.
When a release window is live ([T-2h, T+30min]), one process remains for the
WHOLE window; ending after 840s used to leave a 15-minute gap before relaunch.
The event loop:
  * snapshots the affected series every 5 min (1 min inside ±10 min of the release)
  * claims newly-due runs mid-linger — so the T+3m reassess task executes ON TIME
    with fresh post-release quotes rather than waiting for a separate launchd invocation.

    conda run -n someopark_run python -m prediction_market_macro.jobs.tick
"""
from __future__ import annotations

import json
import os
import tempfile
import time as _time
from datetime import datetime, timedelta, timezone

from prediction_market_macro.config.registry import REGISTRY
from prediction_market_macro.config.settings import load_settings
from prediction_market_macro.ingest.kalshi_md import KalshiMD
from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.jobs import scheduler
from prediction_market_macro.util.execution import execution_lock


def _export_frontend(conn, s) -> None:
    """Best-effort: a display refresh must never fail a trading task."""
    try:
        from prediction_market_macro.ops import frontend_export
        frontend_export.run(conn, s)
    except Exception as e:                                       # noqa: BLE001
        print(f"  frontend_export skipped: {e}")


def _top_up_stale_quotes(conn, md, now: datetime) -> dict:
    """Re-snapshot every series `decide_all` is about to scan whose quotes went stale.

    `_exec_task` snapshots the triggering run's OWN series and then calls
    `decide_all.run()`, which scans every registered series. `predict_all.run()` was
    already made global to match — quotes never were, and that asymmetry is the whole
    bug: decide_all's §8.2-5 hard gate force-passes anything whose freshest quote is
    over QUOTE_STALE_H old, before it ever computes an edge.

    So on a weekly-close evening the one series that triggered the run is decidable and
    the other thirteen are not. Measured: 08-13 103 force-passes at 19:27Z (the tick
    after `weekly_close/arm`), 08-14 204 across decide/freeze/reassess, 08-17 55 across
    the KXAAAGASW close_anchor + weekly_close chain — every one of them reading
    `stale_inputs pred=0h quotes=8.6..15.9h`. Pred fresh, quote stale, edge never looked
    at. The 09:00 refresh is the only thing that snapshots broadly, so the gate shuts
    ~15:00Z and stays shut for the rest of the day.

    Only stale series are fetched. On a day when the morning refresh already covered
    everything this is one SELECT and zero API calls; `snapshot_series` costs an
    orderbook request per market (~8s for a 23-market series), which is why it is not
    simply run unconditionally.

    Best-effort per series: a venue error leaves that series stale and decide_all
    force-passes it exactly as it does today, which is the right failure direction.
    """
    from prediction_market_macro.ops.decide_all import QUOTE_STALE_H
    refreshed, failed = {}, {}
    for spec in REGISTRY.values():
        # mirror decide_all's own loop entry: no active period ⇒ it never looks at this
        # series, so refreshing it would be pure API cost
        row = conn.execute(
            "SELECT MAX(q.ts) m FROM contracts c LEFT JOIN quotes q ON q.ticker=c.ticker"
            " WHERE c.series=? AND c.status='active'", (spec.ticker,)).fetchone()
        if row is None or row["m"] is None:
            continue
        age_h = (now - datetime.fromisoformat(row["m"])).total_seconds() / 3600.0
        if age_h <= QUOTE_STALE_H:
            continue
        try:
            refreshed[spec.ticker] = md.snapshot_series(spec.ticker)
        except Exception as e:                                   # noqa: BLE001
            failed[spec.ticker] = str(e)[:80]
    if failed:
        print(f"  ! quote top-up failed: {failed}")
    return refreshed


def _require_critical_refresh_success(result) -> None:
    """A recent completed refresh can still have failed its trading steps."""
    steps = result.get("steps") if isinstance(result, dict) else None
    if not isinstance(steps, dict):
        return                         # older timestamp-only stamps remain compatible
    failed = [name for name in ("predict_all", "decide_all", "exits")
              if str(steps.get(name, "")).startswith("FAIL")]
    if failed:
        raise RuntimeError("critical refresh steps failed: " + ", ".join(failed))


def _exec_task(conn, s, md, r) -> str:
    task, series = r["task"], r["series"]
    from prediction_market_macro.ops import decide_all, exits, pnl, predict_all
    if task in ("arm", "snapshot", "reassess", "decide"):
        if series in REGISTRY:
            _drain_freezes(conn)
            try:
                md.snapshot_series(series)
            finally:
                _drain_freezes(conn)
    if task in ("arm", "decide", "reassess"):
        # decide_all scans ALL series; give it fresh quotes for all of them, not just
        # this run's. See _top_up_stale_quotes.
        topped = _top_up_stale_quotes(conn, md, datetime.now(timezone.utc))
        if topped:
            print(f"  quote top-up: {topped}")
        _drain_freezes(conn)
        try:
            predict_all.run(conn, s, fail_on_error=True)
        finally:
            _drain_freezes(conn)
        with execution_lock(conn):
            # Another short ledger pass may have owned the lock while this task's
            # window elapsed. Re-check after acquisition, not only before waiting.
            if scheduler.expire_if_overdue(conn, r):
                return "expired before decision"
            decide_all.run(conn, s)
        from prediction_market_macro.ops.position_inputs import fresh_held_tickers
        _drain_freezes(conn)
        eligible = fresh_held_tickers(conn, md, before_each=lambda: _drain_freezes(conn))
        _drain_freezes(conn)
        exits.shadow_run(conn, s, eligible_tickers=eligible)
        exits.run(conn, s, eligible_tickers=eligible)
        pnl.mark_all(conn)
        # §30 mirror backstop: inline on_fill hooks fire inside decide/exits; this
        # sweep catches anything they missed and advances order polling + the
        # balance-sheet snapshot. Best-effort like the export below.
        try:
            from prediction_market_macro.ops import trading_kalshi
            trading_kalshi.sync(conn)
        except Exception as e:                                   # noqa: BLE001
            print(f"  trading_kalshi.sync skipped: {e}")
        _export_frontend(conn, s)   # 2026-08-13: intraday trades were invisible
        # until the NEXT MORNING's refresh — the site reads local public/data over
        # the tunnel, so freshness is decided here, not by a deploy.
    if task == "reassess" and series in REGISTRY:
        # §24-B: the print is public by T+3m — snipe legs whose settlement is
        # already determined but still mispriced
        from prediction_market_macro.strategy import snipe
        with execution_lock(conn):
            if scheduler.expire_if_overdue(conn, r):
                return "expired before reassessment"
            ns = snipe.run_for(conn, series, r["period"])
        if ns:
            return f"snipes={ns}"
    if task == "freeze":
        scheduler.set_coverage(conn, series, r["period"], "frozen")
    if task == "reconcile":
        if series in REGISTRY:
            md.sync_settlements(series)
        pnl.settle_pass(conn)
        scheduler.set_coverage(conn, series, r["period"], "reconciled")
        _export_frontend(conn, s)   # settles change the live track — same reason
    if task in ("daily_refresh", "health", "pred_freshness"):
        last = s.output_dir / "refresh_last.json"
        if last.exists():
            previous = json.loads(last.read_text())
            ts = previous.get("ts")
            if ts and datetime.now(timezone.utc) - datetime.fromisoformat(ts) \
                    < timedelta(hours=20):
                # Surface the failed refresh without launching its entire ingestion
                # again every minute. The regular next refresh can replace this stamp.
                _require_critical_refresh_success(previous)
                return "covered_by_daily_refresh"
        from prediction_market_macro.ops import refresh
        # The stamp above is written only by refresh's LAST line, so between 09:00:05
        # (launchd fires) and ~09:17 (it finishes) this branch cannot tell that today's
        # refresh is already running — and this run is materialised at exactly 09:00:00Z,
        # so the first tick after it lands inside that window essentially every day.
        # refresh.run() holds an flock and refuses; leaving the run 'late' means the next
        # tick re-checks, by which time the stamp is fresh -> covered_by_daily_refresh.
        # If instead the 05:00 job never fired, the lock is free and this really does run.
        result = refresh.run()                           # raises RefreshBusy -> mark_late
        _require_critical_refresh_success(result)
        return "ran_full_refresh"
    return "ok"


def snap_interval(dt_since_release_sec: float) -> int | None:
    """Snapshot cadence inside the event window (§8.1), keyed on seconds SINCE the
    scheduled release (negative = before): 1-min inside ±10 min, 5-min inside
    [T-2h, T+30min], None outside."""
    if -600 <= dt_since_release_sec <= 600:
        return 60
    if -7200 <= dt_since_release_sec <= 1800:
        return 300
    return None


def _active_windows(conn, now: datetime) -> list[tuple[str, datetime]]:
    """(series, scheduled_ts) for every registered series whose release is inside
    [T-2h, T+30min] right now."""
    rows = conn.execute(
        "SELECT cal, period, scheduled_ts FROM releases WHERE scheduled_ts BETWEEN ?"
        " AND ?", ((now - timedelta(minutes=30)).isoformat(),
                   (now + timedelta(hours=2)).isoformat())).fetchall()
    out = []
    for r in rows:
        sch = datetime.fromisoformat(r["scheduled_ts"])
        for spec in REGISTRY.values():
            if spec.calendar == r["cal"]:
                out.append((spec.ticker, sch))
    return out


def _drain_freezes(conn) -> int:
    """Cheap clock-only tasks must not wait behind orderbook/FRED requests."""
    due = scheduler.claim_due(conn, tasks=("freeze",))
    for r in due:
        scheduler.set_coverage(conn, r["series"], r["period"], "frozen")
        scheduler.mark_done(conn, r["id"], "ok")
        print(f"  ✓ {r['lane']}/{r['series']}/{r['period']}/freeze: ok")
    return len(due)


def _drain_due(conn, s, md) -> int:
    due = scheduler.claim_due(conn)
    for r in due:
        _drain_freezes(conn)
        # A freeze may already have been handled above, or the watchdog may have
        # expired this row while an earlier task was doing network work.
        status = conn.execute("SELECT status FROM runs WHERE id=?", (r["id"],)).fetchone()
        if status is None or status["status"] not in {"due", "late"}:
            continue
        if scheduler.expire_if_overdue(conn, r):
            continue
        try:
            note = _exec_task(conn, s, md, r)
            status = conn.execute("SELECT status FROM runs WHERE id=?", (r["id"],)).fetchone()
            if status["status"] == "MISSED":
                print(f"  ! {r['task']}: {note}")
                continue
            scheduler.mark_done(conn, r["id"], note)
            print(f"  ✓ {r['lane']}/{r['series']}/{r['period']}/{r['task']}: {note}")
        except Exception as e:                                   # noqa: BLE001
            scheduler.mark_late(conn, r["id"], str(e)[:200])
            print(f"  ✗ {r['task']}: {e}")
    return len(due)


def linger(conn, s, md, max_sec: float | None = None, poll_sec: float = 20.0,
           state: dict | None = None) -> int:
    """Cover the full release window; max_sec is an explicit test/manual bound."""
    t_end = None if max_sec is None else _time.monotonic() + max_sec
    state = state if state is not None else _load_tick_state(s)
    last_snap: dict[str, float] = {}
    pulled: set = set()
    snaps = 0
    while t_end is None or _time.monotonic() < t_end:
        now = datetime.now(timezone.utc)
        wins = _active_windows(conn, now)
        if not wins:
            break
        _drain_freezes(conn)
        for series, sch in wins:
            iv = snap_interval((now - sch).total_seconds())
            if iv is None:
                continue
            if _time.monotonic() - last_snap.get(series, -1e9) >= iv:
                try:
                    _drain_freezes(conn)
                    md.snapshot_series(series)
                    snaps += 1
                    last_snap[series] = _time.monotonic()
                except Exception as e:                           # noqa: BLE001
                    print(f"  ! densified snapshot {series}: {e}")
                finally:
                    _drain_freezes(conn)
        now = datetime.now(timezone.utc)
        _post_release_fred_pulls(conn, s, wins, now, pulled)
        _drain_due(conn, s, md)          # T+3m reassess executes the minute it's due
        _maintenance(conn, s, md, state, event_window=True)
        _time.sleep(poll_sec)
    return snaps


# (B) 2026-09-06: a print landed in fred_obs ~20h after release (PAYEMS/UNRATE 09-04
# 12:30Z, first_seen 09-05 09:00) because nothing re-pulled FRED between the morning
# refresh and the next one — the tick densified QUOTES around the release but not the
# fundamentals. The Fed model's du12 reads UNRATE, so on the day of the biggest macro
# print it repriced a day late. Pull at +2 min (before the T+3m reassess, which runs
# predict_all) and again at +15 min (FRED occasionally posts late). pull_core is INSERT
# OR IGNORE, so a repeat across tick processes is harmless.
FRED_PULL_THRESHOLDS_S = (120, 900)


def due_fred_pulls(dt_since_release_sec: float, done: set, key: tuple) -> list[int]:
    """Thresholds (seconds after release) that should fire now and have not yet fired
    for `key` in this process. Pure, so it is unit-testable."""
    out = []
    for thr in FRED_PULL_THRESHOLDS_S:
        if dt_since_release_sec >= thr and (key, thr) not in done:
            out.append(thr)
    return out


def _post_release_fred_pulls(conn, s, wins, now, pulled: set) -> None:
    from prediction_market_macro.ingest.fred import CORE_SIDS, FredPIT
    # A CPI calendar has four series, but only two underlying FRED releases. Pull
    # each affected source once per threshold rather than reloading all 17 sources
    # up to eight times while the T+3m reassessment waits in this same thread.
    for series, sch in wins:
        sid = REGISTRY[series].fred_first_release
        if sid not in CORE_SIDS:
            continue
        dt = (now - sch).total_seconds()
        key = (sid, sch.isoformat())
        for thr in due_fred_pulls(dt, pulled, key):
            pulled.add((key, thr))
            try:
                _drain_freezes(conn)
                got = FredPIT(s.fred_api_key, conn).pull(sid)
                _drain_freezes(conn)
                print(f"  fred post-release pull +{thr}s ({sid}): {got} rows")
            except Exception as e:                               # noqa: BLE001
                print(f"  ! fred post-release pull +{thr}s ({sid}): {e}")
            finally:
                _drain_freezes(conn)


def main():
    s = load_settings()
    from prediction_market_macro.ops.refresh import RefreshBusy, _single_instance
    try:
        with _single_instance(s.output_dir, "tick.lock"):
            conn = init_db(s.db_path)
            try:
                md = KalshiMD(conn)
                md.on_progress = lambda: _drain_freezes(conn)
                now = datetime.now(timezone.utc)
                print(f"[tick] {now.isoformat()}")
                state = _load_tick_state(s)
                if _active_windows(conn, datetime.now(timezone.utc)):
                    n = linger(conn, s, md, state=state)
                    print(f"[tick] event-window linger done, densified snapshots={n}")
                else:
                    _drain_due(conn, s, md)
                    _maintenance(conn, s, md, state)
            finally:
                conn.close()
    except RefreshBusy:
        print("[tick] another tick holds the lock — skipping")


def _load_tick_state(s) -> dict:
    try:
        state = json.loads((s.output_dir / "tick_state.json").read_text())
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_tick_state(s, state) -> None:
    fd, path = tempfile.mkstemp(prefix=".tick_state-", dir=s.output_dir)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(state, out)
        os.replace(path, s.output_dir / "tick_state.json")
    finally:
        if os.path.exists(path):
            os.unlink(path)


def _due(state, key, now, seconds):
    try:
        age = (now - datetime.fromisoformat(state[key])).total_seconds()
        return age < 0 or age >= seconds
    except (KeyError, TypeError, ValueError):
        return True


def _maintenance(conn, s, md, state, *, event_window=False) -> None:
    """The minute scheduler does not turn every minute into a full ingestion run.

    Ordinary held-book maintenance is 15 minutes, accelerated to one minute during
    release windows. Persistent timestamps keep this cadence across launchd starts.
    """
    now = datetime.now(timezone.utc)
    if _due(state, "positions", now, 60 if event_window else 900):
        try:
            _maintain_positions(conn, s, md)
        except Exception as exc:
            print(f"  ! position maintenance: {exc}")
        state["positions"] = now.isoformat()
        _save_tick_state(s, state)
    if not _due(state, "aux", now, 900):
        return
    # (A) same-day Treasury curve — lands the day's DGS2/5/10/30 within ~15 min of
    # Treasury posting instead of FRED's next-business-day copy (ingest/treasury.py).
    try:
        from prediction_market_macro.ingest import treasury
        got = treasury.pull_if_due(conn, now)
        if got:
            print(f"  treasury same-day pull: {got}")
    except Exception as e:                                       # noqa: BLE001
        print(f"  ! treasury pull: {e}")
    # (C) late futures bars — yfinance drops the last completed session roughly one day
    # in eight (08-03, 08-28, 09-03 since August, all four roots, always caught up a day
    # later). The morning refresh cannot retry; the tick can.
    try:
        _repull_late_futures(conn, s, now)
    except Exception as e:                                       # noqa: BLE001
        print(f"  ! futures re-pull: {e}")
    state["aux"] = now.isoformat()
    _save_tick_state(s, state)


def _maintain_positions(conn, s, md) -> None:
    """Refresh held legs before applying the existing paper exit/mark policies."""
    from prediction_market_macro.ops import exits, ledger, pnl, predict_all, trading_kalshi
    positions = ledger.open_positions(conn)
    if not positions:
        return
    tickers = {f["ticker"] for p in positions for f in p["fills"]}
    result = md.snapshot_tickers(tickers, before_each=lambda: _drain_freezes(conn))
    if result["failed"]:
        print(f"  ! held quote refresh: {result['failed']}")
    _drain_freezes(conn)
    try:
        predict_all.run(conn, s, only_series={p["series"] for p in positions},
                        update_coverage=False, fail_on_error=True)
        # A failed metadata/quote request must not turn a still-recent cached book
        # into an exit. A multi-leg position needs every leg refreshed this cycle.
        eligible = set(result["refreshed"])
        exits.shadow_run(conn, s, eligible_tickers=eligible)
        exits.run(conn, s, eligible_tickers=eligible)
    finally:
        # Even if a model/exit fails, publish missing/stale marks honestly.
        pnl.mark_all(conn)
        try:
            trading_kalshi.sync(conn)
        except Exception as exc:
            print(f"  ! trading_kalshi.sync: {exc}")
        _export_frontend(conn, s)
    print(f"[positions] {datetime.now(timezone.utc).isoformat()}"
          f" refreshed={len(result['refreshed'])} failed={len(result['failed'])}")


_FUT_ROOTS_WATCHED = ("CL", "NG", "RB", "GC")
FUT_REPULL_MAX_ATTEMPTS = 3          # per session date; a holiday must not spin forever
FUT_REPULL_MIN_GAP_S = 2 * 3600


def last_completed_session(now: datetime):
    """The most recent weekday strictly before today's UTC date (the bar that a morning
    pull should already hold). Weekends roll back to Friday; exchange holidays are not
    modelled — the attempt cap absorbs them."""
    from datetime import timedelta
    d = now.date() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def late_futures_roots(conn, now: datetime) -> list[str]:
    """Roots whose newest stored bar is older than the last completed session."""
    if now.weekday() >= 5 or now.hour < 9:
        return []                       # weekend / before the morning refresh has run
    want = last_completed_session(now).isoformat()
    out = []
    for root in _FUT_ROOTS_WATCHED:
        r = conn.execute("SELECT MAX(event_time) FROM fut_daily WHERE root=?",
                         (root,)).fetchone()
        if r is None or r[0] is None or r[0] < want:
            out.append(root)
    return out


def _repull_late_futures(conn, s, now: datetime) -> None:
    import json
    roots = late_futures_roots(conn, now)
    if not roots:
        return
    marker = s.output_dir / "futures_repull.json"
    want = last_completed_session(now).isoformat()
    st = {}
    try:
        st = json.loads(marker.read_text())
    except Exception:                                            # noqa: BLE001
        st = {}
    if st.get("date") != want:
        st = {"date": want, "attempts": 0, "last_ts": None}
    if st["attempts"] >= FUT_REPULL_MAX_ATTEMPTS:
        return
    if st.get("last_ts"):
        try:
            if (now - datetime.fromisoformat(st["last_ts"])).total_seconds() < FUT_REPULL_MIN_GAP_S:
                return
        except ValueError:
            pass
    from prediction_market_macro.ingest import market_data
    n = market_data.pull_futures(conn, roots=roots, lookback_days=10)
    st["attempts"] += 1
    st["last_ts"] = now.isoformat()
    marker.write_text(json.dumps(st))
    still = late_futures_roots(conn, now)
    print(f"  futures re-pull for {roots} (missing {want}): {n} rows; still missing: {still or 'none'}")


if __name__ == "__main__":
    main()
