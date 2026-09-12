"""Executor deadlines must hold even when the hourly watchdog has not fired."""
from datetime import datetime, timedelta, timezone

import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.jobs import scheduler

NOW = datetime(2026, 9, 4, 12, 34, tzinfo=timezone.utc)


@pytest.fixture()
def conn(tmp_path):
    return init_db(tmp_path / "scheduler.db")


def add_run(conn, task, due, status="due"):
    cur = conn.execute(
        "INSERT INTO runs(lane,series,period,task,due_ts,status) VALUES(?,?,?,?,?,?)",
        ("release_day", "KXCPI", "2026-08", task, due.isoformat(), status))
    conn.commit()
    return dict(conn.execute("SELECT * FROM runs WHERE id=?", (cur.lastrowid,)).fetchone())


@pytest.mark.parametrize("task", ["decide", "reassess"])
@pytest.mark.parametrize("status", ["due", "late"])
def test_expired_decisions_never_reach_executor_before_watchdog(conn, task, status):
    run = add_run(conn, task, NOW - timedelta(minutes=30, microseconds=1), status)
    assert scheduler.claim_due(conn, NOW) == []
    assert conn.execute("SELECT status FROM runs WHERE id=?", (run["id"],)).fetchone()[0] == "MISSED"
    assert conn.execute("SELECT COUNT(*) FROM alerts WHERE source='watchdog'").fetchone()[0] == 1
    assert scheduler.claim_due(conn, NOW) == []
    assert scheduler.expire_if_overdue(conn, run, NOW)
    assert scheduler.watchdog(conn, NOW) == []
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 1


def test_exact_grace_boundary_remains_eligible(conn):
    run = add_run(conn, "decide", NOW - timedelta(minutes=30))
    assert [r["id"] for r in scheduler.claim_due(conn, NOW)] == [run["id"]]
    assert not scheduler.expire_if_overdue(conn, run, NOW)


@pytest.mark.parametrize("task", ["snapshot", "freeze", "arm", "reconcile", "daily_refresh"])
def test_executor_does_not_change_catch_up_semantics(conn, task):
    run = add_run(conn, task, NOW - timedelta(hours=2))
    assert [r["id"] for r in scheduler.claim_due(conn, NOW)] == [run["id"]]
    assert not scheduler.expire_if_overdue(conn, run, NOW)


def test_second_deadline_check_handles_slow_previous_task(conn):
    run = add_run(conn, "reassess", NOW - timedelta(minutes=29))
    fetched = scheduler.claim_due(conn, NOW)
    assert fetched[0]["id"] == run["id"]
    assert scheduler.expire_if_overdue(conn, fetched[0], NOW + timedelta(minutes=2))
    assert conn.execute("SELECT status FROM runs WHERE id=?", (run["id"],)).fetchone()[0] == "MISSED"


def test_freeze_precedes_older_http_work_and_can_be_drained_alone(conn):
    slow = add_run(conn, "snapshot", NOW - timedelta(minutes=15))
    freeze = add_run(conn, "freeze", NOW - timedelta(seconds=1))
    future = add_run(conn, "freeze", NOW + timedelta(minutes=1))
    assert [r["id"] for r in scheduler.claim_due(conn, NOW)] == [freeze["id"], slow["id"]]
    assert [r["id"] for r in scheduler.claim_due(conn, NOW, tasks=("freeze",))] == [freeze["id"]]
    assert scheduler.claim_due(conn, NOW, tasks=()) == []
    scheduler.mark_done(conn, freeze["id"])
    assert [r["id"] for r in scheduler.claim_due(
        conn, NOW + timedelta(minutes=1), tasks=("freeze",))] == [future["id"]]


def test_watchdog_preserves_its_other_task_semantics(conn):
    add_run(conn, "snapshot", NOW - timedelta(minutes=31))
    assert len(scheduler.watchdog(conn, NOW)) == 1
    assert scheduler.watchdog(conn, NOW) == []
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 1
