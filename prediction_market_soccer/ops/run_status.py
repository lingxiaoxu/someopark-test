"""Atomic artifacts and persistent job outcomes; public summaries omit exceptions/paths."""
from __future__ import annotations

import json
import math
import fcntl
from contextlib import contextmanager
import os
import tempfile
import sqlite3
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from prediction_market_soccer.config import CONFIG


# Backoff is additional to SQLite's configured busy timeout (60 s in the store).
_LOCK_RETRY_BASE_S = 5.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_bytes(path: Path, payload: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None  # existing no-sample metrics use NaN; JSON represents absence as null
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def atomic_json(path: Path, doc) -> None:
    path = Path(path)
    from prediction_market_soccer.util.public_projection import PUBLIC_BOARDS, project_public_board
    configured_board = path.name in PUBLIC_BOARDS and path.parent.resolve() in {
        CONFIG.paths.output.resolve(), CONFIG.paths.frontend_data.resolve()}
    if configured_board:
        # Publish a display projection only after its original quote captures are
        # durable. These capture-only files do NOT create source availability or
        # paper observations. Trading already consumed the untouched build object.
        import gzip
        import hashlib
        projected, captures = project_public_board(doc)
        for digest, payload in captures.items():
            private = CONFIG.paths.raw_snapshots / "public_quote_captures" / digest[:2] / (digest + ".json.gz")
            if private.exists():
                if hashlib.sha256(gzip.decompress(private.read_bytes())).hexdigest() != digest:
                    raise ValueError("Private public-board quote capture changed")
            else:
                atomic_bytes(private, gzip.compress(payload, compresslevel=1, mtime=0))
            # atomic_bytes uses a private mkstemp file; preserve that restriction
            # for an existing object as well. Never publish this private path.
            private.chmod(0o600)
        doc = projected
    atomic_bytes(path, json.dumps(_json_safe(doc), ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8"))


@contextmanager
def publication_lock():
    CONFIG.paths.data.mkdir(parents=True, exist_ok=True)
    with (CONFIG.paths.data / ".artifact-publish.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def strategy_ledger_version(doc) -> tuple | None:
    ledger = doc.get("strategy_ledger") if isinstance(doc, dict) else None
    if not isinstance(ledger, dict) or not ledger.get("ledger_id") or not ledger.get("as_of"):
        return None
    # A deliberate model/method replacement can reproduce identical rows and
    # dates. Keep its book identity intact across all three published views.
    book = ledger.get("book_version") or {}
    return ledger["ledger_id"], ledger["as_of"], book.get("version_id")


def read_json_doc(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_both(name: str, doc) -> bool:
    with publication_lock():
        if name == "milestone_marks.json":
            # A live exporter may finish after a settled batch has published a
            # newer ledger. Quotes can wait for its next pass; an old strategy
            # snapshot must never replace the newly published canonical rows.
            expected = strategy_ledger_version(read_json_doc(CONFIG.paths.output / "performance_report.json"))
            if expected and strategy_ledger_version(doc) != expected:
                return False
        for directory in (CONFIG.paths.output, CONFIG.paths.frontend_data):
            atomic_json(directory / name, doc)
    return True


def status_path(name: str) -> Path:
    # data stays stable while a full refresh stages its output/frontend directories.
    return CONFIG.paths.data / "runtime" / f"{name}.json"


def read_status(name: str) -> dict:
    try:
        return json.loads(status_path(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}



def _call_retrying_locks(fn, attempts, name):
    """Call fn, retrying only SQLite write contention, with a short linear backoff."""
    for remaining in range(attempts, -1, -1):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if remaining == 0 or "locked" not in str(exc).lower():
                raise
            wait = float(attempts - remaining + 1) * _LOCK_RETRY_BASE_S
            print(f"  ↻ {name}: {exc} — retrying in {wait:.0f}s ({remaining} left)", flush=True)
            time.sleep(wait)


class RunStatus:
    def __init__(self, name: str):
        self.name = name
        previous = read_status(name)
        self.doc = {"name": name, "state": "running", "last_attempt_at": utc_now(),
                    "last_success_at": previous.get("last_success_at"), "finished_at": None,
                    "steps": [], "last_success_input": previous.get("last_success_input")}
        self.save()

    def save(self):
        self.doc["last_activity_at"] = utc_now()
        atomic_json(status_path(self.name), self.doc)

    def step(self, name, fn, *, required=True, result_validator=None, retry_on_lock=0):
        """Run one named stage.

        ``retry_on_lock`` re-runs the step after SQLite reports write contention.
        It is opt-in per step and NOT a default, because a retry is only safe for
        an idempotent one: re-running a ledger append (settled_bet,
        strategy_book_append) after a partial failure would double-append. Ingest
        projections may opt in only when the callable also owns its transaction
        rollback and post-commit availability recovery.

        Why it exists: a transient `database is locked` inside the required
        ``club_recent`` projection aborts the whole refresh (2026-09-11 18:33 and
        2026-09-13 15:46). ``match_trigger`` does recover it — the results stay
        unacknowledged so the next tick returns RUN — but recovery cost a full
        re-ingest and 19 minutes of stale exports on 09-13 (15:46 failed, 16:05
        succeeded). The two retries add 5 s then 10 s of backoff to SQLite's own
        busy timeout; a persistent lock still fails the required step.
        """
        item = {"name": name, "state": "running", "required": required, "started_at": utc_now()}
        self.doc["steps"].append(item)
        self.save()
        try:
            result = _call_retrying_locks(fn, retry_on_lock, name)
            complete = result_validator(result) if result_validator else not (
                isinstance(result, dict) and result.get('complete') is False)
            item["state"] = "ok" if complete else "partial"
            if not complete:
                item['reason'] = 'incomplete_result'
            print(f"  {'✓' if complete else '△'} {name}", flush=True)
            return result
        except Exception as exc:
            item.update(state="failed", error_type=type(exc).__name__)
            # Full diagnostics belong in the private job log, never in public JSON.
            print(f"  ✗ {name}: {exc}", flush=True)
            traceback.print_exc()
            return None
        finally:
            item["finished_at"] = utc_now()
            self.save()

    @property
    def failed(self):
        return any(s["state"] in ("failed", "partial") and s["required"] for s in self.doc["steps"])

    def finish(self, *, error=None, input_value=None):
        failures = [s for s in self.doc["steps"] if s["state"] in ("failed", "partial")]
        self.doc["state"] = ("failed" if error or self.failed else "degraded" if failures else "ok")
        self.doc["finished_at"] = utc_now()
        if error:
            self.doc["error_type"] = type(error).__name__
        if self.doc["state"] == "ok":
            self.doc["last_success_at"] = self.doc["finished_at"]
            self.doc["last_success_input"] = input_value
        self.save()
        return not (error or self.failed)


def safe_summary() -> dict:
    jobs = []
    now = datetime.now(timezone.utc)
    for name in ("full_refresh", "live_refresh", "settle_reports", "publish"):
        doc = read_status(name)
        if not doc:
            continue
        item = {k: doc.get(k) for k in
                ("name", "state", "last_attempt_at", "last_success_at")}
        item["failed_steps"] = [s["name"] for s in doc.get("steps", []) if s.get("state") in ("failed", "partial")]
        try:
            last = datetime.fromisoformat((doc.get("last_activity_at") or doc["last_attempt_at"]).replace("Z", "+00:00"))
            age = (now - last).total_seconds()
        except (KeyError, TypeError, ValueError):
            age = float("inf")
        limit = 300 if name == "live_refresh" else 7200
        if (doc.get("state") == "running" or name == "live_refresh") and age > limit:
            item["state"] = "unavailable"
            item["failed_steps"].append("heartbeat_stale" if name == "live_refresh" else "job_interrupted_or_stale")
        jobs.append(item)
    states = {j["state"] for j in jobs}
    state = ("unavailable" if not jobs else "degraded" if states & {"failed", "degraded", "unavailable"}
             else "running" if "running" in states else "ok")
    return {"as_of": utc_now(), "state": state, "jobs": jobs}


def publish_operations() -> None:
    """Update health without racing a newer overview or forging its generation time."""
    with publication_lock():
        path = CONFIG.paths.output / "frontend_overview.json"
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        doc["operations"] = safe_summary()
        for directory in (CONFIG.paths.output, CONFIG.paths.frontend_data):
            atomic_json(directory / path.name, doc)
