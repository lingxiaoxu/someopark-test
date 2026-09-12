"""Soccer-only admission gate shared by ordinary writers and version maintenance.

Routine writers hold shared kernel locks. Maintenance first persists an admission
stop, then takes the exclusive lock. A crash keeps admission closed until explicit
reconciliation; a stale PID never proves that outside orders failed.
"""
from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
import fcntl
import json
import os
from pathlib import Path
import threading


class MaintenanceBusy(RuntimeError):
    pass


_local = threading.local()


def _paths(root):
    if root is None:
        from prediction_market_soccer.config import CONFIG
        root = CONFIG.paths.data
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root, root / '.soccer-writers.lock', root / '.soccer-maintenance.json'


def _held():
    if not hasattr(_local, 'held'):
        _local.held = {}
    return _local.held


def maintenance_active(*, root=None):
    root, _, _ = _paths(root)
    return _held().get(str(root), {}).get('mode') == 'exclusive'


@contextmanager
def writer_gate(*, root=None):
    root, lock, stop = _paths(root)
    key, held = str(root), _held()
    if key in held:
        yield
        return
    if stop.exists():
        raise MaintenanceBusy('Soccer version maintenance is awaiting reconciliation')
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MaintenanceBusy('Soccer maintenance owns the writer gate') from exc
        if stop.exists():
            raise MaintenanceBusy('Soccer maintenance closed writer admission')
        held[key] = {'mode': 'shared', 'fd': fd}
        yield
    finally:
        held.pop(key, None)
        os.close(fd)


def writer(fn):
    @wraps(fn)
    def guarded(*args, **kwargs):
        with writer_gate():
            return fn(*args, **kwargs)
    return guarded


@contextmanager
def maintenance_gate(operation_id, *, root=None):
    root, lock, stop = _paths(root)
    key, held = str(root), _held()
    if not isinstance(operation_id, str) or not operation_id or len(operation_id) > 128:
        raise ValueError('A stable maintenance operation identity is required')
    if key in held:
        if held[key]['mode'] != 'exclusive' or held[key]['operation_id'] != operation_id:
            raise MaintenanceBusy('A writer cannot upgrade or nest another maintenance operation')
        yield
        return
    payload = json.dumps({'operation_id': operation_id}, sort_keys=True).encode()
    try:
        out = os.open(stop, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            active = json.loads(stop.read_text())
        except (OSError, ValueError) as exc:
            raise MaintenanceBusy('Invalid maintenance marker requires explicit recovery') from exc
        if active != {'operation_id': operation_id}:
            raise MaintenanceBusy('Another maintenance operation requires recovery')
    else:
        with os.fdopen(out, 'wb') as stream:
            stream.write(payload); stream.flush(); os.fsync(stream.fileno())
        dirfd = os.open(root, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MaintenanceBusy('Existing writers must finish; retry this same operation') from exc
        held[key] = {'mode': 'exclusive', 'fd': fd, 'operation_id': operation_id}
        yield
    finally:
        held.pop(key, None)
        os.close(fd)


def finish_maintenance(operation_id, *, root=None):
    """Only after a verified completed operation or verified full restoration."""
    root, _, stop = _paths(root)
    current = _held().get(str(root))
    if not current or current['mode'] != 'exclusive' or current['operation_id'] != operation_id:
        raise MaintenanceBusy('Maintenance completion requires its exclusive gate')
    if json.loads(stop.read_text()) != {'operation_id': operation_id}:
        raise MaintenanceBusy('Maintenance marker identity changed')
    stop.unlink()
    fd = os.open(root, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
