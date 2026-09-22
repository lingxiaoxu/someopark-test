"""Small file-level diagnostic contract tests; never load a real model or raw data."""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from VolumePrediction import lgbm_candidate_diagnostic as diag


def put_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(diag, "REPO", tmp_path)
    pairs = [("2026-09-04", "2026-09-08", pd.Timestamp("2026-09-08 09:30", tz="America/New_York")),
             ("2026-09-08", "2026-09-09", pd.Timestamp("2026-09-09 09:30", tz="America/New_York"))]
    monkeypatch.setattr(diag, "_sessions", lambda start, end: pairs)
    new, old = tmp_path / "lgbm_cal_fixed", tmp_path / "lgbm_current"
    for art in (new, old):
        put_json(art / "meta.json", {"kind": "learned.lgbm", "version": art.name,
                  "trained_through": "2026-09-04", "feature_cols": ["cal_x"]})
        (art / "model.pkl").write_bytes(b"synthetic model; never unpickle")
        (art / "per_ticker.parquet").write_bytes(b"synthetic artifact; never read")
    refroot, rnnroot, advice = tmp_path / "reference", tmp_path / "rnn", tmp_path / "advice"
    refroot.mkdir()
    (refroot / "vp_lgbm_diagnostic_20260917_fixed.py").write_text("# synthetic reference producer\n")
    targets, reports = {}, []
    tickers = [f"T{i:02}" for i in range(40)]
    actual = pd.Series(np.arange(100., 140.), index=pd.Index(tickers, name="ticker"))
    for asof, target, _ in pairs:
        frame = pd.DataFrame({"actual": actual, "lgbm": actual * 1.1, "ma5": actual * 1.2})
        p = refroot / "after_fix" / f"diagnostic_{target}.parquet"
        p.parent.mkdir(exist_ok=True)
        frame.to_parquet(p)
        report = {"target": target, "asof": asof, "scope": "all", "n": len(frame)}
        for arm in ("lgbm", "ma5"):
            report[arm + "_mape"] = float(np.abs(frame[arm] / frame.actual - 1).mean() * 100)
            report[arm + "_log_mse"] = float(((np.log(frame[arm]) - np.log(frame.actual)) ** 2).mean())
        reports.append(report)
        targets[target] = pd.DataFrame({"ticker": tickers, "date": asof,
            "pred_V": actual.to_numpy() * 1.01, "model_version": new.name,
            "trained_through": "2026-09-04", "generated_at": "2026-09-17T20:00:00-04:00"})
        rnn = targets[target].assign(model_version="rnn_test", pred_V=actual.to_numpy() * 5,
                                    generated_at=asof + "T17:33:00-04:00")
        rnnroot.mkdir(exist_ok=True)
        rnn.to_parquet(rnnroot / f"rnn_pred_{asof}.parquet", index=False)
        for stem in ("pairs_mrpt_advice", "pairs_mtfs_advice", "aiss_advice", "aeus_advice", "ssrs_advice"):
            put_json(advice / f"{stem}_{asof}.json", {"holdings": [{"ticker": t} for t in tickers]})
    put_json(refroot / "after_fix.json", {"model_version": old.name, "trained_through": "2026-09-04",
        "evaluation_kind": "retrospective_diagnostic_not_prospective_acceptance", "rows": reports})
    return {"candidate_art": new, "current_art": old, "out_dir": tmp_path / "result",
            "reference_dir": refroot, "rnn_dir": rnnroot, "rnn_version": "rnn_test",
            "advice_dir": advice, "targets": targets}


def test_run_serves_only_candidate_and_preserves_all_inputs(inputs):
    targets = inputs.pop("targets")
    before = {str(p): diag._sha(p) for p in inputs["candidate_art"].parent.rglob("*") if p.is_file()}
    calls = []

    class FakeServer:
        def __init__(self, art, out, sources, future_calendar):
            assert art == inputs["candidate_art"]
            self.out = out

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def serve(self, target):
            plan = json.loads((self.out / "plan.json").read_text())
            assert plan["min_held"] == 30 and plan["min_coverage"] == .8
            assert plan["promotion_authorized"] is False
            calls.append(target)
            return targets[target].copy()

    result = diag.run(**inputs, server_factory=FakeServer)
    assert calls == ["2026-09-08", "2026-09-09"]
    assert result["status"] == "diagnostic_pass"
    assert result["pooled"]["held_common"]["n"] == 80
    assert result["evaluation_kind"] == "retrospective_diagnostic_not_forward_acceptance"
    assert result["promotion_authorized"] is False
    assert all(e["candidate"]["forecast_kind"] == "replay" for e in result["evidence"])
    assert all(e["current_reference"]["generated_at"] is None for e in result["evidence"])
    assert result["rnn_forward_targets"] == calls
    assert all(len(e["cohort_source"]["sha256"]) == 64 for e in result["evidence"])
    assert all(diag._sha(Path(p)) == digest for p, digest in before.items())
    assert (inputs["out_dir"] / "metrics.csv").exists()
    assert (inputs["out_dir"] / "result.json").exists()


def test_bad_stored_prediction_fails_before_candidate_serve(inputs):
    inputs.pop("targets")
    p = inputs["rnn_dir"] / "rnn_pred_2026-09-08.parquet"
    wrong = pd.read_parquet(p).assign(model_version="another_version")
    wrong.to_parquet(p, index=False)
    with pytest.raises(ValueError, match="model_version"):
        diag.run(**inputs, server_factory=lambda *a: pytest.fail("Must preflight stored evidence first"))
    error = json.loads((inputs["out_dir"] / "failure.json").read_text())
    assert error["status"] == "diagnostic_error"
    assert error["completed_targets"] == []


def test_output_cannot_overwrite_existing_evidence(inputs):
    inputs.pop("targets")
    inputs["out_dir"].mkdir()
    with pytest.raises(FileExistsError):
        diag.run(**inputs, server_factory=lambda *a: pytest.fail("Unexpected serve"))
