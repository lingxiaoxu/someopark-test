"""Two full refreshes must never run at once.

Regression for 2026-08-20. `refresh_last.json` is written by the last line of
`refresh._run`, and jobs/tick.py decides "has today's refresh happened?" from its `ts`.
The daily_refresh run is materialised at 09:00:00Z — the same instant the launchd refresh
fires — so the first tick after 09:00 lands inside the ~17-minute window where the stamp
still reads yesterday, and started a second concurrent full pass. Three days running
(08-18/19/20); 08-18's collided outright with `sqlite3.OperationalError: database is
locked` on step 1.

The damage is behavioural, not just duplicated CPU: `_run` calls decide_all BEFORE exits,
so one pass can never re-enter a position it closed in the same cycle. The second pass
ran decide_all 13 s after the first pass's exits had flattened KXNATGASW, found
`already_open_no_averaging_down` lifted, and opened a same-day reversal leg (decision
7389, YES T2.799 @0.41) that no single pass could have taken.
"""
import multiprocessing as mp
import time

import pytest

from prediction_market_macro.ops import refresh


def _hold(output_dir, started, release):
    with refresh._single_instance(output_dir):
        started.set()
        release.wait(30)


def test_uncontended_acquire_is_reentrant_across_calls(tmp_path):
    for _ in range(3):
        with refresh._single_instance(tmp_path):
            pass
    assert (tmp_path / "refresh.lock").exists()


def test_second_caller_is_refused_not_queued(tmp_path):
    ctx = mp.get_context("fork")
    started, release = ctx.Event(), ctx.Event()
    p = ctx.Process(target=_hold, args=(tmp_path, started, release))
    p.start()
    try:
        assert started.wait(30), "holder never acquired"
        t0 = time.monotonic()
        with pytest.raises(refresh.RefreshBusy) as ei:
            with refresh._single_instance(tmp_path):
                pass
        # refused immediately — a blocking flock would silently serialise the two passes
        # instead of cancelling the second, which is the same duplicate work
        assert time.monotonic() - t0 < 2.0
        assert "pid=" in str(ei.value)                    # holder is identifiable
    finally:
        release.set()
        p.join(30)


def test_killed_holder_does_not_wedge_the_daily_job(tmp_path):
    """flock dies with the fd, so a SIGKILLed refresh leaves no stale lock."""
    ctx = mp.get_context("fork")
    p = ctx.Process(target=_hold, args=(tmp_path, ctx.Event(), ctx.Event()))
    p.start()
    time.sleep(0.5)
    p.kill()
    p.join(30)
    with refresh._single_instance(tmp_path):
        pass


def test_run_is_gated_by_the_lock(tmp_path, monkeypatch):
    """run() must not reach _run while another refresh holds the lock."""
    calls = []
    monkeypatch.setattr(refresh, "_run", lambda weekly=False: calls.append(weekly))
    monkeypatch.setattr(refresh, "load_settings",
                        lambda *a, **k: type("S", (), {"output_dir": tmp_path})())

    ctx = mp.get_context("fork")
    started, release = ctx.Event(), ctx.Event()
    p = ctx.Process(target=_hold, args=(tmp_path, started, release))
    p.start()
    try:
        assert started.wait(30)
        with pytest.raises(refresh.RefreshBusy):
            refresh.run()
        assert calls == [], "a second full refresh executed"
    finally:
        release.set()
        p.join(30)

    refresh.run()                                         # lock free -> really runs
    assert calls == [False]


def test_tick_marks_the_run_late_rather_than_done(tmp_path, monkeypatch):
    """RefreshBusy must reach _drain_due's handler so the run retries next tick.

    If it were swallowed, the run would be marked done and a genuinely-missed 05:00
    refresh would never be caught up.
    """
    from prediction_market_macro.jobs import tick

    assert issubclass(refresh.RefreshBusy, Exception)
    monkeypatch.setattr(refresh, "run",
                        lambda *a, **k: (_ for _ in ()).throw(refresh.RefreshBusy("busy")))
    s = type("S", (), {"output_dir": tmp_path})()         # no refresh_last.json -> stale
    with pytest.raises(refresh.RefreshBusy):
        tick._exec_task(None, s, None, {"task": "daily_refresh", "series": "*"})
