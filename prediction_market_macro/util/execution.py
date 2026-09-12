"""Serialize short ledger mutation passes across refresh, tick and manual runs.

Research, market ingestion and full refreshes do not hold this lock. The lock starts
before reading open positions/risk caps, so two writers cannot both act on the same
old book. The kernel releases it on process exit. Nested helpers are reentrant.
"""
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
import fcntl
import threading

_guard = threading.Lock()
_locks = {}
_local = threading.local()


class ExecutionBusy(RuntimeError):
    pass


@contextmanager
def execution_lock(conn):
    path = next((r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"), "")
    key = str(Path(path).resolve()) if path else f"memory:{id(conn)}"
    with _guard:
        local_lock = _locks.setdefault(key, threading.RLock())
    if not local_lock.acquire(blocking=not conn.in_transaction):
        raise ExecutionBusy("another ledger pass is running")
    try:
        held = getattr(_local, "held", set())
        if key in held or not path:
            yield
            return
        with open(key + ".execution.lock", "a") as fd:
            # Waiting while owning a SQLite write transaction would invert the lock
            # order against another executor. Such a caller retries its own unit.
            flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if conn.in_transaction else 0)
            try:
                fcntl.flock(fd.fileno(), flags)
            except BlockingIOError as exc:
                raise ExecutionBusy("another ledger pass is running") from exc
            _local.held = held | {key}
            try:
                yield
            finally:
                _local.held = held
                fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
    finally:
        local_lock.release()


def serialized_execution(fn):
    @wraps(fn)
    def wrapped(conn, *args, **kwargs):
        with execution_lock(conn):
            return fn(conn, *args, **kwargs)
    return wrapped
