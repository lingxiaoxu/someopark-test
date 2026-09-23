"""Crash, timestamp and incremental-input regressions for the W9-only worker."""
import json
import shutil
from pathlib import Path

import numpy as np
import pandas as pd

from crypto_trading.crypto_strategies.w9_rnn_paper import model
from crypto_trading.crypto_strategies.w9_rnn_paper.data import ReceivedTail, RESEARCH


def test_torn_record_preserved_and_next_record_readable(tmp_path):
    path = tmp_path / "live_predictions.jsonl"
    path.write_bytes(b'{"id":1}\n{"id":')
    model.repair_partial_tail(path)
    assert next(tmp_path.glob("*.partial.*")).read_bytes() == b'{"id":'
    model.append_jsonl(path, [{"id": 2}])
    assert [json.loads(line)["id"] for line in path.read_text().splitlines()] == [1, 2]


def test_risk_oof_rejects_published_after_daily_cutoff(tmp_path):
    cutoff = pd.Timestamp("2026-09-16T00:00:00Z").timestamp()
    row = dict(asset="BTC", decision_ts=cutoff - 60, generated_at=cutoff - 58,
               persisted_at=cutoff - 57, model_available_ts=cutoff - 3600,
               model_test_day="2026-09-15", strict_recorded_pit_eligible=True,
               mode="live_paper_forecast")
    records = [row, {**row, "asset": "ETH", "persisted_at": cutoff + .1},
               {**row, "asset": "SOL", "model_available_ts": cutoff + .1},
               {**row, "asset": "XRP", "model_test_day": "2026-09-14"}]
    model.append_jsonl(tmp_path / "live_predictions.jsonl", records)
    assert model._read_live_oof(tmp_path, cutoff).asset.tolist() == ["BTC"]


def test_incremental_reader_retains_partial_and_hides_future(tmp_path):
    day = pd.Timestamp("2026-09-16T00:00:00Z").timestamp()
    folder = tmp_path / "KXBTCPERP"
    folder.mkdir()
    path = folder / "2026-09-16.jsonl"
    def record(ts, quantity):
        return json.dumps({"recv_ts": ts, "m": {"bid": 99, "ask": 100,
            "volume": quantity, "contract_size": 1, "open_interest": 1}}).encode() + b"\n"
    partial = record(day + 30, 4)
    path.write_bytes(record(day + 10, 2) + partial[:25])
    tail = ReceivedTail(tmp_path, "BTC")
    assert tail.read(day, day + 20).recv_ts.tolist() == [day + 10]
    with path.open("ab") as stream:
        stream.write(partial[25:])
    assert tail.read(day, day + 20).recv_ts.tolist() == [day + 10]
    assert tail.read(day, day + 40).recv_ts.tolist() == [day + 10, day + 30]
    assert len(tail.rows) == 2


def test_complete_model_without_active_pointer_can_recover(tmp_path):
    name = "2026-09-15"
    source = RESEARCH / "recent_refit_model/folds" / name
    dest = tmp_path / "models" / name
    dest.mkdir(parents=True)
    for path in list(source.glob("*.npz")) + [source / "metadata.json"]:
        shutil.copyfile(path, dest / path.name)
    saved = pd.read_parquet(source / "causal_predictions.parquet")
    thresholds = {asset: {key: float(group.iloc[0][key]) for key in group
        if key.startswith("prior_calibration_risk_q90_")}
        for asset, group in saved.groupby("asset")}
    model.atomic_json(dest / "risk_thresholds.json", thresholds)
    model.atomic_json(dest / "deployment_provenance.json", {
        "day": name, "training_completed_at": 100.0,
        "model_id": model._model_digest(dest),
    })
    before = model._model_digest(dest)
    result = model.activate_existing(tmp_path, name)
    assert result["model_id"] == before
    assert result["publication_recovery"] is True
    assert result["model_available_ts"] > 100
    assert json.loads((tmp_path / "active_model.json").read_text())["model_id"] == before
    assert model._model_digest(dest) == before
