"""Single-instance process lock (R10 — the macro module's ebdf6a9 lesson).

macOS has no flock(1) binary, so the lock lives at the PYTHON level via
fcntl.flock on a file under data/ (NOT /tmp): the kernel releases it when the
holding fd closes — including SIGKILL — so no stale-lock cleanup is ever needed.
The 30/900-second launchd cadences can overlap a slow run; the second instance
must exit immediately instead of double-writing (the WC module had no lock and
that was a known weakness; the macro module's refresh double-ran for weeks).

Usage (top of a pipeline main()):
    from prediction_market_soccer.ops.proc_lock import acquire_or_exit
    acquire_or_exit("live_refresh")
"""
from __future__ import annotations

import fcntl
import os
import sys

from prediction_market_soccer.config import CONFIG

_HELD = {}   # name -> fd (kept referenced so the fd never closes while we run)


def acquire_or_exit(name: str) -> None:
    CONFIG.paths.ensure()
    path = CONFIG.paths.data / f".{name}.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        os.close(fd)
        print(f"[{name}] another instance holds the lock — exiting (single-instance guard)")
        sys.exit(0)
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    _HELD[name] = fd   # released by the kernel on process exit
