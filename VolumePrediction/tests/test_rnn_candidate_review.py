"""Synthetic candidate acceptance checks; outputs stay under /tmp/vp_tests/."""
import json
from pathlib import Path
import tempfile

import pandas as pd
import pytest

from VolumePrediction import rnn_candidate_review as review

VERSION = "rnn_candidate_test"
TRAINED = "2026-09-04"
INCUMBENT = "rnn_incumbent_test"
PAIRS = [("2026-09-04", "2026-09-08"), ("2026-09-08", "2026-09-09"),
         ("2026-09-09", "2026-09-10"), ("2026-09-10", "2026-09-11"),
         ("2026-09-11", "2026-09-14"), ("2026-09-14", "2026-09-15")]


@pytest.fixture
def work():
    root = Path("/tmp/vp_tests/rnn_candidate_review")
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root) as td:
        yield Path(td)


def _row(pred, actual, **changes):
    row = {"pred_date": pred, "actual_date": actual, "candidate_version": VERSION,
           "candidate_trained_through": TRAINED, "incumbent_version": INCUMBENT,
           "n_held": 100, "n_held_common": 100, "n_held_expected": 110,
           "held_sample_scope": "common", "rnn_held_mape": 18.0,
           "rnn_held_log_mse": .12, "prod_held_mape": 20.0,
           "prod_held_log_mse": .15, "ma5_held_mape": 24.0,
           "ma5_held_log_mse": .20}
    row.update(changes)
    return row


def _save(work, rows, stamps=None):
    stamps = stamps or {}
    pd.DataFrame(rows).to_csv(work / "rnn_ab_tracking.csv", index=False)
    for row in rows:
        pred = row["pred_date"]
        df = pd.DataFrame({"date": [pred], "model_version": [VERSION],
                           "trained_through": [TRAINED],
                           "generated_at": [stamps.get(pred, f"{pred}T17:34:00-04:00")]})
        df.to_parquet(work / f"rnn_pred_{pred}.parquet", index=False)


def _review(work, **kwargs):
    return review.review_candidate(VERSION, trained_through=TRAINED, candidate_dir=work,
                                   artifact_dir=work / "artifact",
                                   now="2026-09-30T20:00:00-04:00", **kwargs)


def test_five_forward_sessions_ready_without_promotion(work):
    _save(work, [_row(*pair) for pair in PAIRS[:5]])
    result = _review(work)
    assert result["status"] == "ready"
    assert result["summary"]["n_forward_days"] == 5
    assert result["summary"]["n_held_stock_days"] == 500
    assert result["summary"]["required_daily_wins"] == 3
    assert result["promotion_performed"] is False
    assert result["automatic_promotion_authorized"] is False
    assert result["incumbent_version"] == INCUMBENT
    assert all(result["gates"].values())
    assert result["excluded_rows"] == []
    path = review.write_report(result, work / "review.json")
    assert json.loads(path.read_text()) == result
    assert not list(work.glob("*.tmp"))


def test_insufficient_or_missing_forward_days_observing(work):
    assert _review(work)["status"] == "observing"
    _save(work, [_row(*pair) for pair in PAIRS[:4]])
    result = _review(work)
    assert result["status"] == "observing"
    assert result["summary"]["days_remaining"] == 1


def test_backfilled_predictions_do_not_count_as_forward(work):
    rows = [_row(*pair) for pair in PAIRS[:5]]
    _save(work, rows, stamps={pred: "2026-09-20T17:34:00-04:00" for pred, _ in PAIRS[:5]})
    # A forged forward claim in CSV cannot override the persisted forecast.
    df = pd.read_csv(work / "rnn_ab_tracking.csv")
    df["forecast_generated_at"] = "2026-09-01T17:00:00-04:00"
    df.to_csv(work / "rnn_ab_tracking.csv", index=False)
    result = _review(work)
    assert result["status"] == "observing"
    assert result["summary"]["n_forward_days"] == 0
    assert all(r["reasons"] == ["not_forward"] for r in result["excluded_rows"])


@pytest.mark.parametrize("stamp,accepted", [("2026-09-08T09:29:59-04:00", True),
                                            ("2026-09-08T13:29:59Z", True),
                                            ("2026-09-08T09:29:59", True),
                                            ("2026-09-08T09:30:00-04:00", False)])
def test_open_time_cutoff_and_timezones(work, stamp, accepted):
    _save(work, [_row(*PAIRS[0])], stamps={PAIRS[0][0]: stamp})
    assert _review(work)["summary"]["n_forward_days"] == int(accepted)


def test_training_days_wrong_versions_and_holidays_excluded(work):
    rows = [_row("2026-09-03", "2026-09-04"),
            _row(*PAIRS[0], candidate_version="other_candidate"),
            _row("2026-09-04", "2026-09-07")]
    _save(work, rows)
    result = _review(work)
    reasons = {r for row in result["excluded_rows"] for r in row["reasons"]}
    assert {"inside_training_window", "different_candidate_version",
            "not_next_trading_session"} <= reasons
    assert result["summary"]["n_forward_days"] == 0


@pytest.mark.parametrize("changes,reason", [
    ({"n_held": 40, "n_held_common": 40}, "held_coverage_below_minimum"),
    ({"n_held": 20, "n_held_common": 20, "n_held_expected": 20}, "insufficient_held_sample"),
    ({"n_held_common": 101}, "invalid_held_counts"),
    ({"rnn_held_mape": float("nan")}, "invalid_metric:rnn_held_mape"),
    ({"ma5_held_log_mse": float("inf")}, "invalid_metric:ma5_held_log_mse"),
    ({"held_sample_scope": "separate"}, "common_held_sample_unverified"),
    ({"incumbent_version": ""}, "incumbent_version_missing"),
    ({"incumbent_version": VERSION}, "incumbent_is_candidate"),
])
def test_bad_forward_day_blocks_ready_even_with_five_good_days(work, changes, reason):
    _save(work, [_row(*pair) for pair in PAIRS[:5]] + [_row(*PAIRS[5], **changes)])
    result = _review(work)
    assert result["status"] == "failed"
    assert result["summary"]["n_forward_days"] == 5
    assert reason in result["excluded_rows"][0]["reasons"]
    assert not result["gates"]["no_invalid_forward_evidence"]


def test_relative_coverage_drop_is_visible(work):
    _save(work, [_row(*PAIRS[0], n_held_expected=100),
                 _row(*PAIRS[1], n_held=85, n_held_common=85, n_held_expected=100)])
    result = _review(work, max_coverage_drop=.1)
    assert result["summary"]["n_forward_days"] == 1
    assert "held_coverage_drop" in result["excluded_rows"][0]["reasons"]


def test_daily_consistency_and_weighted_aggregate_both_required(work):
    # Four days narrowly win, one larger sample loses heavily. Daily voting
    # alone would pass; pooling common held observations correctly vetoes it.
    rows = [_row(*pair, rnn_held_mape=19.9) for pair in PAIRS[:4]]
    rows.append(_row(*PAIRS[4], n_held=400, n_held_common=400, n_held_expected=400,
                     rnn_held_mape=30, rnn_held_log_mse=.30))
    _save(work, rows)
    result = _review(work)
    assert result["status"] == "failed"
    assert result["gates"]["daily_consistency"]
    assert result["summary"]["weighted_metrics"]["rnn_held_mape"] == pytest.approx(24.95)
    assert not result["gates"]["mape_no_worse_than_incumbent"]
    # Conversely a strong aggregate cannot hide inconsistent daily results.
    rows = [_row(*pair, rnn_held_mape=20.1) for pair in PAIRS[:3]]
    rows += [_row(*pair, rnn_held_mape=5) for pair in PAIRS[3:5]]
    _save(work, rows)
    result = _review(work)
    assert result["gates"]["mape_no_worse_than_incumbent"]
    assert not result["gates"]["daily_consistency"]
    assert result["status"] == "failed"


def test_ma5_tie_fails_and_incumbent_tie_passes(work):
    _save(work, [_row(*pair, prod_held_mape=18, prod_held_log_mse=.12) for pair in PAIRS[:5]])
    assert _review(work)["status"] == "ready"
    _save(work, [_row(*pair, ma5_held_mape=18) for pair in PAIRS[:5]])
    result = _review(work)
    assert result["status"] == "failed"
    assert not result["gates"]["mape_beats_ma5"]


def test_duplicate_dates_and_missing_forecast_cannot_qualify(work):
    rows = [_row(*pair) for pair in PAIRS[:5]]
    _save(work, rows + [rows[-1]])
    (work / f"rnn_pred_{PAIRS[0][0]}.parquet").unlink()
    result = _review(work)
    assert result["summary"]["n_forward_days"] == 3
    reasons = [r for row in result["excluded_rows"] for r in row["reasons"]]
    assert reasons.count("duplicate_actual_date") == 2
    assert "forecast_evidence_missing" in reasons


def test_forecast_metadata_must_match_candidate_and_training(work):
    _save(work, [_row(*PAIRS[0])])
    path = work / f"rnn_pred_{PAIRS[0][0]}.parquet"
    df = pd.read_parquet(path)
    df["model_version"] = INCUMBENT
    df.to_parquet(path, index=False)
    result = _review(work)
    assert "forecast_model_version_mismatch" in result["excluded_rows"][0]["reasons"]
    adir = work / "artifact"
    adir.mkdir()
    (adir / "meta.json").write_text(json.dumps({"version": VERSION, "trained_through": "2026-09-08"}))
    with pytest.raises(ValueError, match="disagrees"):
        _review(work)


def test_actual_session_must_be_finished(work):
    _save(work, [_row(*PAIRS[0])])
    result = review.review_candidate(VERSION, trained_through=TRAINED, candidate_dir=work,
                                    artifact_dir=work / "artifact", now="2026-09-08T15:59:59-04:00")
    assert result["status"] == "observing"
    assert result["excluded_rows"][0]["reasons"] == ["actual_session_not_complete"]


def test_minimum_five_sessions_cannot_be_lowered(work):
    with pytest.raises(ValueError, match="five"):
        _review(work, min_days=4)


def test_expected_incumbent_version_is_enforced(work):
    _save(work, [_row(*PAIRS[0])])
    result = _review(work, expected_incumbent_version="different_incumbent")
    assert result["summary"]["n_forward_days"] == 0
    assert "incumbent_version_mismatch" in result["excluded_rows"][0]["reasons"]


def test_blended_incumbent_identifies_rnn_and_custom_outputs_root(work):
    cdir = work / "shadow_rnn" / "candidates" / VERSION
    cdir.mkdir(parents=True)
    adir = work / "registry" / "artifacts" / VERSION
    adir.mkdir(parents=True)
    (adir / "meta.json").write_text(json.dumps({"version": VERSION, "trained_through": TRAINED}))
    _save(cdir, [_row(*pair, incumbent_version=f"ma5_baseline;{INCUMBENT}") for pair in PAIRS[:5]])
    result = review.review_candidate(VERSION, artifacts_dir=work,
                                     now="2026-09-30T20:00:00-04:00")
    assert result["status"] == "ready"
    assert result["incumbent_version"] == INCUMBENT
    assert result["tracking_csv"] == str(cdir / "rnn_ab_tracking.csv")


def test_cannot_infer_a_missing_incumbent_rnn(work):
    _save(work, [_row(*pair, incumbent_version="ma5_baseline;lgbm_test") for pair in PAIRS[:5]])
    result = _review(work)
    assert result["status"] == "failed"
    assert result["incumbent_version"] is None
    assert not result["gates"]["incumbent_rnn_identified"]


def test_completed_forward_forecast_without_evaluation_blocks_ready(work):
    rows = [_row(*pair) for pair in PAIRS]
    _save(work, rows)
    # Simulate evaluation failing before its tracking row was persisted.
    pd.DataFrame(rows[:-1]).to_csv(work / "rnn_ab_tracking.csv", index=False)
    result = _review(work)
    assert result["summary"]["n_forward_days"] == 5
    assert result["status"] == "failed"
    assert result["excluded_rows"][0]["reasons"] == ["missing_tracking_row"]
    # A replay with no CSV row is never evidence of an actual forward trial.
    path = work / f"rnn_pred_{PAIRS[-1][0]}.parquet"
    frame = pd.read_parquet(path)
    frame["generated_at"] = "2026-09-20T17:34:00-04:00"
    frame.to_parquet(path, index=False)
    assert _review(work)["status"] == "ready"
