#!/usr/bin/env python3
"""Short-lived launcher for the daily pipeline.

This keeps OpenClaw cron out of the long-running process lifetime while still
leaving enough metadata for watchdogs and manual stop commands.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
REPO = SCRIPT_DIR.parent
PIPEDIR = REPO / "pipeline_state"
LOGDIR = PIPEDIR / "logs"
LAUNCH_LOG = LOGDIR / "daily_pipeline_launcher.log"
LOCKDIR = PIPEDIR / "daily_pipeline.lock"

RUNNER_PID_FILE = PIPEDIR / "runner.pid"
RUNNER_PGID_FILE = PIPEDIR / "runner.pgid"
RUNNER_STARTED_FILE = PIPEDIR / "runner.started_at"
RUNNER_FINISHED_FILE = PIPEDIR / "runner.finished_at"
WRAPPER_PID_FILE = PIPEDIR / "wrapper.pid"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(message: str) -> None:
    LOGDIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    line = f"[{stamp}] {message}"
    print(line, flush=True)
    with LAUNCH_LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def write_text(path: Path, text: str) -> None:
    path.write_text(text + "\n", encoding="utf-8")


def last_status_line() -> str:
    status = read_text(PIPEDIR / "status")
    return status.splitlines()[-1].strip() if status else ""


def is_terminal_status(status: str) -> bool:
    return status == "ALL_DONE" or status.startswith("FAIL:") or status.startswith("ABORTED:")


def numeric_file(path: Path) -> int | None:
    raw = read_text(path)
    digits = "".join(ch for ch in raw if ch.isdigit())
    return int(digits) if digits else None


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def pgid_alive(pgid: int | None) -> bool:
    if not pgid:
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def first_live_pid() -> int | None:
    for path in (
        RUNNER_PID_FILE,
        LOCKDIR / "runner.pid",
        WRAPPER_PID_FILE,
        LOCKDIR / "wrapper.pid",
    ):
        pid = numeric_file(path)
        if pid_alive(pid):
            return pid
    return None


def first_live_pgid() -> int | None:
    for path in (RUNNER_PGID_FILE, LOCKDIR / "runner.pgid"):
        pgid = numeric_file(path)
        if pgid_alive(pgid):
            return pgid
    return None


def clear_lock_if_stale() -> None:
    if not LOCKDIR.exists():
        return

    status = last_status_line()
    live_pid = first_live_pid()
    live_pgid = first_live_pgid()
    if (live_pid or live_pgid) and not is_terminal_status(status):
        active = live_pid if live_pid else f"pgid:{live_pgid}"
        log(f"Another daily pipeline appears active; active={active} status={status or 'UNKNOWN'}")
        print(f"ACTIVE:{active}:{status or 'UNKNOWN'}")
        raise SystemExit(75)

    stale_name = PIPEDIR / f"daily_pipeline.lock.stale-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    log(
        "Moving stale daily pipeline lock aside; "
        f"status={status or 'UNKNOWN'} live_pid={live_pid or 'NONE'} live_pgid={live_pgid or 'NONE'}"
    )
    if stale_name.exists():
        shutil.rmtree(stale_name)
    LOCKDIR.rename(stale_name)


def acquire_lock() -> None:
    clear_lock_if_stale()
    try:
        LOCKDIR.mkdir(mode=0o700)
    except FileExistsError:
        log("Could not acquire daily pipeline lock after stale check")
        raise SystemExit(75)
    write_text(LOCKDIR / "acquired_at", utc_now())
    write_text(LOCKDIR / "wrapper.pid", str(os.getpid()))
    write_text(WRAPPER_PID_FILE, str(os.getpid()))
    log(f"Acquired daily pipeline lock; launcher_pid={os.getpid()}")


def cleanup_lock() -> None:
    if LOCKDIR.exists():
        shutil.rmtree(LOCKDIR)


def run_pre_pipeline() -> int:
    pre_status = PIPEDIR / "pre_status"
    if pre_status.exists():
        pre_status.unlink()
    log("Starting conductor/pre_pipeline.sh")
    proc = subprocess.run(["bash", str(SCRIPT_DIR / "pre_pipeline.sh")], cwd=REPO)
    status = read_text(pre_status).splitlines()[-1].strip() if pre_status.exists() else ""

    if proc.returncode == 0 and status == "ALL_DONE":
        log("conductor/pre_pipeline.sh completed successfully")
        return 0

    pre_log_tail = ""
    pre_log = LOGDIR / "pre_pipeline_current.log"
    if pre_log.exists():
        pre_log_tail = "\n".join(pre_log.read_text(encoding="utf-8", errors="ignore").splitlines()[-40:])
    if proc.returncode == 0 and not status and "NYSE 今日休市" in pre_log_tail:
        log("NYSE closed; conductor/pre_pipeline.sh skipped without status")
        write_text(RUNNER_FINISHED_FILE, utc_now())
        cleanup_lock()
        return 0

    log(f"conductor/pre_pipeline.sh failed or status unknown; exit={proc.returncode} status={status or 'MISSING'}")
    cleanup_lock()
    return proc.returncode or 1


def launch_runner() -> int:
    status_file = PIPEDIR / "status"
    if status_file.exists():
        status_file.unlink()
    if RUNNER_FINISHED_FILE.exists():
        RUNNER_FINISHED_FILE.unlink()

    write_text(RUNNER_STARTED_FILE, utc_now())
    log("Starting conductor/pipeline_runner.sh with nohup-style detached session")

    # The runner writes its own pipeline_current.log; keep launcher stdio quiet
    # and detach the runner into a new process group/session.
    devnull = open(os.devnull, "ab")
    proc = subprocess.Popen(
        ["bash", str(SCRIPT_DIR / "pipeline_runner.sh")],
        cwd=REPO,
        stdin=subprocess.DEVNULL,
        stdout=devnull,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    runner_pid = proc.pid
    time.sleep(0.3)
    try:
        runner_pgid = os.getpgid(runner_pid)
    except ProcessLookupError:
        runner_pgid = None

    write_text(RUNNER_PID_FILE, str(runner_pid))
    write_text(LOCKDIR / "runner.pid", str(runner_pid))
    write_text(RUNNER_PGID_FILE, str(runner_pgid) if runner_pgid else "UNKNOWN")
    write_text(LOCKDIR / "runner.pgid", str(runner_pgid) if runner_pgid else "UNKNOWN")
    log(f"conductor/pipeline_runner.sh launched; runner_pid={runner_pid} runner_pgid={runner_pgid or 'UNKNOWN'}")

    time.sleep(5)
    if not pid_alive(runner_pid):
        rc = proc.poll()
        write_text(RUNNER_FINISHED_FILE, utc_now())
        cleanup_lock()
        log(f"runner died during launch verification; runner_pid={runner_pid} exit={rc}")
        return rc if isinstance(rc, int) and rc != 0 else 1

    log(f"runner verified alive after 5s; runner_pid={runner_pid} runner_pgid={runner_pgid or 'UNKNOWN'}")
    print(f"LAUNCHED:{runner_pid}:{runner_pgid or 'UNKNOWN'}")
    return 0


def main() -> int:
    LOGDIR.mkdir(parents=True, exist_ok=True)
    os.chdir(REPO)
    acquire_lock()

    pre_exit = run_pre_pipeline()
    if pre_exit != 0:
        return pre_exit

    # If pre_pipeline skipped for a holiday it cleaned the lock and returned 0.
    if not LOCKDIR.exists():
        return 0

    return launch_runner()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("Launcher interrupted")
        cleanup_lock()
        raise SystemExit(130)
