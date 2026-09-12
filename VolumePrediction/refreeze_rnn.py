"""Refreeze the serving RNN into an isolated, validated candidate.

``python -m VolumePrediction.refreeze_rnn [--panel PATH] [--dry-run]``
reuses a completed local panel; it never rebuilds the panel, fetches data, or
changes production/blend pointers. The production protocol stays at 3 seeds and
50 epochs. A staging directory is published only after validation. Exit 3 means
the daytime/pairs/concurrent-run guard refused training and a later retry is safe.

Dry-run inspects parquet footers and process metadata only: no directory/log,
status, model import, parquet scan, or registration writes. Synthetic tests can
inject the trainer and guard; production CLI does not expose guard bypasses.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, time
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
from typing import Callable, Optional
import uuid
from zoneinfo import ZoneInfo

from VolumePrediction.common import OUT, REPO

ET = ZoneInfo("America/New_York")
FEATURE_COLS = [f"tech_{s}_ma{w}" for w in (1, 5, 22, 252)
                for s in ("ret", "v")] + [
    "fund1_dimson_beta", "fund1_size_ln_mcap", "fund1_firm_age",
    "fund1_be_me", "fund1_book_leverage", "fund1_sue"]
BASE_COLS = ["V", "v", "ma5_v", "eta", "ret"]
PROTOCOL = {"seeds": 3, "epochs": 50, "seq_len": 10, "batch_size": 1024,
            "optimizer": "Adam defaults", "loss": "MSE", "hidden": [32, 16, 8],
            "early_stopping": False, "dropout": False,
            "feature_dtype": "float32"}


def _now(now: Optional[datetime] = None) -> datetime:
    value = now or datetime.now(ET)
    if value.tzinfo is None:
        raise ValueError("now must include a timezone")
    return value.astimezone(ET)


def _read_registry(out_dir: Path) -> dict:
    """Unlike Registry.load, fail closed on damaged state before a mutation."""
    p = out_dir / "registry" / "registry.json"
    if not p.exists():
        return {"models": {}, "production": None, "promotions": []}
    d = json.loads(p.read_text())
    if not isinstance(d, dict) or not isinstance(d.get("models"), dict):
        raise ValueError("registry must be an object containing models")
    return d


def _raw_last(raw_dir: Path, now: datetime) -> str:
    import pyarrow.parquet as pq
    today = now.date().isoformat()
    # A same-day grouped file before the post-close cutoff can be provisional.
    # The training start guard is daytime, so normally this selects yesterday.
    for p in sorted(raw_dir.glob("grouped_????-??-??.parquet"), reverse=True):
        d = p.stem.removeprefix("grouped_")
        if d > today or (d == today and now.time() < time(17)):
            continue
        try:
            if pq.ParquetFile(p).metadata.num_rows > 0:
                return d
        except Exception:
            continue
    raise ValueError("no completed, nonempty raw day available")


def inspect_panel(path: str | Path, raw_last: str) -> dict:
    """Read only parquet footer statistics; never infer asof from the filename."""
    import pyarrow.parquet as pq
    p = Path(path).resolve()
    pf = pq.ParquetFile(p)
    names = pf.schema_arrow.names
    pandas_meta = json.loads((pf.schema_arrow.metadata or {}).get(b"pandas", b"{}"))
    if pandas_meta.get("index_columns") != ["date", "ticker"]:
        raise ValueError(f"{p.name}: requires MultiIndex(date,ticker)")
    required = FEATURE_COLS + BASE_COLS + ["date", "ticker"]
    missing = sorted(set(required) - set(names))
    actual_features = [c for c in names if c.startswith(("tech_", "fund1_"))]
    if missing or set(actual_features) != set(FEATURE_COLS):
        raise ValueError(f"{p.name}: incorrect narrow feature/base schema; missing={missing}")
    idx = names.index("date")
    stats = [pf.metadata.row_group(i).column(idx).statistics
             for i in range(pf.metadata.num_row_groups)]
    if not stats or any(s is None or not s.has_min_max or s.null_count for s in stats):
        raise ValueError(f"{p.name}: date footer statistics unavailable/incomplete")
    first = min(s.min for s in stats).date().isoformat()
    last = max(s.max for s in stats).date().isoformat()
    if last > raw_last:
        raise ValueError(f"{p.name}: panel end {last} exceeds completed raw {raw_last}")
    # A dated prod tag and an associated latest pointer must agree with the data.
    tag = re.search(r"_prod_(\d{8})(?:_[^.]*)?\.parquet$", p.name)
    if tag and tag[1] != last.replace("-", ""):
        raise ValueError(f"{p.name}: dated panel tag disagrees with footer")
    ptr = p.parent / "latest.json"
    if ptr.exists():
        meta = json.loads(ptr.read_text())
        ref = Path(meta.get("path", ""))
        if not ref.is_absolute():
            ref = p.parent / ref
        if ref.resolve() == p:
            if meta.get("date_range") != [first, last] or meta.get("rows") != pf.metadata.num_rows:
                raise ValueError(f"{p.name}: latest.json disagrees with parquet footer")
    eta_i = names.index("eta")
    eta_stats = [pf.metadata.row_group(i).column(eta_i).statistics
                 for i in range(pf.metadata.num_row_groups)]
    if any(s is None or not s.has_null_count for s in eta_stats):
        raise ValueError(f"{p.name}: eta footer statistics unavailable")
    n_train = pf.metadata.num_rows - sum(s.null_count for s in eta_stats)
    if n_train <= 0:
        raise ValueError(f"{p.name}: no trainable eta rows")
    st = p.stat()
    return {"path": str(p), "date_range": [first, last], "asof": last,
            "rows": pf.metadata.num_rows, "n_train_rows": n_train,
            "size_bytes": st.st_size, "mtime_ns": st.st_mtime_ns,
            "features": list(FEATURE_COLS), "retained_columns": FEATURE_COLS + BASE_COLS}


def guard_status(repo: Path = REPO, now: Optional[datetime] = None) -> dict:
    """Conservative read-only guard: live pairs PIDs/groups and unmatched START."""
    stamp = _now(now)
    reasons = []
    if not time(9, 30) <= stamp.time() <= time(14, 30):
        reasons.append("outside_start_window_0930_1430_ET")
    try:
        ps = subprocess.run(["ps", "-axo", "pid=,pgid=,command="],
                            capture_output=True, text=True, check=True, timeout=10)
        processes = []
        for line in ps.stdout.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                processes.append((int(parts[0]), int(parts[1]), parts[2]))
    except Exception:
        processes = []
        reasons.append("process_inventory_unavailable")
    live = {pid for pid, _, _ in processes}
    groups = {pgid for _, pgid, _ in processes}
    state = Path(repo) / "pipeline_state"
    for base in (state, state / "daily_pipeline.lock"):
        for name in ("runner.pid", "wrapper.pid", "runner.pgid"):
            p = base / name
            if not p.exists():
                continue
            value = p.read_text().strip()
            if value.isdigit() and int(value) in (groups if name.endswith("pgid") else live):
                reasons.append(f"pairs_live_{name}")
    pattern = re.compile(r"(?:^|[ /])(?:pipeline_runner|pre_pipeline|bdc_daily_pipeline)\.sh(?:\s|$)")
    if any(pid != os.getpid() and pattern.search(cmd) for pid, _, cmd in processes):
        reasons.append("pairs_process_running")
    log_path = state / "logs" / "pipeline_current.log"
    last_start = last_complete = -1
    if log_path.exists():
        with log_path.open(errors="replace") as f:
            for i, line in enumerate(f):
                if "PIPELINE START" in line:
                    last_start = i
                if "PIPELINE COMPLETE" in line:
                    last_complete = i
        if last_start > last_complete:
            reasons.append("pairs_unmatched_PIPELINE_START")
    return {"allowed": not reasons, "reasons": sorted(set(reasons)),
            "checked_at": stamp.isoformat(), "latest_start_et": "14:30",
            "hard_stop_et": "17:15", "daily_refresh_et": "17:33"}


def plan(panel_path: Optional[str | Path] = None, *, out_dir: Path = OUT,
         raw_dir: Optional[Path] = None, now: Optional[datetime] = None) -> dict:
    out_dir = Path(out_dir)
    raw = Path(raw_dir) if raw_dir else REPO / "price_data" / "volume_prediction" / "raw"
    stamp = _now(now)
    raw_last = _raw_last(raw, stamp)
    rejected = []
    if panel_path:
        source = inspect_panel(panel_path, raw_last)
    else:
        choices = []
        for p in (out_dir / "panel").glob("panel_*.parquet"):
            try:
                choices.append(inspect_panel(p, raw_last))
            except Exception as exc:
                rejected.append({"panel": p.name, "reason": str(exc)})
        if not choices:
            raise ValueError(f"no usable completed panel ({len(rejected)} rejected)")
        source = max(choices, key=lambda d: (d["asof"], d["mtime_ns"], d["path"]))
    version = "rnn_prod_" + source["asof"].replace("-", "")
    reg = _read_registry(out_dir)
    return {"version": version, "asof": source["asof"], "raw_through": raw_last,
            "source_panel": source, "protocol": dict(PROTOCOL),
            "previous_rnn_version": (reg.get("blend") or {}).get("rnn_version"),
            "incumbent_version": (reg.get("blend") or {}).get("rnn_version"),
            "artifact_dir": str(out_dir / "registry" / "artifacts" / version),
            "status_file": str(out_dir / "refreeze_rnn" / f"status_{version}.json"),
            "rejected_panels": rejected, "planned_at": stamp.isoformat()}


def _atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _write_status(out_dir: Path, data: dict) -> dict:
    data = {**data, "updated_at": _now().isoformat()}
    _atomic_json(out_dir / "refreeze_rnn" / "status.json", data)
    if data.get("version"):
        _atomic_json(out_dir / "refreeze_rnn" / f"status_{data['version']}.json", data)
    return data


def _narrow_panel(source: dict, dest: Path) -> None:
    import numpy as np
    import pandas as pd
    p = Path(source["path"])
    st = p.stat()
    if (st.st_size, st.st_mtime_ns) != (source["size_bytes"], source["mtime_ns"]):
        raise ValueError("source panel changed since planning")
    df = pd.read_parquet(p, columns=FEATURE_COLS + BASE_COLS)
    if list(df.index.names) != ["date", "ticker"] or not df.index.is_monotonic_increasing:
        raise ValueError("source panel index is not sorted (date,ticker)")
    if df.index.has_duplicates:
        raise ValueError("source panel has duplicate (date,ticker) rows")
    if str(df.index.get_level_values("date").max().date()) != source["asof"]:
        raise ValueError("source panel asof changed since planning")
    # Validate by column to avoid a second full-size float64 feature matrix.
    for c in FEATURE_COLS:
        if not np.isfinite(df[c].to_numpy()).all():
            raise ValueError(f"nonfinite feature: {c}")
        df[c] = df[c].astype("float32")
    if int(df["eta"].notna().sum()) != source["n_train_rows"]:
        raise ValueError("source trainable row count changed")
    for c in BASE_COLS:
        values = df[c].dropna().to_numpy()
        if not np.isfinite(values).all():
            raise ValueError(f"nonfinite base/target column: {c}")
    df.to_parquet(dest)


def validate_artifact(art: Path, expected: dict) -> dict:
    """Check exported weights/state without torch or a production serve call."""
    import numpy as np
    import pandas as pd
    from VolumePrediction.rnn_export import RNNWeights
    meta = json.loads((art / "meta.json").read_text())
    checks = {"version": expected["version"], "kind": "learned.rnn",
              "trained_through": expected["asof"], "seeds": 3, "seq_len": 10,
              "feature_cols": FEATURE_COLS,
              "weight_files": [f"weights_s{i}.npz" for i in range(3)],
              "n_train_rows": expected["source_panel"]["n_train_rows"]}
    for k, value in checks.items():
        if meta.get(k) != value:
            raise ValueError(f"artifact {k} mismatch")
    if meta.get("refreeze", {}).get("protocol") != PROTOCOL:
        raise ValueError("artifact refreeze protocol mismatch")
    old_source = meta["refreeze"].get("source_panel", {})
    if any(old_source.get(k) != expected["source_panel"].get(k)
           for k in ("path", "size_bytes", "mtime_ns", "asof")):
        raise ValueError("existing version belongs to a different source panel")
    if meta.get("seq_tail_date", "") < expected["asof"]:
        raise ValueError("artifact seq_tail predates training")
    if meta.get("first_serve_date", "") <= expected["asof"]:
        raise ValueError("first serve date must follow training cutoff")
    if meta.get("fund_cols") != FEATURE_COLS[8:]:
        raise ValueError("artifact fund columns mismatch")
    per = pd.read_parquet(art / "per_ticker.parquet")
    if len(per) != meta.get("n_tickers") or per.index.has_duplicates:
        raise ValueError("invalid per_ticker universe")
    if "active" not in per or int(per["active"].sum()) != meta.get("n_active") or not per["active"].any():
        raise ValueError("invalid/empty active universe")
    frozen_cols = [f"{c}__z_next" for c in FEATURE_COLS[:8]] + FEATURE_COLS[8:]
    if not set(frozen_cols + ["ma5v_next"]).issubset(per.columns):
        raise ValueError("missing frozen serving features")
    active = per.loc[per["active"]]
    if not np.isfinite(active[frozen_cols].to_numpy()).all():
        raise ValueError("nonfinite frozen serving features")
    servable = active[np.isfinite(active["ma5v_next"].to_numpy())]
    if servable.empty:
        raise ValueError("no active ticker has a finite volume anchor")
    with np.load(art / "seq_tail.npz", allow_pickle=False) as z:
        if z["feats"].shape != (len(per), 9, 14) or not np.isfinite(z["feats"]).all():
            raise ValueError("invalid sequence state")
        if len(z["tickers"]) != len(per) or set(z["tickers"].astype(str)) != set(per.index):
            raise ValueError("sequence universe mismatch")
        if len(z["last_date"]) != len(per):
            raise ValueError("sequence date count mismatch")
    shapes = {"W_ih": (128, 14), "W_hh": (128, 32), "b_ih": (128,),
              "W2": (16, 32), "b2": (16,), "W3": (8, 16), "b3": (8,),
              "W4": (1, 8), "b4": (1,)}
    for name in meta["weight_files"]:
        with np.load(art / name, allow_pickle=False) as z:
            for k, shape in shapes.items():
                if z[k].shape != shape or not np.isfinite(z[k]).all():
                    raise ValueError(f"invalid exported weight: {name}/{k}")
        w = RNNWeights.load(art / name)
        if (w.n_pred, w.seq_len) != (14, 10) or not np.isfinite(w.predict_windows(np.zeros((2, 10, 14), dtype="float32"))).all():
            raise ValueError(f"invalid numpy inference: {name}")
    return {"n_tickers": len(per), "n_active": int(per["active"].sum()),
            "n_servable": len(servable),
            "weight_files": meta["weight_files"], "valid": True}


def _register(expected: dict, out_dir: Path) -> None:
    from VolumePrediction.outputs.recorder import Registry
    reg = _read_registry(out_dir)
    version = expected["version"]
    if version in reg["models"]:
        old = reg["models"][version]
        if old.get("kind") != "learned.rnn" or old.get("trained_through") != expected["asof"]:
            raise ValueError("existing registry version disagrees with candidate")
        return  # Preserve subsequent AB metrics or a later approved promotion.
    if version == reg.get("production") or version == (reg.get("blend") or {}).get("rnn_version"):
        raise ValueError("will not register an unregistered active version as candidate")
    Registry(out_dir).record_model(
        version, kind="learned.rnn", trained_through=expected["asof"], status="candidate",
        meta={"source_panel": expected["source_panel"], "protocol": PROTOCOL,
              "previous_rnn_version": expected["previous_rnn_version"],
              "incumbent_version": expected["incumbent_version"],
              "refreeze_status_file": expected["status_file"]})


@contextmanager
def _training_deadline(now: datetime):
    """CLI training stops by 17:15 ET, leaving 18 minutes before daily refresh."""
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("real training must run in the main thread for deadline protection")
    seconds = (now.replace(hour=17, minute=15, second=0, microsecond=0) - now).total_seconds()
    if seconds <= 0:
        raise TimeoutError("17:15 ET training deadline has already passed")
    old_handler = signal.getsignal(signal.SIGALRM)
    old_timer = signal.getitimer(signal.ITIMER_REAL)
    if old_timer[0]:
        raise RuntimeError("an existing process alarm conflicts with training deadline")
    def stop(_sig, _frame):
        raise TimeoutError("RNN training reached 17:15 ET deadline; retry in daytime")
    signal.signal(signal.SIGALRM, stop)
    signal.setitimer(signal.ITIMER_REAL, max(1, seconds))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)


def run(panel_path: Optional[str | Path] = None, *, dry_run: bool = False,
        out_dir: Path = OUT, raw_dir: Optional[Path] = None, repo: Path = REPO,
        now: Optional[datetime] = None, freeze_fn: Optional[Callable] = None,
        validate_fn: Optional[Callable] = None, guard_fn: Optional[Callable] = None) -> dict:
    """Plan/train/register a candidate; dependencies may be injected for tests."""
    out_dir = Path(out_dir)
    stamp = _now(now)
    state = {"status": "planning", "exit_code": 1, "pid": os.getpid()}
    stage_parent = None
    lock = None
    handler = None
    previous_level = None
    logger = logging.getLogger("VolumePrediction.refreeze_rnn")
    try:
        state.update(plan(panel_path, out_dir=out_dir, raw_dir=raw_dir, now=stamp))
        guard = (guard_fn or guard_status)(Path(repo), stamp)
        state["guard"] = guard
        art = Path(state["artifact_dir"])
        if dry_run:
            return {**state, "status": "dry_run", "exit_code": 0,
                    "action": "validate_and_reuse" if art.exists() else "train_candidate",
                    "would_train_now": guard["allowed"] and not art.exists()}
        log_dir = out_dir.parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_dir / f"refreeze_rnn_{stamp:%Y%m%d}.log")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        # Capture trainer's per-seed progress as well as this orchestration.
        logger = logging.getLogger("VolumePrediction")
        previous_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        if not guard["allowed"] and not art.exists():
            return _write_status(out_dir, {**state, "status": "deferred", "exit_code": 3})
        lock_dir = out_dir / "refreeze_rnn"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock = (lock_dir / "training.lock").open("a+")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Do not overwrite the running trainer's status/PID with this retry.
            return {**state, "status": "deferred", "exit_code": 3,
                    "reason": "another_rnn_refreeze_is_running"}
        validator = validate_fn or validate_artifact
        if art.exists():
            validation = validator(art, state)
            _register(state, out_dir)
            return _write_status(out_dir, {**state, "status": "reused", "exit_code": 0,
                                         "validation": validation})
        artifacts = art.parent
        artifacts.mkdir(parents=True, exist_ok=True)
        stage_parent = artifacts / f".{state['version']}.staging.{uuid.uuid4().hex}"
        stage_art = stage_parent / state["version"]
        stage_art.mkdir(parents=True)
        narrow = stage_art / "training_panel.parquet"
        state.update({"status": "preparing_panel", "staging_dir": str(stage_parent)})
        _write_status(out_dir, state)
        _narrow_panel(state["source_panel"], narrow)
        # Recheck after projection: a pairs run may have started meanwhile.
        guard = (guard_fn or guard_status)(Path(repo), _now(now))
        if not guard["allowed"]:
            return _write_status(out_dir, {**state, "guard": guard,
                                         "status": "deferred", "exit_code": 3})
        state.update({"status": "training", "started_at": _now().isoformat()})
        _write_status(out_dir, state)
        logger.info("RNN refreeze starts version=%s asof=%s protocol=%s", state["version"], state["asof"], PROTOCOL)
        if freeze_fn is None:
            from VolumePrediction.prod_model_rnn import freeze
            with _training_deadline(_now()):
                freeze(narrow, state["asof"], art_dir=stage_art,
                       version=state["version"], seeds=3, epochs=50)
        else:
            freeze_fn(narrow, state["asof"], art_dir=stage_art,
                      version=state["version"], seeds=3, epochs=50)
        meta_path = stage_art / "meta.json"
        meta = json.loads(meta_path.read_text())
        meta["refreeze"] = {"source_panel": state["source_panel"], "protocol": PROTOCOL,
                            "previous_rnn_version": state["previous_rnn_version"],
                            "incumbent_version": state["incumbent_version"],
                            "prepared_at": _now().isoformat()}
        _atomic_json(meta_path, meta)
        state["status"] = "validating"
        _write_status(out_dir, state)
        validation = validator(stage_art, state)
        if art.exists():
            raise FileExistsError(f"candidate appeared while training: {art.name}")
        os.rename(stage_art, art)  # same-filesystem directory publication
        state["status"] = "registering"
        _write_status(out_dir, state)
        _register(state, out_dir)
        logger.info("RNN candidate published and registered: %s", state["version"])
        return _write_status(out_dir, {**state, "status": "complete", "exit_code": 0,
                                     "validation": validation})
    except Exception as exc:
        error = str(exc)
        for key in ("POLYGON_API_KEY", "MONGO_URI"):
            if os.environ.get(key):
                error = error.replace(os.environ[key], "<REDACTED>")
        result = {**state, "status": "deferred" if isinstance(exc, TimeoutError) else "error",
                  "exit_code": 3 if isinstance(exc, TimeoutError) else 1,
                  "error_type": type(exc).__name__, "error": error}
        if dry_run:
            return result
        if handler:
            logger.error("RNN refreeze failed: %s", error)
        return _write_status(out_dir, result)
    finally:
        if stage_parent is not None and stage_parent.exists():
            shutil.rmtree(stage_parent)
        if lock is not None:
            lock.close()
        if handler is not None:
            logger.removeHandler(handler)
            handler.close()
            logger.setLevel(previous_level)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--panel", help="Existing complete source parquet; default latest eligible panel")
    ap.add_argument("--dry-run", action="store_true", help="Read-only footer/guard plan; zero writes")
    args = ap.parse_args(argv)
    result = run(args.panel, dry_run=args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
