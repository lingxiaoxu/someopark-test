"""jobs/scheduler.py — never-miss machinery (PLAN §8.2).

runs table = "what SHOULD happen" materialised ahead of time from the calendars;
tick() executes due tasks; watchdog() (separate process) flags overdue work as MISSED.
Decision-class tasks past their window are NEVER back-run (a late decision used the
future); snapshot/reconcile tasks may catch up.

Tasks per (series, period):
  arm        T-24h   release_day lanes: full ingest + draft predict
  snapshot   T-2h    snapshot at the T-2h mark; tick.linger() then densifies inside
                     the event window (5-min snaps, 1-min inside ±10min — §8.1)
  decide     T-1h    final predict + decision window        [decision-class]
  freeze     T-10m   no new entries/exits marker
  reassess   T+3m    post-release re-estimation             [decision-class]
  reconcile  T+1d    settlement reconciliation
Plus daily lane-independent tasks: daily_refresh, health, pred_freshness.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from prediction_market_macro.config.registry import REGISTRY
from prediction_market_macro.ingest import calendars as cal

DECISION_TASKS = {"decide", "reassess"}
_OFFSETS = [("arm", timedelta(hours=-24)), ("snapshot", timedelta(hours=-2)),
            ("decide", timedelta(hours=-1)), ("freeze", timedelta(minutes=-10)),
            ("reassess", timedelta(minutes=3)), ("reconcile", timedelta(days=1))]


def _upsert_run(conn, lane, series, period, task, due_ts) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO runs(lane, series, period, task, due_ts) VALUES(?,?,?,?,?)",
        (lane, series, period, task, due_ts.isoformat()))


def materialize(conn, now: datetime | None = None, horizon_days: int = 30) -> int:
    """Write the next `horizon_days` of expected work into runs + seed coverage rows."""
    now = now or datetime.now(timezone.utc)
    end = now + timedelta(days=horizon_days)
    n = 0
    for spec in REGISTRY.values():
        lane = ("fomc_week" if spec.calendar == "FOMC" else
                "weekly_close" if spec.calendar.endswith("_WEEKLY") else
                "quarterly" if spec.calendar == "BEA_GDP" else "release_day")
        for ev in cal.releases_between(spec.calendar, now - timedelta(days=2), end):
            for task, off in _OFFSETS:
                due = ev.scheduled_ts + off
                # never create work that is already overdue at creation time — only the
                # catch-up-able reconcile task may be materialised into the recent past
                floor_ts = now - timedelta(days=2) if task == "reconcile" else now
                if floor_ts <= due <= end:
                    _upsert_run(conn, lane, spec.ticker, ev.period, task, due)
                    n += 1
            conn.execute(
                "INSERT OR IGNORE INTO coverage(series, period, state, updated_ts)"
                " VALUES(?,?,?,?)",
                (spec.ticker, ev.period, "scheduled", now.isoformat()))
    # daily lane-independent heartbeats for the next horizon
    d = now.replace(hour=9, minute=0, second=0, microsecond=0)   # 05:00 ET == 09:00/10:00 UTC; run marker at 09:00Z
    while d <= end:
        if d > now - timedelta(days=1):
            for task in ("daily_refresh", "health", "pred_freshness"):
                _upsert_run(conn, "daily", "*", d.date().isoformat(), task, d)
                n += 1
        d += timedelta(days=1)
    conn.commit()
    return n


def claim_due(conn, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    rows = conn.execute(
        "SELECT * FROM runs WHERE status IN ('due','late') AND due_ts<=? ORDER BY due_ts",
        (now.isoformat(),)).fetchall()
    return [dict(r) for r in rows]


def mark_done(conn, run_id: int, note: str = "") -> None:
    conn.execute("UPDATE runs SET status='done', done_ts=?, note=? WHERE id=?",
                 (datetime.now(timezone.utc).isoformat(), note, run_id))
    conn.commit()


def mark_late(conn, run_id: int, note: str = "") -> None:
    conn.execute("UPDATE runs SET status='late', note=? WHERE id=?", (note, run_id))
    conn.commit()


def set_coverage(conn, series: str, period: str, state: str) -> None:
    conn.execute(
        "INSERT INTO coverage(series, period, state, updated_ts) VALUES(?,?,?,?) "
        "ON CONFLICT(series, period) DO UPDATE SET state=excluded.state,"
        " updated_ts=excluded.updated_ts",
        (series, period, state, datetime.now(timezone.utc).isoformat()))
    conn.commit()


def watchdog(conn, now: datetime | None = None, grace_min: int = 30) -> list[dict]:
    """Overdue due|late beyond grace → MISSED + alert. Decision tasks are NEVER re-queued.
    Also enforces the 24h pred-freshness SLA (PLAN §8.0)."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(minutes=grace_min)).isoformat()
    missed = []
    for r in conn.execute(
            "SELECT * FROM runs WHERE status IN ('due','late') AND due_ts<?", (cutoff,)):
        conn.execute("UPDATE runs SET status='MISSED' WHERE id=?", (r["id"],))
        msg = f"MISSED {r['lane']}/{r['series']}/{r['period']}/{r['task']} due {r['due_ts']}"
        conn.execute("INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                     (now.isoformat(), "error", "watchdog", msg))
        missed.append(dict(r))
    # pred-freshness SLA: every open (series, period) must have a pred younger than 24h.
    # contracts store the Kalshi token ('26SEP'); preds store the ISO key ('2026-09').
    from prediction_market_macro.util.periods import kalshi_period_to_key
    stale_cut = (now - timedelta(hours=24)).isoformat()
    open_pairs = conn.execute(
        "SELECT DISTINCT series, period FROM contracts WHERE status='active'").fetchall()
    for sp in open_pairs:
        key = kalshi_period_to_key(sp["period"])
        if key is None:
            continue
        r = conn.execute(
            "SELECT MAX(asof) m FROM preds WHERE series=? AND period=?",
            (sp["series"], key)).fetchone()
        if r["m"] is None or r["m"] < stale_cut:
            msg = f"pred-freshness SLA breach {sp['series']}/{key} last={r['m']}"
            conn.execute("INSERT INTO alerts(ts, level, source, message) VALUES(?,?,?,?)",
                         (now.isoformat(), "error", "watchdog", msg))
            missed.append({"series": sp["series"], "period": key, "task": "pred_sla"})
    conn.commit()
    return missed
