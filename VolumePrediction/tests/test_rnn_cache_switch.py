"""Synthetic regressions for a new RNN version reusing same-day predictions."""
import json

import numpy as np
import pandas as pd
import pytest

from VolumePrediction.service import VolumeService, _rnn_cache


def forecast(version="rnn_new", asof="2026-09-08"):
    return pd.DataFrame({"date": [asof], "ticker": ["AAA"], "pred_v": [15.0],
                         "pred_V": [float(np.exp(15))], "pred_eta": [0.1],
                         "model_version": [version], "trained_through": ["2026-09-04"]})


def save(frame, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False)


def test_reject_old_shared_forecast(tmp_path):
    save(forecast("rnn_old"), tmp_path / "shadow_rnn/rnn_pred_2026-09-08.parquet")
    with pytest.raises(RuntimeError, match="version mismatch"):
        _rnn_cache(tmp_path, "rnn_new", "2026-09-08", "2026-09-04")


@pytest.mark.parametrize("bad", ["date", "trained_through", "pred_V", "duplicate", "missing_date"])
def test_reject_invalid_cache(tmp_path, bad):
    frame = forecast()
    if bad == "missing_date":
        frame = frame.drop(columns="date")
    elif bad == "duplicate":
        frame = pd.concat([frame, frame])
    else:
        frame[bad] = np.inf if bad == "pred_V" else "2026-07-31"
    save(frame, tmp_path / "registry/artifacts/rnn_new/blend_serve_2026-09-08.parquet")
    with pytest.raises(RuntimeError, match="no valid RNN cache"):
        _rnn_cache(tmp_path, "rnn_new", "2026-09-08", "2026-09-04")


def test_same_day_switch_uses_candidate_then_is_idempotent(tmp_path, monkeypatch):
    from VolumePrediction import blend_routing, prod_model_rnn, shadow_rnn
    raw, art = tmp_path / "raw", tmp_path / "out"
    for day in ("2026-09-04", "2026-09-08"):
        save(pd.DataFrame({"ticker": ["AAA", "BBB"], "v": [100.0, 200.0],
                           "vw": [20.0, 30.0], "c": [20.0, 30.0]}),
             raw / f"grouped_{day}.parquet")
    rdir = art / "registry/artifacts/rnn_new"
    rdir.mkdir(parents=True)
    (rdir / "meta.json").write_text(json.dumps({"kind": "learned.rnn",
        "version": "rnn_new", "trained_through": "2026-09-04",
        "seq_tail_date": "2026-09-09"}))
    svc = VolumeService(artifacts_dir=art, raw_dir=raw)
    svc.registry.save({"models": {}, "production": "baselines.ma5",
                       "blend": {"enabled": True, "rnn_version": "rnn_new",
                                 "rnn_full_coverage": True}})
    save(forecast("rnn_old"), art / "shadow_rnn/rnn_pred_2026-09-08.parquet")
    save(forecast(), art / "shadow_rnn/candidates/rnn_new/rnn_pred_2026-09-08.parquet")
    monkeypatch.setattr(prod_model_rnn, "serve", lambda *a, **k: pytest.fail("must use cache"))
    monkeypatch.setattr(shadow_rnn, "_held_tickers", lambda *a, **k: {"AAA"})
    monkeypatch.setattr(blend_routing, "recent_measured_adv", lambda *a, **k: {})
    monkeypatch.setattr(blend_routing, "rnn_layer", lambda cov, *a, **k:
                        (set(cov), {"gate_source": "test", "thr": 0, "n_measured_lifted": 0}))
    for _ in range(2):
        assert svc.ops.refresh(fetch=False)["status"] == "ok"
        out = pd.read_parquet(art / "volume_forecast_latest.parquet").set_index("ticker")
        assert out.loc["AAA", "model_version"] == "rnn_new"
        assert out.loc["BBB", "model_version"] == "baselines.ma5"
    published = pd.read_parquet(art / "shadow_rnn/rnn_pred_2026-09-08.parquet")
    assert published.model_version.eq("rnn_new").all()
    assert (rdir / "blend_serve_2026-09-08.parquet").exists()


def test_version_switch_preserves_coverage_and_records_previous(tmp_path):
    art = tmp_path / "outputs"
    (art / "registry/artifacts/rnn_new").mkdir(parents=True)
    svc = VolumeService(artifacts_dir=art)
    svc.registry.save({"models": {}, "production": "lgbm_old",
                      "blend": {"enabled": True, "rnn_version": "rnn_old",
                                "rnn_full_coverage": True}})
    svc.ops.set_blend(True, rnn_version="rnn_new", by="test")
    result = svc.registry.load()
    assert result["production"] == "lgbm_old"
    assert result["blend"]["rnn_full_coverage"] is True
    assert result["blend_changes"][-1]["previous_version"] == "rnn_old"
