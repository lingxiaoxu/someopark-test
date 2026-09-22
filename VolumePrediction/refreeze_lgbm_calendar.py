"""Rebuild the frozen 0904 calendar inputs and train an isolated LGBM candidate.

This targeted rebuild preserves the original training cutoff and every unaffected
input. It neither fetches revised market data nor promotes a model. The caller
coordinates this memory-intensive operation with other local training jobs.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import pickle
import subprocess
from zoneinfo import ZoneInfo

from VolumePrediction.common import OUT, REPO

VERSION = "lgbm_prod_20260904_calfix_20260917"
INCUMBENT = "lgbm_prod_20260904"
SOURCE = OUT / "panel/panel_20260905_140236_prod_20260904.parquet"
PANEL = OUT / "panel/panel_20260917_calfix_prod_20260904.parquet"


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def stamp():
    return datetime.now(ZoneInfo("America/New_York")).isoformat()


def atomic_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    os.replace(tmp, path)


def assert_no_competing_training():
    """Read process state; never kill jobs or clear another process's lock."""
    result = subprocess.run(["ps", "-axo", "pid=,pgid=,command="],
                            capture_output=True, text=True, check=True)
    procs = [s.strip().split(None, 2) for s in result.stdout.splitlines()]
    procs = [p for p in procs if len(p) == 3]
    live = {int(p[0]) for p in procs}
    groups = {int(p[1]) for p in procs}
    for base in (REPO / "pipeline_state", REPO / "pipeline_state/daily_pipeline.lock"):
        for name in ("runner.pid", "wrapper.pid", "runner.pgid"):
            p = base / name
            if p.exists():
                value = p.read_text().strip()
                if value.isdigit() and int(value) in (groups if name.endswith("pgid") else live):
                    raise RuntimeError(f"Pairs pipeline is active ({name})")
    for pid, _, command in procs:
        if int(pid) in {os.getpid(), os.getppid()}:
            continue
        if any(token in command for token in (
            "-m VolumePrediction.refreeze", "-m VolumePrediction.pipeline_build",
            "-m VolumePrediction.daily_update", "pipeline_runner.sh")):
            raise RuntimeError(f"Competing training/update process: pid={pid}")


def run():
    from VolumePrediction.rebuild_calendar_panel import rebuild
    from VolumePrediction import prod_model
    from VolumePrediction.outputs.recorder import Registry
    import pandas as pd

    run_dir = OUT / "refreeze_lgbm" / VERSION
    run_dir.mkdir(parents=True, exist_ok=True)
    status_path = run_dir / "status.json"
    registry = Registry()
    before = registry.load()
    assert before.get("production") == INCUMBENT
    art = OUT / "registry/artifacts" / VERSION
    staging = art.with_name("." + VERSION + ".staging")
    if art.exists() or staging.exists() or VERSION in before.get("models", {}):
        raise FileExistsError("Candidate or staging already exists; inspect status before retrying")

    old_art = OUT / "registry/artifacts" / INCUMBENT
    old_meta = json.loads((old_art / "meta.json").read_text())
    old_hashes = {name: sha(old_art / name)
                  for name in ("model.pkl", "meta.json", "per_ticker.parquet")}
    status = {"version": VERSION, "incumbent": INCUMBENT, "status": "starting",
              "started_at": stamp(), "trained_through": "2026-09-04",
              "source_panel": str(SOURCE), "rebuilt_panel": str(PANEL),
              "original_artifact_sha256": old_hashes,
              "scope": "rebuild calendar columns; original cutoff/features/model protocol",
              "performance_gate": {"kind": "retrospective_diagnostic_only",
                  "held_min_per_day": 30, "held_min_coverage": 0.8,
                  "mape_and_log_mse": "both <= incumbent and both < MA5",
                  "rnn_reference_only": True, "automatic_promotion": False}}
    atomic_json(status_path, status)
    with (run_dir / ".train.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            assert_no_competing_training()
            status.update(status="rebuilding_panel", updated_at=stamp())
            atomic_json(status_path, status)
            provenance = rebuild(SOURCE, PANEL)
            status.update(status="training", panel_provenance=provenance, updated_at=stamp())
            atomic_json(status_path, status)
            assert_no_competing_training()
            meta = prod_model.freeze(PANEL, asof="2026-09-04", art_dir=staging,
                                     version=VERSION)
            assert meta["trained_through"] == old_meta["trained_through"]
            for key in ("n_train_rows", "n_tickers", "feature_cols", "fund_cols", "target",
                        "pred_rule", "first_serve_date"):
                assert meta[key] == old_meta[key], f"Protocol mismatch: {key}"
            pd.testing.assert_frame_equal(pd.read_parquet(staging / "per_ticker.parquet"),
                                          pd.read_parquet(old_art / "per_ticker.parquet"),
                                          check_exact=True)
            with (staging / "model.pkl").open("rb") as f:
                model = pickle.load(f)
            with (old_art / "model.pkl").open("rb") as f:
                old_model = pickle.load(f)
            assert model._cols == old_model._cols and model.param_count() == old_model.param_count()
            assert model._est.get_params() == old_model._est.get_params()
            assert model._est.booster_.params["objective"] == "regression"
            assert sha(staging / "model.pkl") != old_hashes["model.pkl"]
            assert all(sha(old_art / name) == digest for name, digest in old_hashes.items())
            current = registry.load()
            assert current["production"] == before["production"]
            assert current["blend"] == before["blend"]
            audit = {**status, "status": "trained_validated", "validated_at": stamp(),
                     "artifact_metadata": meta,
                     "artifact_sha256": {n: sha(staging / n) for n in old_hashes},
                     "per_ticker_state_identical": True, "training_protocol_identical": True,
                     "production_unchanged": True}
            atomic_json(staging / "training_audit.json", audit)
            staging.rename(art)
            registry.record_model(VERSION, kind="learned.lgbm", trained_through="2026-09-04",
                                  status="candidate", metrics={},
                                  meta={"repair": "calendar_boundary_20260917",
                                        "source_version": INCUMBENT, "panel": str(PANEL),
                                        "training_audit": str(art / "training_audit.json"),
                                        "acceptance": "pending_diagnostic"})
            status.update(status="trained_validated", artifact=str(art), completed_at=stamp(),
                          n_train_rows=meta["n_train_rows"], n_features=len(meta["feature_cols"]))
            atomic_json(status_path, status)
            print(json.dumps(status, ensure_ascii=False, default=str), flush=True)
        except Exception as exc:
            status.update(status="failed", failed_at=stamp(), error_type=type(exc).__name__,
                          error=str(exc))
            atomic_json(status_path, status)
            raise


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run()
