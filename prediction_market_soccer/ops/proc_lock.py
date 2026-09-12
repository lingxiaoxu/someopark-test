"""Kernel-held process locks; duplicate jobs never proceed as successful work."""
from __future__ import annotations

import fcntl
import os
import sys

from prediction_market_soccer.config import CONFIG

_HELD = {}


def acquire(name: str) -> bool:
    CONFIG.paths.data.mkdir(parents=True, exist_ok=True)
    fd = os.open(CONFIG.paths.data / f".{name}.lock", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return False
    except OSError:
        os.close(fd)
        raise
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    _HELD[name] = fd
    return True


def release(name: str) -> None:
    fd = _HELD.pop(name, None)
    if fd is not None:
        os.close(fd)


def acquire_or_exit(name: str, *, busy_exit: int = 0) -> None:
    if not acquire(name):
        print(f"[{name}] another instance holds the lock — skipping")
        sys.exit(busy_exit)


from prediction_market_soccer.ops.maintenance_gate import writer


@writer
def main():
    import argparse
    import subprocess
    ap = argparse.ArgumentParser(description="Hold a job lock through its complete subprocess")
    ap.add_argument("--run", required=True)
    ap.add_argument("command", nargs=argparse.REMAINDER)
    args = ap.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        ap.error("a command is required")
    if not acquire(args.run):
        print(f"[{args.run}] pipeline already active — skipped before publishing")
        return
    from prediction_market_soccer.ops.run_status import RunStatus, publish_operations
    status = RunStatus("publish")
    try:
        result = status.step("refresh_build_deploy", lambda: subprocess.run(command, check=True))
        status.finish()
        if result is None:
            raise SystemExit(1)
    finally:
        publish_operations()
        release(args.run)


if __name__ == "__main__":
    main()
