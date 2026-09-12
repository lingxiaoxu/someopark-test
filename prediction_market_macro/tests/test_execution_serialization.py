"""Concurrent ledger passes must observe the preceding close before acting.

All databases and file locks are temporary. Mirror hooks are stubbed; no network.
"""
from datetime import datetime, timedelta, timezone
import multiprocessing as mp
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.ops import exits, risk, trading_kalshi
from prediction_market_macro.util.execution import ExecutionBusy, execution_lock, serialized_execution


def connection(path, **kwargs):
    conn = sqlite3.connect(path, timeout=2, **kwargs)
    conn.row_factory = sqlite3.Row
    return conn


def seed_position(conn):
    now = datetime.now(timezone.utc)
    cur = conn.execute(
        "INSERT INTO decisions(ts_utc,series,period,structure_json,kind,size_usd,"
        "inputs_json,model_version,gate_snapshot,note) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (now.isoformat(), "KXCPI", "2026-08", '{"kind":"bucket"}', "open", 1,
         "{}", "cpi/test", "{}", ""))
    opened = cur.lastrowid
    for ticker, side in (("CPI-LOW", "yes"), ("CPI-HIGH", "no")):
        conn.execute(
            "INSERT INTO fills(decision_id,ts_utc,ticker,side,price,count,fee_usd,mode)"
            " VALUES(?,?,?,?,?,?,?,?)", (opened, now.isoformat(), ticker, side, .5, 1, .01, "paper"))
        conn.execute(
            "INSERT INTO contracts(ticker,series,event_ticker,period,status,close_time,first_seen_ts)"
            " VALUES(?,?,?,?,?,?,?)", (ticker, "KXCPI", "CPI-EVENT", "26AUG", "active",
                                      (now + timedelta(days=1)).isoformat(), now.isoformat()))
        conn.execute("INSERT INTO quotes VALUES(?,?,?,?,?,?)",
                     (now.isoformat(), ticker, .60, .62, 200, 200))
    conn.commit()
    return opened


def test_two_connections_close_a_position_only_once(tmp_path, monkeypatch):
    path = tmp_path / "ledger.db"
    conn = init_db(path)
    opened = seed_position(conn)
    mirrored = []
    monkeypatch.setattr(trading_kalshi, "on_fill", lambda conn, fid: mirrored.append(fid))
    monkeypatch.setattr(risk, "breaker_tripped", lambda *args: "test breaker")
    first_read, second_started, second_read, release_first = [threading.Event() for _ in range(4)]
    original = exits.open_positions

    def observed_open_positions(conn):
        rows = original(conn)
        if threading.current_thread().name == "first-exit":
            first_read.set()
            assert release_first.wait(5)
        else:
            second_read.set()
        return rows

    monkeypatch.setattr(exits, "open_positions", observed_open_positions)
    results, errors = [], []

    def worker(second=False):
        db = connection(path)
        try:
            if second:
                second_started.set()
            results.append(exits.run(db, SimpleNamespace()))
        except BaseException as exc:
            errors.append(exc)
        finally:
            db.close()

    first = threading.Thread(target=worker, name="first-exit", daemon=True)
    second = threading.Thread(target=worker, kwargs={"second": True}, name="second-exit", daemon=True)
    first.start()
    try:
        assert first_read.wait(5)
        second.start()
        assert second_started.wait(5)
        # The second caller has reached exits.run, but it must not read the stale
        # open book while the first caller is still acting on it.
        assert not second_read.wait(.1)
    finally:
        release_first.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert sorted(results) == [0, 1]
    rows = conn.execute("SELECT id FROM decisions WHERE kind='exit' AND closes_decision_id=?",
                        (opened,)).fetchall()
    assert len(rows) == 1
    assert conn.execute("SELECT COUNT(*) FROM fills WHERE decision_id=?", (rows[0]["id"],)).fetchone()[0] == 2
    assert len(mirrored) == 2


def test_nested_execution_is_reentrant_and_exception_releases_lock(tmp_path):
    conn = init_db(tmp_path / "nested.db")

    @serialized_execution
    def inner(db):
        db.execute("INSERT INTO alerts(ts,level,source,message) VALUES('now','info','test','nested')")
        db.commit()

    with execution_lock(conn):
        with execution_lock(conn):
            inner(conn)
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 1
    with pytest.raises(ValueError):
        with execution_lock(conn):
            raise ValueError("abort")
    with execution_lock(conn):
        inner(conn)
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 2


def test_transaction_holder_never_waits_for_another_thread_execution_lock(tmp_path):
    path = tmp_path / "order.db"
    seed = init_db(path)
    seed.close()
    held, release, done = [threading.Event() for _ in range(3)]
    errors = []

    def holder():
        db = connection(path)
        try:
            with execution_lock(db):
                held.set()
                assert release.wait(5)
        finally:
            db.close()

    locked = threading.Thread(target=holder, daemon=True)
    locked.start()
    assert held.wait(5)
    transactional = connection(path, check_same_thread=False)
    transactional.execute("BEGIN IMMEDIATE")

    def contender():
        try:
            with execution_lock(transactional):
                errors.append("incorrectly acquired")
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    blocked = threading.Thread(target=contender, daemon=True)
    blocked.start()
    try:
        # It must fail promptly, allowing its caller to roll back the DB lock.
        # Waiting for RLock here creates the same inversion as waiting for flock.
        assert done.wait(.5), "transaction holder blocked on the thread lock"
        assert len(errors) == 1 and isinstance(errors[0], ExecutionBusy)
    finally:
        transactional.rollback()
        release.set()
        locked.join(5)
        blocked.join(5)
        transactional.close()
    assert not locked.is_alive() and not blocked.is_alive()


def hold_process_lock(path, ready, release):
    conn = connection(path)
    try:
        with execution_lock(conn):
            ready.set()
            release.wait(10)
    finally:
        conn.close()


def test_transaction_holder_never_waits_for_another_process_file_lock(tmp_path):
    path = tmp_path / "process.db"
    conn = init_db(path)
    ctx = mp.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    process = ctx.Process(target=hold_process_lock, args=(path, ready, release))
    process.start()
    try:
        assert ready.wait(5)
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(ExecutionBusy):
            with execution_lock(conn):
                pytest.fail("entered another process's execution lock")
        conn.rollback()
    finally:
        conn.rollback()
        release.set()
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join(5)
    assert process.exitcode == 0
    with execution_lock(conn):
        pass
