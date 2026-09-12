"""Candidate observation, scheduling and explicit promotion in a synthetic store."""
import json

import numpy as np
import pandas as pd
import pytest

from VolumePrediction import rnn_maintenance as maintenance
from VolumePrediction.service import VolumeService


@pytest.fixture
def svc(tmp_path):
    art, raw = tmp_path / "outputs", tmp_path / "raw"
    raw.mkdir()
    for day in ("2026-09-04", "2026-09-08"):
        pd.DataFrame({"ticker": ["AAA"], "v": [100], "vw": [25]}).to_parquet(
            raw / f"grouped_{day}.parquet")
    svc = VolumeService(artifacts_dir=art, raw_dir=raw)
    svc.registry.save({"models": {"rnn_new": {
        "kind": "learned.rnn", "status": "candidate",
        "recorded_at": "2026-09-09T10:30:00",
        "meta": {"incumbent_version": "rnn_old"}}},
        "production": "lgbm_old", "blend": {"enabled": True,
        "rnn_version": "rnn_old", "rnn_full_coverage": True}})
    model = art / "registry/artifacts/rnn_new"
    model.mkdir(parents=True)
    (model / "meta.json").write_text(json.dumps({"version": "rnn_new",
        "kind": "learned.rnn", "trained_through": "2026-09-04",
        "seq_tail_date": "2026-09-09"}))
    forecast = art / "shadow_rnn/candidates/rnn_new/rnn_pred_2026-09-08.parquet"
    forecast.parent.mkdir(parents=True)
    pd.DataFrame({"date": ["2026-09-08"], "ticker": ["AAA"],
        "pred_v": [15.0], "pred_V": [float(np.exp(15))], "pred_eta": [0.1],
        "model_version": ["rnn_new"], "trained_through": ["2026-09-04"]}).to_parquet(forecast)
    return svc


def test_daily_observation_does_not_promote(svc, monkeypatch):
    from VolumePrediction import shadow_rnn, rnn_candidate_review
    before = svc.registry.load()
    called = []
    monkeypatch.setattr(shadow_rnn, "run_daily", lambda **kw: called.append(kw) or 0)
    monkeypatch.setattr(rnn_candidate_review, "review_candidate", lambda v, **kw:
                        {"status": "ready", "incumbent_version": kw["expected_incumbent_version"]})
    result = maintenance.run_candidate_shadows(svc)
    assert result["candidates"]["rnn_new"]["status"] == "ready"
    assert called[0]["version"] == "rnn_new" and called[0]["service"] is svc
    assert svc.registry.load() == before
    assert (svc.art / "shadow_rnn/candidates/rnn_new/review.json").exists()


def test_candidate_failure_is_reported(svc, monkeypatch):
    from VolumePrediction import shadow_rnn
    monkeypatch.setattr(shadow_rnn, "run_daily", lambda **kw: 2)
    result = maintenance.run_candidate_shadows(svc)
    assert result["status"] == "error"
    assert svc.registry.load()["blend"]["rnn_version"] == "rnn_old"


@pytest.mark.parametrize("status,incumbent", [("observing", "rnn_old"),
    ("failed", "rnn_old"), ("ready", "another_rnn")])
def test_promotion_rejects_insufficient_or_stale_review(svc, monkeypatch, status, incumbent):
    from VolumePrediction import rnn_candidate_review
    monkeypatch.setattr(rnn_candidate_review, "review_candidate", lambda *a, **kw:
                        {"status": status, "incumbent_version": incumbent})
    before = svc.registry.load()
    with pytest.raises(ValueError):
        maintenance.promote_candidate("rnn_new", by="test", service=svc)
    assert svc.registry.load() == before


def test_explicit_ready_promotion_has_matching_cache(svc, monkeypatch):
    from VolumePrediction import rnn_candidate_review
    monkeypatch.setattr(rnn_candidate_review, "review_candidate", lambda *a, **kw:
                        {"status": "ready", "incumbent_version": "rnn_old"})
    result = maintenance.promote_candidate("rnn_new", by="test", service=svc)
    assert result["previous_version"] == "rnn_old"
    state = svc.registry.load()
    assert state["blend"]["rnn_version"] == "rnn_new"
    assert state["blend"]["rnn_full_coverage"] is True
    assert state["production"] == "lgbm_old"
    assert state["models"]["rnn_new"]["status"] == "serving"
    cache = svc.art / "registry/artifacts/rnn_new/blend_serve_2026-09-08.parquet"
    assert pd.read_parquet(cache).model_version.eq("rnn_new").all()


def test_monthly_completed_candidate_is_skipped(svc, monkeypatch):
    from VolumePrediction import refreeze_rnn
    monkeypatch.setattr(refreeze_rnn, "run", lambda **kw: pytest.fail("already built"))
    result = maintenance.train_monthly(service=svc, now=pd.Timestamp("2026-09-09 10:03", tz="America/New_York"))
    assert result["status"] == "skipped"


def test_saturday_does_not_compete_with_other_vp_jobs(svc):
    result = maintenance.train_monthly(service=svc, now=pd.Timestamp("2026-09-12 10:03", tz="America/New_York"))
    assert result["status"] == "skipped" and "Saturday" in result["reason"]


def test_old_panel_waits_for_lgbm_panel_refresh(svc, monkeypatch):
    from VolumePrediction import refreeze_rnn
    state = svc.registry.load()
    state["models"] = {}
    svc.registry.save(state)
    monkeypatch.setattr(refreeze_rnn, "plan", lambda **kw: {"asof": "2026-07-31"})
    monkeypatch.setattr(refreeze_rnn, "run", lambda **kw: pytest.fail("panel too old"))
    result = maintenance.train_monthly(service=svc, now=pd.Timestamp("2026-09-09 10:03", tz="America/New_York"))
    assert result["exit_code"] == 3 and result["panel_age_sessions"] > 10
