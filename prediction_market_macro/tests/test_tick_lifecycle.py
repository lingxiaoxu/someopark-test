"""Tick lifecycle and timing regressions, with a simulated clock and temporary DBs."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.jobs import scheduler, tick
from prediction_market_macro.ops.refresh import _single_instance

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.elapsed = 0.0

    def now(self):
        return NOW + timedelta(seconds=self.elapsed)

    def sleep(self, seconds):
        self.elapsed += seconds


@pytest.fixture()
def clock(monkeypatch):
    value = Clock()

    class DateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return value.now()

    monkeypatch.setattr(tick, "datetime", DateTime)
    monkeypatch.setattr(scheduler, "datetime", DateTime)
    monkeypatch.setattr(tick, "_time", SimpleNamespace(
        monotonic=lambda: value.elapsed, sleep=value.sleep))
    return value


@pytest.fixture()
def settings(tmp_path):
    return SimpleNamespace(output_dir=tmp_path, db_path=tmp_path / "tick.db")


def quiet_linger(monkeypatch):
    monkeypatch.setattr(tick, "_drain_due", lambda *args: 0)
    monkeypatch.setattr(tick, "_drain_freezes", lambda *args: 0)
    monkeypatch.setattr(tick, "_post_release_fred_pulls", lambda *args: None)
    monkeypatch.setattr(tick, "_maintenance", lambda *args, **kwargs: None)


def test_default_linger_covers_window_past_old_840_second_cutoff(clock, settings, monkeypatch):
    quiet_linger(monkeypatch)
    monkeypatch.setattr(tick, "_active_windows", lambda *args:
                        [("KXCPI", NOW + timedelta(hours=1))] if clock.elapsed < 1000 else [])
    snapshots = []
    md = SimpleNamespace(snapshot_series=lambda _: snapshots.append(clock.elapsed))
    assert tick.linger(None, settings, md, state={}) == 4
    assert snapshots == [0, 300, 600, 900]
    assert clock.elapsed == 1000


def test_explicit_linger_limit_still_bounds_manual_or_test_runs(clock, settings, monkeypatch):
    quiet_linger(monkeypatch)
    monkeypatch.setattr(tick, "_active_windows", lambda *args:
                        [("KXCPI", NOW + timedelta(hours=1))])
    snapshots = []
    md = SimpleNamespace(snapshot_series=lambda _: snapshots.append(clock.elapsed))
    assert tick.linger(None, settings, md, max_sec=80, state={}) == 1
    assert clock.elapsed == 80


@pytest.mark.parametrize("fails", [False, True])
def test_freezes_are_drained_before_and_after_slow_snapshot(clock, settings, monkeypatch, fails):
    quiet_linger(monkeypatch)
    monkeypatch.setattr(tick, "_active_windows", lambda *args:
                        [("KXCPI", NOW + timedelta(hours=1))])
    calls = []
    monkeypatch.setattr(tick, "_drain_freezes", lambda *args:
                        calls.append(("freeze", clock.elapsed)))

    def snapshot(series):
        calls.append(("snapshot", clock.elapsed))
        clock.sleep(5)
        if fails:
            raise RuntimeError("venue timeout")

    tick.linger(None, settings, SimpleNamespace(snapshot_series=snapshot), max_sec=10, state={})
    snapshot_index = calls.index(("snapshot", 0))
    assert calls[snapshot_index - 1] == ("freeze", 0)
    assert calls[snapshot_index + 1] == ("freeze", 5)


def test_main_refuses_a_second_tick_before_opening_database(settings, monkeypatch, capsys):
    monkeypatch.setattr(tick, "load_settings", lambda: settings)
    reached_db = []
    monkeypatch.setattr(tick, "init_db", lambda _: reached_db.append(True))
    with _single_instance(settings.output_dir, "tick.lock"):
        tick.main()
    assert reached_db == []
    assert "another tick holds the lock" in capsys.readouterr().out


def add_run(conn, task, due):
    cur = conn.execute(
        "INSERT INTO runs(lane,series,period,task,due_ts) VALUES(?,?,?,?,?)",
        ("release_day", "KXCPI", "2026-08", task, due.isoformat()))
    conn.commit()
    return cur.lastrowid


def test_drain_rechecks_deadline_after_previous_task_took_too_long(clock, settings, monkeypatch):
    conn = init_db(settings.db_path)
    slow = add_run(conn, "snapshot", NOW - timedelta(minutes=30))
    decision = add_run(conn, "decide", NOW - timedelta(minutes=29))
    calls = []

    def execute(conn, s, md, run):
        calls.append(run["id"])
        clock.sleep(120)
        return "ok"

    monkeypatch.setattr(tick, "_exec_task", execute)
    tick._drain_due(conn, settings, None)
    assert calls == [slow]
    assert conn.execute("SELECT status FROM runs WHERE id=?", (decision,)).fetchone()[0] == "MISSED"


def test_own_snapshot_can_expire_decision_without_marking_it_done(clock, settings, monkeypatch):
    from prediction_market_macro.ops import decide_all, exits, pnl, predict_all, trading_kalshi
    conn = init_db(settings.db_path)
    decision = add_run(conn, "decide", NOW - timedelta(minutes=29))
    calls = []
    monkeypatch.setattr(tick, "_top_up_stale_quotes", lambda *args: {})
    monkeypatch.setattr(tick, "_export_frontend", lambda *args: None)
    monkeypatch.setattr(predict_all, "run", lambda *args, **kwargs: calls.append("predict"))
    monkeypatch.setattr(decide_all, "run", lambda *args: calls.append("decide"))
    monkeypatch.setattr(exits, "run", lambda *args: calls.append("exit"))
    monkeypatch.setattr(exits, "shadow_run", lambda *args: calls.append("shadow"))
    monkeypatch.setattr(pnl, "mark_all", lambda *args: calls.append("mark"))
    monkeypatch.setattr(trading_kalshi, "sync", lambda *args: calls.append("sync"))
    md = SimpleNamespace(snapshot_series=lambda _: clock.sleep(120))
    tick._drain_due(conn, settings, md)
    assert "decide" not in calls
    assert "exit" not in calls
    assert conn.execute("SELECT status FROM runs WHERE id=?", (decision,)).fetchone()[0] == "MISSED"


def test_waiting_for_execution_lock_can_expire_a_decision(clock, settings, monkeypatch):
    from prediction_market_macro.ops import decide_all, exits, pnl, predict_all, trading_kalshi
    from prediction_market_macro.util.execution import execution_lock
    conn = init_db(settings.db_path)
    decision = add_run(conn, "decide", NOW - timedelta(minutes=29))
    calls = []
    monkeypatch.setattr(tick, "_top_up_stale_quotes", lambda *args: {})
    monkeypatch.setattr(tick, "_export_frontend", lambda *args: None)
    monkeypatch.setattr(predict_all, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(decide_all, "run", lambda *args: calls.append("decide"))
    monkeypatch.setattr(exits, "run", lambda *args: None)
    monkeypatch.setattr(exits, "shadow_run", lambda *args: None)
    monkeypatch.setattr(pnl, "mark_all", lambda *args: None)
    monkeypatch.setattr(trading_kalshi, "sync", lambda *args: None)
    held, attempting = threading.Event(), threading.Event()
    errors = []

    def other_pass():
        other = sqlite3.connect(settings.db_path)
        try:
            with execution_lock(other):
                held.set()
                assert attempting.wait(5)
                # The tick was eligible before waiting. Move the simulated clock
                # while a real lock from another connection still excludes it.
                clock.sleep(120)
        except BaseException as exc:
            errors.append(exc)
        finally:
            other.close()

    @contextmanager
    def observed_lock(db):
        attempting.set()
        with execution_lock(db):
            yield

    monkeypatch.setattr(tick, "execution_lock", observed_lock)
    holder = threading.Thread(target=other_pass, daemon=True)
    holder.start()
    try:
        assert held.wait(5)
        tick._drain_due(conn, settings, SimpleNamespace(snapshot_series=lambda _: None))
    finally:
        attempting.set()
        holder.join(5)
    assert not holder.is_alive() and errors == []
    assert calls == []
    row = conn.execute("SELECT status,done_ts FROM runs WHERE id=?", (decision,)).fetchone()
    assert row["status"] == "MISSED" and row["done_ts"] is None


def test_post_release_pull_uses_clock_after_slow_snapshot_before_reassess(
        clock, settings, monkeypatch):
    quiet_linger(monkeypatch)
    # Start at T+1:50; a slow quote request crosses both the +2m FRED pull and
    # +3m reassessment. The pull must see the new time before full task draining.
    scheduled = NOW - timedelta(seconds=110)
    monkeypatch.setattr(tick, "_active_windows", lambda *args: [("KXCPI", scheduled)])
    calls = []

    def pull(conn, s, wins, now, pulled):
        calls.append(("fred", (now - scheduled).total_seconds()))

    monkeypatch.setattr(tick, "_post_release_fred_pulls", pull)
    monkeypatch.setattr(tick, "_drain_due", lambda *args: calls.append(("reassess", clock.elapsed)))
    md = SimpleNamespace(snapshot_series=lambda _: clock.sleep(80))
    tick.linger(None, settings, md, max_sec=1, state={})
    assert calls[0] == ("fred", 190)
    assert calls[1] == ("reassess", 80)


def test_due_freeze_is_recorded_before_slow_work_starts(clock, settings, monkeypatch):
    conn = init_db(settings.db_path)
    add_run(conn, "snapshot", NOW - timedelta(minutes=5))
    freeze = add_run(conn, "freeze", NOW - timedelta(seconds=1))
    states = []

    def execute(conn, s, md, run):
        states.append(conn.execute("SELECT status FROM runs WHERE id=?", (freeze,)).fetchone()[0])
        return "ok"

    monkeypatch.setattr(tick, "_exec_task", execute)
    tick._drain_due(conn, settings, None)
    assert states == ["done"]
    assert conn.execute("SELECT state FROM coverage").fetchone()[0] == "frozen"


def test_maintenance_cadence_survives_restart_and_accelerates_only_positions(
        clock, settings, monkeypatch):
    from prediction_market_macro.ingest import treasury
    positions, treasury_calls, futures = [], [], []
    monkeypatch.setattr(tick, "_maintain_positions", lambda *args: positions.append(clock.elapsed))
    monkeypatch.setattr(treasury, "pull_if_due", lambda *args: treasury_calls.append(clock.elapsed))
    monkeypatch.setattr(tick, "_repull_late_futures", lambda *args: futures.append(clock.elapsed))

    tick._maintenance(None, settings, None, {})
    assert positions == treasury_calls == futures == [0]
    clock.sleep(60)
    tick._maintenance(None, settings, None, tick._load_tick_state(settings))
    assert positions == [0]
    tick._maintenance(None, settings, None, tick._load_tick_state(settings), event_window=True)
    assert positions == [0, 60]
    assert treasury_calls == futures == [0]
    clock.sleep(59)
    tick._maintenance(None, settings, None, tick._load_tick_state(settings), event_window=True)
    assert positions == [0, 60]
    clock.sleep(1)
    tick._maintenance(None, settings, None, tick._load_tick_state(settings), event_window=True)
    assert positions == [0, 60, 120]
    clock.elapsed = 900
    tick._maintenance(None, settings, None, tick._load_tick_state(settings))
    assert positions == [0, 60, 120]             # ordinary: last positions + 900s
    assert treasury_calls == futures == [0, 900]
    clock.elapsed = 1020
    tick._maintenance(None, settings, None, tick._load_tick_state(settings))
    assert positions == [0, 60, 120, 1020]
    saved = tick._load_tick_state(settings)
    assert datetime.fromisoformat(saved["positions"]) == clock.now()
    assert datetime.fromisoformat(saved["aux"]) == NOW + timedelta(seconds=900)
    assert not list(settings.output_dir.glob(".tick_state-*"))


def test_bad_state_file_recovers_without_suppressing_maintenance(settings):
    path = settings.output_dir / "tick_state.json"
    for bad in ("broken json", "[]", "null"):
        path.write_text(bad)
        assert tick._load_tick_state(settings) == {}


def test_clock_rollback_does_not_suppress_maintenance(settings):
    state = {"positions": (NOW + timedelta(hours=1)).isoformat()}
    tick._save_tick_state(settings, state)
    assert tick._due(tick._load_tick_state(settings), "positions", NOW, 900)
