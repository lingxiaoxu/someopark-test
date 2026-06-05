#!/usr/bin/env python3
"""Stop the detached daily pipeline by process group."""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import time
from datetime import datetime, timezone
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
REPO = SCRIPT_DIR.parent
PIPEDIR = REPO / "pipeline_state"
LOCKDIR = PIPEDIR / "daily_pipeline.lock"


def read_number(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    return int(digits) if digits else None


def alive_pid(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def alive_pgid(pgid: int | None) -> bool:
    if not pgid:
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def write_status(status: str) -> None:
    PIPEDIR.mkdir(parents=True, exist_ok=True)
    (PIPEDIR / "status").write_text(status + "\n", encoding="utf-8")
    (PIPEDIR / "runner.finished_at").write_text(
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=20.0, help="seconds to wait after TERM before KILL")
    parser.add_argument("--kill", action="store_true", help="send KILL immediately instead of TERM")
    parser.add_argument("--keep-lock", action="store_true", help="do not remove daily_pipeline.lock after stop")
    args = parser.parse_args()

    pgid = read_number(PIPEDIR / "runner.pgid") or read_number(LOCKDIR / "runner.pgid")
    pid = read_number(PIPEDIR / "runner.pid") or read_number(LOCKDIR / "runner.pid")

    if not pgid and pid:
        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            pgid = None

    if not pgid and not pid:
        print("NO_METADATA")
        return 2

    print(f"TARGET pid={pid or 'UNKNOWN'} pgid={pgid or 'UNKNOWN'}")

    if pgid and alive_pgid(pgid):
        sig = signal.SIGKILL if args.kill else signal.SIGTERM
        os.killpg(pgid, sig)
        print(f"SENT:{sig.name}:PGID:{pgid}")
    elif pid and alive_pid(pid):
        sig = signal.SIGKILL if args.kill else signal.SIGTERM
        os.kill(pid, sig)
        print(f"SENT:{sig.name}:PID:{pid}")
    else:
        print("NOT_RUNNING")
        write_status("ABORTED:STOP:not_running")
        if LOCKDIR.exists() and not args.keep_lock:
            shutil.rmtree(LOCKDIR)
        return 0

    if not args.kill:
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            if not alive_pgid(pgid) and not alive_pid(pid):
                break
            time.sleep(0.5)

        if alive_pgid(pgid):
            os.killpg(pgid, signal.SIGKILL)
            print(f"SENT:SIGKILL:PGID:{pgid}")
        elif alive_pid(pid):
            os.kill(pid, signal.SIGKILL)
            print(f"SENT:SIGKILL:PID:{pid}")

    time.sleep(0.5)
    if alive_pgid(pgid) or alive_pid(pid):
        print("STOP_INCOMPLETE")
        return 1

    write_status("ABORTED:STOP:daily_pipeline_stop")
    if LOCKDIR.exists() and not args.keep_lock:
        shutil.rmtree(LOCKDIR)
    print("STOPPED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
