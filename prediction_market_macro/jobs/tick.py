"""jobs/tick.py — the 15-min executor of materialised runs (PLAN §8.2-2) + the
event-window densifier (§8.1: T-2h 5-min snapshots, ±10min 1-min fast polling).

launchd fires every 900s. Outside event windows one pass suffices. When a release
window is live ([T-2h, T+30min] for any registered calendar), the process LINGERS
(≤840s, always ending before the next launchd fire) and:
  * snapshots the affected series every 5 min (1 min inside ±10 min of the release)
  * claims newly-due runs mid-linger — so the T+3m reassess task executes ON TIME
    with fresh post-release quotes instead of waiting for the next 15-min fire.

    conda run -n someopark_run python -m prediction_market_macro.jobs.tick
"""
from __future__ import annotations

import json
import time as _time
from datetime import datetime, timedelta, timezone

from prediction_market_macro.config.registry import REGISTRY
from prediction_market_macro.config.settings import load_settings
from prediction_market_macro.ingest.kalshi_md import KalshiMD
from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.jobs import scheduler


def _exec_task(conn, s, md, r) -> str:
    task, series = r["task"], r["series"]
    from prediction_market_macro.ops import decide_all, exits, pnl, predict_all
    if task in ("arm", "snapshot", "reassess", "decide"):
        if series in REGISTRY:
            md.snapshot_series(series)
    if task in ("arm", "decide", "reassess"):
        predict_all.run(conn, s)
        decide_all.run(conn, s)
        exits.run(conn, s)
    if task == "freeze":
        scheduler.set_coverage(conn, series, r["period"], "frozen")
    if task == "reconcile":
        if series in REGISTRY:
            md.sync_settlements(series)
        pnl.settle_pass(conn)
        scheduler.set_coverage(conn, series, r["period"], "reconciled")
    if task in ("daily_refresh", "health", "pred_freshness"):
        last = s.output_dir / "refresh_last.json"
        if last.exists():
            ts = json.loads(last.read_text()).get("ts")
            if ts and datetime.now(timezone.utc) - datetime.fromisoformat(ts) \
                    < timedelta(hours=20):
                return "covered_by_daily_refresh"
        from prediction_market_macro.ops import refresh
        refresh.run()
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


def _drain_due(conn, s, md) -> int:
    due = scheduler.claim_due(conn)
    for r in due:
        try:
            note = _exec_task(conn, s, md, r)
            scheduler.mark_done(conn, r["id"], note)
            print(f"  ✓ {r['lane']}/{r['series']}/{r['period']}/{r['task']}: {note}")
        except Exception as e:                                   # noqa: BLE001
            scheduler.mark_late(conn, r["id"], str(e)[:200])
            print(f"  ✗ {r['task']}: {e}")
    return len(due)


def linger(conn, s, md, max_sec: float = 840.0, poll_sec: float = 20.0) -> int:
    """Densified event-window loop; returns snapshots taken. Always ends before the
    next launchd fire so tick processes never pile up."""
    t_end = _time.monotonic() + max_sec
    last_snap: dict[str, float] = {}
    snaps = 0
    while _time.monotonic() < t_end:
        now = datetime.now(timezone.utc)
        wins = _active_windows(conn, now)
        if not wins:
            break
        for series, sch in wins:
            iv = snap_interval((now - sch).total_seconds())
            if iv is None:
                continue
            if _time.monotonic() - last_snap.get(series, -1e9) >= iv:
                try:
                    md.snapshot_series(series)
                    snaps += 1
                    last_snap[series] = _time.monotonic()
                except Exception as e:                           # noqa: BLE001
                    print(f"  ! densified snapshot {series}: {e}")
        _drain_due(conn, s, md)          # T+3m reassess executes the minute it's due
        _time.sleep(poll_sec)
    return snaps


def main():
    s = load_settings()
    conn = init_db(s.db_path)
    md = KalshiMD(conn)
    now = datetime.now(timezone.utc)
    print(f"[tick] {now.isoformat()}")
    _drain_due(conn, s, md)
    # intraday price track (§15 mother port): mark open positions every fire
    try:
        from prediction_market_macro.ops import pnl
        pnl.mark_all(conn)
    except Exception as e:                                       # noqa: BLE001
        print(f"  ! mark_all: {e}")
    if _active_windows(conn, now):
        n = linger(conn, s, md)
        print(f"[tick] event-window linger done, densified snapshots={n}")


if __name__ == "__main__":
    main()
