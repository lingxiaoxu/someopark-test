"""Independent W9 rolling-volume worker: local data, paper forecasts only.

Numerical training follows the audited recent-refit experiment. Historical
bootstrap OOF forecasts are explicitly retrospective training material and
never enter the append-only live publication stream.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

for _key in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
    os.environ[_key] = "1"

import numpy as np
import pandas as pd
import torch

from . import training
from .data import ASSETS, MinuteStore, REPO, RESEARCH, SEED_SHA256

DEFAULT_RUNTIME = REPO / "crypto_trading/trading_signals/w9_rnn_paper"
RESEARCH_MODELS = RESEARCH / "recent_refit_model"
torch.set_num_threads(1)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    with tmp.open("w") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def _json_value(value):
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value) if math.isfinite(value) else None
    return value


def append_jsonl(path, records):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        for record in records:
            clean = {key: _json_value(value) for key, value in record.items()}
            stream.write(json.dumps(clean, separators=(",", ":"), allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def repair_partial_tail(path):
    """Worker-lock owner preserves and removes only an incomplete final record."""
    path = Path(path)
    if not path.exists() or not path.stat().st_size:
        return
    with path.open("rb+") as stream:
        stream.seek(-1, os.SEEK_END)
        if stream.read(1) == b"\n":
            return
        size = stream.tell()
        window = 4096
        while True:
            start = max(0, size - window)
            stream.seek(start)
            tail = stream.read()
            index = tail.rfind(b"\n")
            if index >= 0 or start == 0:
                cut = start + index + 1 if index >= 0 else 0
                break
            window *= 2
        stream.seek(cut)
        evidence = stream.read()
        quarantine = path.with_name(path.name + f".partial.{time.time_ns()}")
        quarantine.write_bytes(evidence)
        stream.truncate(cut)
        stream.flush()
        os.fsync(stream.fileno())


def _scale(values, mean, scale):
    """Match StandardScaler's two in-place operations, including float32 rounding."""
    result = values.copy()
    result -= mean
    result /= scale
    return result


class LoadedModel:
    """Load only this crypto experiment's explicit independent NPZ format."""
    def __init__(self, fold_dir):
        self.path = Path(fold_dir)
        self.metadata = json.loads((self.path / "metadata.json").read_text())
        with np.load(self.path / "scalers_and_ridge.npz", allow_pickle=False) as arrays:
            self.arrays = {key: arrays[key].copy() for key in arrays.files}
        self.models = {}
        for variant in ("fixed_rnn", "tuned_rnn"):
            config = self.metadata["final_configs"][variant]
            self.models[variant] = []
            for seed in (0, 1, 2):
                network = training.VolumeLSTM(len(training.FEATURES), config["hidden"])
                with np.load(self.path / f"{variant}_seed{seed}.npz", allow_pickle=False) as arrays:
                    network.load_state_dict({key: torch.from_numpy(arrays[key].copy()) for key in arrays.files})
                network.eval()
                self.models[variant].append(network)
        threshold_path = self.path / "risk_thresholds.json"
        if threshold_path.exists():
            self.thresholds = json.loads(threshold_path.read_text())
        else:
            historical = pd.read_parquet(self.path / "causal_predictions.parquet")
            self.thresholds = {asset: {key: float(group.iloc[0][key]) for key in group
                if key.startswith("prior_calibration_risk_q90_")}
                for asset, group in historical.groupby("asset")}
        if self.metadata["risk_available"]:
            for asset in ASSETS:
                for label in ("baseline", "augmented", "fixed_rnn"):
                    value = float(self.thresholds[asset][f"prior_calibration_risk_q90_{label}"])
                    if not math.isfinite(value) or value < 0:
                        raise ValueError("nonfinite/negative required prior risk threshold")

    def predict_rows(self, minute_frame, cutoff_ts):
        """Pure inference, no file publication and no target access in features."""
        if minute_frame.empty:
            return pd.DataFrame()
        training.CUTOFF = cutoff_ts
        frames = training.prepare(minute_frame)
        X, metas = [], []
        for asset, original in frames.items():
            g = original.copy()
            if len(g) < 25:
                continue
            norm = self.metadata["train_volume_normalizers"][asset]
            for key, rawkey in (("log_volume1", "volume"), ("log_volume5", "volume5"), ("log_volume15", "volume15")):
                g[key] = np.log1p(g[rawkey] / norm)
            g.loc[~g.history_valid, training.FEATURES] = np.nan
            windows = np.lib.stride_tricks.sliding_window_view(g[training.FEATURES].to_numpy("float32"), 10, axis=0).transpose(0, 2, 1)
            columns = ["asset", "bar_end_ts", "decision_ts", "feature_first_bar_end_ts", "feature_last_recorded_receipt_ts", "strict_feature_available"]
            meta = g.iloc[9:][columns].copy().reset_index(drop=True)
            meta["pred_ma5"] = g.iloc[9:].log_volume5.to_numpy()
            meta["volume_normalizer"] = norm
            good = np.isfinite(windows).all(axis=(1, 2)) & meta.strict_feature_available.to_numpy() & meta.decision_ts.le(cutoff_ts).to_numpy()
            X.append(windows[good].copy())
            metas.append(meta[good])
        if not X or not sum(len(piece) for piece in X):
            return pd.DataFrame()
        rawx = np.concatenate(X)
        result = pd.concat(metas, ignore_index=True)
        x = np.clip(_scale(rawx, self.arrays["feature_mean"], self.arrays["feature_scale"]), -10, 10).astype("float32")
        flat = x.reshape(len(x), -1)
        offset = result.pred_ma5.to_numpy()
        for variant in ("fixed_rnn", "tuned_rnn"):
            config = self.metadata["final_configs"][variant]
            predictions = [training.predict(network, x) * config["target_scale"] + config["target_mean"] + (offset if config["target"] == "residual" else 0) for network in self.models[variant]]
            unconstrained = np.mean(predictions, axis=0)
            physical = np.maximum(unconstrained, 0)
            result[f"pred_volume_{variant}_unconstrained_log"] = unconstrained
            result[f"pred_volume_{variant}"] = physical
            result[f"pred_volume_{variant}_contracts"] = np.expm1(np.minimum(physical, 50)) * result.volume_normalizer.to_numpy()
        residual = flat @ self.arrays["ridge_coef"] + self.arrays["ridge_intercept"]
        ridge_log = np.maximum(residual * self.arrays["target_residual_scale"] + self.arrays["target_residual_mean"] + offset, 0)
        result["pred_volume_ridge_contracts"] = np.expm1(np.minimum(ridge_log, 50)) * result.volume_normalizer.to_numpy()
        risk_available = bool(self.metadata["risk_available"])
        result["risk_available"] = risk_available
        if risk_available:
            base = _scale(flat, self.arrays["risk_base_mean"], self.arrays["risk_base_scale"]).astype("float32")
            for label, variant in (("baseline", None), ("augmented", "tuned_rnn"), ("fixed_rnn", "fixed_rnn")):
                features = base if variant is None else np.column_stack([base, result[f"pred_volume_{variant}"].to_numpy() - offset])
                state = self.metadata["risk_models"][label]
                transformed = _scale(features, np.asarray(state["mean"]), np.asarray(state["scale"]))
                result[f"pred_abs_return_{label}"] = np.maximum(transformed @ np.asarray(state["coef"], dtype=features.dtype) + np.asarray(state["intercept"], dtype=features.dtype), 0)
                result[f"prior_calibration_risk_q90_{label}"] = result.asset.map({a: self.thresholds[a][f"prior_calibration_risk_q90_{label}"] for a in ASSETS})
        else:
            for label in ("baseline", "augmented", "fixed_rnn"):
                result[f"pred_abs_return_{label}"] = np.nan
                result[f"prior_calibration_risk_q90_{label}"] = np.nan
        result["strict_recorded_pit_eligible"] = result.strict_feature_available
        result["model_test_day"] = self.metadata["test_day"]
        result["model_train_end"] = pd.Timestamp(self.metadata["test_day"], tz="UTC").timestamp()
        result["prediction_ts"] = result.decision_ts
        result["source_grid_end_ts"] = result.bar_end_ts
        return result


def _model_digest(folder):
    digest = hashlib.sha256()
    for path in sorted(Path(folder).glob("*.npz")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    digest.update((Path(folder) / "metadata.json").read_bytes())
    threshold = Path(folder) / "risk_thresholds.json"
    if threshold.exists():
        digest.update(threshold.name.encode())
        digest.update(threshold.read_bytes())
    return digest.hexdigest()


class ModelService:
    def __init__(self, runtime_dir=DEFAULT_RUNTIME):
        self.runtime = Path(runtime_dir).resolve()
        self._manifest = None
        self._model = None
        self._published = set()
        self._attempted = None
        path = self.runtime / "live_predictions.jsonl"
        if path.exists():
            with path.open() as stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                        self._published.add((row["asset"], row["decision_ts"]))
                    except (ValueError, KeyError):
                        continue

    def predict_available(self, minute_frame, now_ts=None):
        now_ts = time.time() if now_ts is None else now_ts
        manifest_path = self.runtime / "active_model.json"
        if not manifest_path.exists():
            return []
        manifest = json.loads(manifest_path.read_text())
        if manifest["model_available_ts"] > now_ts:
            return []
        model_day = pd.Timestamp(manifest["day"], tz="UTC").timestamp()
        # Do not substitute yesterday's model while today's fit is pending.
        if not model_day + 300 <= now_ts < model_day + 86400:
            return []
        if self._manifest is None or self._manifest["model_id"] != manifest["model_id"]:
            folder = Path(manifest["path"]).resolve()
            if not folder.is_relative_to(self.runtime / "models"):
                raise ValueError("model outside isolated runtime")
            if _model_digest(folder) != manifest["model_id"]:
                raise ValueError("published model digest differs from stored files")
            self._model = LoadedModel(folder)
            self._manifest = manifest
        attempt = (manifest["model_id"], int(now_ts // 60))
        if self._attempted == attempt:
            return []
        self._attempted = attempt
        output = self._model.predict_rows(minute_frame, now_ts)
        if output.empty:
            return []
        output = output[(output.decision_ts <= now_ts) & (output.decision_ts > now_ts - 60)]
        records = []
        for row in output.to_dict("records"):
            identity = (row["asset"], row["decision_ts"])
            if identity in self._published:
                continue
            # generated_at is the true execution time, never the nominal minute.
            generated = time.time()
            if not model_day + 300 <= generated < model_day + 86400 or not 0 <= generated - row["decision_ts"] < 60:
                continue
            row.update(generated_at=generated, model_available_ts=manifest["model_available_ts"],
                       model_prepared_at=manifest["prepared_at"], model_id=manifest["model_id"],
                       mode="live_paper_forecast", availability_mode="actual_local_publication",
                       source="kalshi_prod_markets_postresponse_recorded_cumulative_volume_delta")
            if not all(math.isfinite(row[k]) for k in ("pred_abs_return_augmented", "prior_calibration_risk_q90_augmented")):
                continue
            records.append(row)
        if not records:
            return []
        append_jsonl(self.runtime / "raw_predictions.jsonl", records)
        persisted = time.time()  # AFTER the underlying prediction records fsync.
        for row in records:
            row["persisted_at"] = persisted
        append_jsonl(self.runtime / "live_predictions.jsonl", records)
        for row in records:
            self._published.add((row["asset"], row["decision_ts"]))
        return records


def _read_live_oof(runtime, cutoff_ts):
    path = runtime / "live_predictions.jsonl"
    rows = []
    if path.exists():
        with path.open() as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("mode") != "live_paper_forecast" or row.get("strict_recorded_pit_eligible") is not True:
                    continue
                try:
                    decision, generated, persisted, available = (float(row[key]) for key in ("decision_ts", "generated_at", "persisted_at", "model_available_ts"))
                    nominal_day = pd.Timestamp(decision, unit="s", tz="UTC").strftime("%Y-%m-%d")
                    valid = all(math.isfinite(value) for value in (decision, generated, persisted, available)) and available <= generated <= persisted < cutoff_ts and 0 <= generated - decision < 60 and row["model_test_day"] == nominal_day
                except (ValueError, KeyError, TypeError):
                    valid = False
                if valid:
                    rows.append(row)
    return pd.DataFrame(rows)


def bootstrap_oof(runtime, data, now_ts):
    """Keep retrospective calibration material distinct from live records."""
    path = runtime / "bootstrap_oof.parquet"
    provenance_path = runtime / "bootstrap_provenance.json"
    if path.exists():
        if not provenance_path.exists():
            os.replace(path, path.with_name(path.name + f".unpublished.{time.time_ns()}"))
        else:
            provenance = json.loads(provenance_path.read_text())
            if hashlib.sha256(path.read_bytes()).hexdigest() != provenance["oof_sha256"]:
                raise ValueError("bootstrap OOF evidence digest changed")
            past = pd.read_parquet(path)
            if past.duplicated(["asset", "decision_ts"]).any() or not past.strict_recorded_pit_eligible.eq(True).all() or not past.model_train_end.le(past.decision_ts).all():
                raise ValueError("bootstrap OOF evidence is not causal/unique")
            return past
    past = pd.read_parquet(RESEARCH_MODELS / "causal_predictions.parquet")
    past["forecast_origin"] = "audited_retrospective_research_bootstrap_NOT_LIVE"
    day = pd.Timestamp(now_ts, unit="s", tz="UTC").floor("D").timestamp()
    last = float(past.decision_ts.max())
    # Only the already-trained research model's own test day may be extended.
    # A later deployment needs separately reconstructed daily OOF folds.
    checkpoint_day = str(past.model_test_day.max())
    checkpoint_start = pd.Timestamp(checkpoint_day, tz="UTC").timestamp()
    if day > checkpoint_start + 86400:
        raise ValueError("research bootstrap is more than one day old; reconstruct missing daily OOF folds explicitly")
    piece = data[(data.bar_end_ts >= last - 26 * 60) & (data.bar_end_ts < day)].copy()
    if not piece.empty:
        historical = LoadedModel(RESEARCH_MODELS / "folds" / checkpoint_day).predict_rows(piece, day - 1)
        historical = historical[(historical.decision_ts > last) & (historical.decision_ts < day)]
        historical["forecast_origin"] = "audited_prior_daily_model_extension_for_training_NOT_LIVE"
        historical["retrospective_generated_at"] = time.time()
        past = pd.concat([past, historical], ignore_index=True)
    assert not past.duplicated(["asset", "decision_ts"]).any()
    tmp = path.with_suffix(".tmp.parquet")
    past.to_parquet(tmp, index=False)
    os.replace(tmp, path)
    atomic_json(runtime / "bootstrap_provenance.json", {
        "created_at": time.time(), "source": str(RESEARCH_MODELS), "source_minute_sha256": SEED_SHA256,
        "semantics": "Retrospective daily out-of-sample predictions, used ONLY to seed risk calibration; no historical paper trades or live publication.",
        "rows": len(past), "last_decision_ts": float(past.decision_ts.max()),
        "oof_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    })
    return past


def train(runtime_dir=DEFAULT_RUNTIME, now_ts=None):
    runtime = Path(runtime_dir).resolve()
    if runtime == RESEARCH or runtime.is_relative_to(RESEARCH):
        raise ValueError("refusing to write research evidence")
    runtime.mkdir(parents=True, exist_ok=True)
    now_ts = time.time() if now_ts is None else now_ts
    day = pd.Timestamp(now_ts, unit="s", tz="UTC").floor("D").timestamp()
    name = pd.Timestamp(day, unit="s", tz="UTC").strftime("%Y-%m-%d")
    if now_ts < day + 300:
        raise ValueError("daily fit starts only after 00:05 UTC")
    final = runtime / "models" / name
    lock = runtime / "training.lock"
    lock_handle = lock.open("a+")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        lock_handle.close()
        raise RuntimeError("another W9-only training job owns training.lock") from error
    lock_handle.seek(0)
    lock_handle.truncate()
    lock_handle.write(str(os.getpid()))
    lock_handle.flush()
    start = time.time()
    work = runtime / f"training-{name}-{os.getpid()}"
    try:
        if final.exists():
            return activate_existing(runtime, name)
        try:
            os.nice(10)
        except OSError:
            pass
        atomic_json(runtime / "training_status.json", {"status": "training", "day": name, "started_at": start})
        data = MinuteStore(runtime).history(now_ts)
        prior = bootstrap_oof(runtime, data, now_ts)
        live = _read_live_oof(runtime, day)
        if not live.empty:
            prior = pd.concat([prior, live], ignore_index=True).drop_duplicates(["asset", "decision_ts"], keep="last")
        prior = prior[(prior.decision_ts < day) & (prior.decision_ts >= day - 7 * 86400)]
        # The calibration needs every one of the preceding seven UTC dates.
        dates = set(pd.to_datetime(prior.decision_ts, unit="s", utc=True).dt.strftime("%Y-%m-%d"))
        required = {pd.Timestamp(day - offset * 86400, unit="s", tz="UTC").strftime("%Y-%m-%d") for offset in range(1, 8)}
        if not required <= dates:
            raise ValueError("insufficient preceding seven daily OOF forecast dates")
        work.mkdir(parents=True)
        training.HERE = work
        training.CUTOFF = now_ts
        frames = training.prepare(data)
        output, metadata = training.fit_fold(frames, day, [prior])
        folder = work / "folds" / name
        # All five prior calibration cohorts exist even when today's first
        # observed minutes are missing for one asset. Never derive model
        # availability from coverage of today's diagnostic inference rows.
        calibration = pd.read_parquet(folder / "risk_calibration_predictions.parquet")
        thresholds = {asset: {key: float(group.iloc[0][key]) for key in group if key.startswith("prior_calibration_risk_q90_")}
                      for asset, group in calibration.groupby("asset")}
        atomic_json(folder / "risk_thresholds.json", thresholds)
        LoadedModel(folder)  # Shapes and all five finite thresholds before publication.
        # Audited numerical fit writes test diagnostics; these are never live.
        atomic_json(folder / "deployment_provenance.json", {
            "training_started_at": start, "training_completed_at": time.time(), "day": name,
            "source_minute_sha256": SEED_SHA256, "source_cutoff_ts": now_ts,
            "historical_test_files": "retrospective fit diagnostics, never live forecasts or paper admissions",
            "bootstrap_rows_in_risk_window": int(prior.forecast_origin.fillna("").str.contains("bootstrap|training").sum()) if "forecast_origin" in prior else 0,
            "actual_live_rows_in_risk_window": int(prior.get("mode", pd.Series(dtype=str)).eq("live_paper_forecast").sum()),
            "numerical_kernel": "copied audited recent_refit_model with exact grid/scalers/candidates/seeds/OOF risk",
            "model_id": _model_digest(folder),
        })
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(folder, final)
        prepared = time.time()
        manifest = {"day": name, "path": str(final), "model_id": _model_digest(final),
                    "prepared_at": prepared, "model_available_ts": prepared,
                    "mode": "paper_only", "selected_config": metadata["selected_config"],
                    "selected_epoch": metadata["selected_epoch"], "calibration_rows": metadata["calibration_rows"]}
        atomic_json(runtime / "active_model.json", manifest)
        atomic_json(runtime / "training_status.json", {"status": "ready", **manifest, "duration_s": time.time() - start})
        return manifest
    except Exception as error:
        atomic_json(runtime / "training_status.json", {"status": "failed", "day": name,
                    "started_at": start, "failed_at": time.time(), "error": repr(error)})
        raise
    finally:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()


def activate_existing(runtime, name):
    """Recover a fully written immutable model after interrupted publication."""
    final = Path(runtime) / "models" / name
    model = LoadedModel(final)
    metadata = model.metadata
    provenance = json.loads((final / "deployment_provenance.json").read_text())
    digest = _model_digest(final)
    expected_digest = provenance.get("model_id")
    if expected_digest is None:
        # An early first-deployment generation can have an independently
        # recorded digest supplement; never silently bless the current bytes.
        supplement = json.loads((final / "publication_integrity.json").read_text())
        expected_digest = supplement["model_id"]
    if expected_digest != digest:
        raise ValueError("saved generation digest differs from pre-publication evidence")
    day = pd.Timestamp(name, tz="UTC").timestamp()
    if metadata["test_day"] != name or provenance["day"] != name or not metadata["risk_available"] or not metadata["strict_model_fit_available"]:
        raise ValueError("incomplete or inconsistent saved daily model")
    if not metadata["refit_train_last_receipt"] < day or not metadata["validation_last_receipt"] < day or not metadata["risk_calibration_last_receipt"] < day:
        raise ValueError("saved daily model violates fit receipt cutoffs")
    now = time.time()
    manifest = {"day": name, "path": str(final.resolve()), "model_id": digest,
                "prepared_at": provenance["training_completed_at"], "model_available_ts": now,
                "mode": "paper_only", "selected_config": metadata["selected_config"],
                "selected_epoch": metadata["selected_epoch"], "calibration_rows": metadata["calibration_rows"],
                "publication_recovery": True}
    atomic_json(Path(runtime) / "active_model.json", manifest)
    return manifest


def worker(runtime_dir=DEFAULT_RUNTIME, poll_seconds=2.0):
    runtime = Path(runtime_dir).resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    singleton = (runtime / "model_worker.lock").open("a+")
    try:
        fcntl.flock(singleton.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        singleton.close()
        raise RuntimeError("W9 model worker already running") from error
    singleton.seek(0)
    singleton.truncate()
    singleton.write(str(os.getpid()))
    singleton.flush()
    for filename in ("raw_predictions.jsonl", "live_predictions.jsonl"):
        repair_partial_tail(runtime / filename)
    try:
        os.nice(10)
    except OSError:
        pass
    service = ModelService(runtime)
    store = MinuteStore(runtime)
    trainer = None
    trainer_log = None
    retry_after = 0
    while True:
        now = time.time()
        current_day = pd.Timestamp(now, unit="s", tz="UTC").floor("D")
        name = current_day.strftime("%Y-%m-%d")
        pointer = runtime / "active_model.json"
        active_day = json.loads(pointer.read_text())["day"] if pointer.exists() else None
        if (runtime / "models" / name).exists() and active_day != name:
            activate_existing(runtime, name)
        if trainer is not None and trainer.poll() is not None:
            retry_after = now + (300 if trainer.returncode else 0)
            trainer = None
            if trainer_log:
                trainer_log.close()
                trainer_log = None
        if trainer is None and now >= max(current_day.timestamp() + 300, retry_after) and not (runtime / "models" / name).exists():
            trainer_log = (runtime / "training.log").open("a")
            trainer = subprocess.Popen([sys.executable, "-m", "crypto_trading.crypto_strategies.w9_rnn_paper.model", "train", "--runtime-dir", str(runtime)], stdout=trainer_log, stderr=subprocess.STDOUT)
        try:
            rows = service.predict_available(store.refresh(now), now)
            status = {"status": "running", "heartbeat_ts": time.time(), "published_this_cycle": len(rows),
                      "model_id": service._manifest["model_id"] if service._manifest else None,
                      "model_day": service._manifest["day"] if service._manifest else None,
                      "training_pid": trainer.pid if trainer else None,
                      "latest_source_grid": store._last_grid, "mode": "paper_only", "pid": os.getpid()}
        except Exception as error:
            status = {"status": "error", "heartbeat_ts": time.time(), "error": repr(error), "pid": os.getpid(), "mode": "paper_only"}
        atomic_json(runtime / "model_status.json", status)
        time.sleep(poll_seconds)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("bootstrap", "train", "worker"))
    parser.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    args = parser.parse_args()
    if args.command == "worker":
        worker(args.runtime_dir, args.poll_seconds)
    else:
        print(json.dumps(train(args.runtime_dir), indent=2), flush=True)


if __name__ == "__main__":
    main()
