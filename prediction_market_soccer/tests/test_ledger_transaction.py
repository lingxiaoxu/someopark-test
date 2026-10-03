"""Ledger transactions own the write lock up front, so a concurrent commit cannot fail the append."""
import sqlite3
import threading
import time

import pytest

from prediction_market_soccer.util.frozen_strategy_store import _transaction
from prediction_market_soccer.util.source_history import ObservedConnection


def _connect(path, factory=sqlite3.Connection):
    conn = sqlite3.connect(path, factory=factory, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "ledger.db"
    conn = _connect(path)
    conn.execute("CREATE TABLE book (x INTEGER)")
    conn.commit()
    conn.close()
    return path


def test_append_survives_a_commit_between_its_read_and_write(db):
    appender, live = _connect(db), _connect(db)
    waited = {}

    def live_write():
        started = time.time()
        live.execute("INSERT INTO book VALUES (1)")
        live.commit()
        waited["s"] = time.time() - started

    with _transaction(appender):
        appender.execute("SELECT COUNT(*) FROM book").fetchone()
        writer = threading.Thread(target=live_write)
        writer.start()
        time.sleep(0.3)
        appender.execute("INSERT INTO book VALUES (2)")
    writer.join()
    assert sorted(r[0] for r in appender.execute("SELECT x FROM book")) == [1, 2]
    assert waited["s"] >= 0.25           # the live writer queued behind the ledger
    assert not appender.in_transaction


def test_failure_rolls_back_everything_and_ends_the_transaction(db):
    conn = _connect(db)
    with pytest.raises(RuntimeError):
        with _transaction(conn):
            conn.execute("INSERT INTO book VALUES (7)")
            raise RuntimeError("boom")
    assert conn.execute("SELECT COUNT(*) FROM book").fetchone()[0] == 0
    assert not conn.in_transaction


def test_inside_a_callers_transaction_only_the_savepoint_rolls_back(db):
    conn = _connect(db)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO book VALUES (1)")
    with pytest.raises(RuntimeError):
        with _transaction(conn):
            conn.execute("INSERT INTO book VALUES (2)")
            raise RuntimeError("inner")
    assert conn.in_transaction            # the caller still owns its transaction
    conn.execute("COMMIT")
    assert [r[0] for r in conn.execute("SELECT x FROM book")] == [1]


def test_commit_does_not_finalize_the_callers_pending_source_revisions(db):
    conn = _connect(db, factory=ObservedConnection)
    conn.pending_source_revisions.add("pending-revision")
    batch = conn.source_batch_id
    with _transaction(conn):
        conn.execute("INSERT INTO book VALUES (3)")
    assert conn.pending_source_revisions == {"pending-revision"}
    assert conn.source_batch_id == batch
