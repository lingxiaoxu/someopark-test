"""Candidate diagnostic contracts exercised with small in-memory data only."""
import numpy as np
import pandas as pd
import pytest

from VolumePrediction import lgbm_candidate_diagnostic as diagnostic


VERSION = "lgbm_test_candidate"
TRAINED = "2026-09-04"
ASOF = "2026-09-08"
TARGET = "2026-09-09"
OPEN = pd.Timestamp("2026-09-09 09:30", tz="America/New_York")
ARMS = ("candidate", "current", "ma5", "rnn")


def forecast():
    return pd.DataFrame({
        "ticker": ["A", "B"], "date": ASOF, "pred_V": [100., 200.],
        "model_version": VERSION, "trained_through": TRAINED,
        "generated_at": "2026-09-08T17:35:00-04:00",
    })


def validate(frame, **overrides):
    kwargs = dict(version=VERSION, trained_through=TRAINED, asof=ASOF,
                  target=TARGET, market_open=OPEN)
    kwargs.update(overrides)
    return diagnostic.validate_forecast(frame, **kwargs)


@pytest.mark.parametrize("fault", [
    "empty", "duplicate", "version", "date", "trained", "missing_stamp",
    "bad_stamp", "null_stamp",
])
def test_validate_rejects_unverifiable_forecast(fault):
    frame = forecast()
    if fault == "empty":
        frame = frame.iloc[:0]
    elif fault == "duplicate":
        frame.loc[1, "ticker"] = "A"
    elif fault == "version":
        frame.loc[1, "model_version"] = "another_version"
    elif fault == "date":
        frame.loc[1, "date"] = "2026-09-04"
    elif fault == "trained":
        frame.loc[1, "trained_through"] = "2026-08-31"
    elif fault == "missing_stamp":
        frame = frame.drop(columns="generated_at")
    elif fault == "bad_stamp":
        frame.loc[1, "generated_at"] = "not-a-timestamp"
    else:
        frame.loc[1, "generated_at"] = None
    with pytest.raises(ValueError):
        validate(frame)


@pytest.mark.parametrize("stamps,kind", [
    (["2026-09-09T09:29:59", "2026-09-09T09:29:58"], "forward"),
    (["2026-09-09T09:29:59", "2026-09-09T09:30:00"], "replay"),
    (["2026-09-09T09:31:00", "2026-09-09T09:31:00"], "replay"),
    (["2026-09-09T13:29:59Z", "2026-09-09T09:29:58-04:00"], "forward"),
])
def test_forward_uses_latest_generation_and_naive_eastern(stamps, kind):
    frame = forecast()
    frame["generated_at"] = stamps
    pred, evidence = validate(frame)
    assert pred.to_dict() == {"A": 100., "B": 200.}
    assert evidence["forecast_kind"] == kind


def test_target_inside_training_period_is_replay_even_if_generated_early():
    frame = forecast()
    frame["trained_through"] = TARGET
    _, evidence = validate(frame, trained_through=TARGET)
    assert evidence["forecast_kind"] == "replay"


def test_validate_preserves_invalid_numbers_for_common_sample_filter():
    frame = forecast()
    frame["pred_V"] = [np.nan, -1.]
    pred, _ = validate(frame)
    assert list(pred.index) == ["A", "B"]
    assert np.isnan(pred.loc["A"])
    assert pred.loc["B"] == -1.


def test_cohorts_filter_every_arm_once_and_keep_independent_held_denominator():
    ref = pd.DataFrame({
        "actual": [100.] * 7 + [np.nan],
        "lgbm": [110.] * 5 + [0., 110., 110.],
        "ma5": [120.] * 6 + [np.inf, 120.],
    }, index=list("ABCDEFGH"))
    candidate = pd.Series([100., np.nan, 100., 100., 0., 100., 100., 100.],
                          index=ref.index)
    rnn = pd.Series([105., 105., np.inf, 105., 105., 105., 105., 105.],
                    index=ref.index)
    joined, counts = diagnostic.cohorts(ref, candidate, rnn, set(ref.index))
    assert list(joined.index) == ["A", "D"]
    assert set(("actual", *ARMS, "is_held")) <= set(joined.columns)
    assert joined["current"].eq(110.).all()
    assert joined["is_held"].all()
    assert counts["n_reference_valid"] == 5
    assert counts["n_held_expected"] == 5
    assert counts["n_common"] == counts["n_held_common"] == 2
    assert counts["held_coverage"] == pytest.approx(2 / 5)
    assert counts["n_invalid_common"] == 6


def test_missing_candidate_or_rnn_reduces_coverage_without_counting_as_invalid():
    ref = pd.DataFrame({"actual": 100., "lgbm": 110., "ma5": 120.},
                       index=list("ABCD"))
    candidate = pd.Series(100., index=list("ABC"))
    rnn = pd.Series(105., index=list("ABD"))
    joined, counts = diagnostic.cohorts(ref, candidate, rnn, set(ref.index))
    assert set(joined.index) == {"A", "B"}
    assert counts["n_held_expected"] == 4
    assert counts["held_coverage"] == pytest.approx(.5)
    assert counts["n_invalid_common"] == 0


def cohort(n=40, *, candidate=1.05, current=1.10, ma5=1.20, rnn=1.):
    actual = np.arange(100., 100. + n)
    return pd.DataFrame({
        "actual": actual, "candidate": actual * candidate,
        "current": actual * current, "ma5": actual * ma5,
        "rnn": actual * rnn, "is_held": True,
    }, index=[f"T{i}" for i in range(n)])


def day_rows(frame, *, asof=ASOF, target=TARGET, coverage=1.):
    rows = diagnostic.evaluate_cohort(frame)
    return [{**row, "asof": asof, "target": target,
             "held_coverage": coverage} for row in rows]


def test_evaluate_uses_correct_scope_and_unrounded_metrics():
    frame = cohort(n=3)
    frame["actual"] = [3., 7., 11.]
    frame["candidate"] = [3.000001, 7.123456789, 12.345678901]
    frame["is_held"] = [True, False, True]
    rows = {row["scope"]: row for row in diagnostic.evaluate_cohort(frame)}
    assert set(rows) == {"all_common", "held_common"}
    for scope, subset in (("all_common", frame),
                          ("held_common", frame[frame.is_held])):
        row = rows[scope]
        assert row["n"] == len(subset)
        for arm in ARMS:
            mape = np.mean(np.abs(subset[arm] / subset.actual - 1.)) * 100.
            log_mse = np.mean((np.log(subset[arm]) - np.log(subset.actual)) ** 2)
            assert row[f"{arm}_mape"] == pytest.approx(mape, rel=1e-13, abs=1e-13)
            assert row[f"{arm}_log_mse"] == pytest.approx(log_mse, rel=1e-13, abs=1e-13)


@pytest.mark.parametrize("rnn", [1., 100.])
def test_rnn_performance_is_reference_only(rnn):
    result = diagnostic.summarize(day_rows(cohort(rnn=rnn)))
    assert result["status"] == "diagnostic_pass"
    assert result["pooled"]["held_common"]["n"] == 40


def test_summary_weights_stock_days_instead_of_taking_daily_mean():
    first = day_rows(cohort(n=300, candidate=1.))
    second = day_rows(cohort(n=30, candidate=1.30),
                      asof="2026-09-09", target="2026-09-10")
    result = diagnostic.summarize(first + second)
    assert result["status"] == "diagnostic_pass"
    pooled = result["pooled"]["held_common"]
    assert pooled["n"] == 330
    assert pooled["candidate_mape"] == pytest.approx(30. * 30 / 330)
    assert pooled["candidate_log_mse"] == pytest.approx(np.log(1.30) ** 2 * 30 / 330)


@pytest.mark.parametrize("frame,coverage", [
    (cohort(n=40), .799),
    (cohort(n=29), 1.),
    (cohort(n=40, candidate=1.20, current=1.10), 1.),
    (cohort(n=40, candidate=1.20, current=1.30, ma5=1.20), 1.),
    (cohort(n=40, candidate=.90, current=1.11), 1.),
    (cohort(n=40, candidate=1.11, current=.90), 1.),
])
def test_summary_rejects_bad_coverage_small_sample_or_failed_held_gates(frame, coverage):
    result = diagnostic.summarize(day_rows(frame, coverage=coverage))
    assert result["status"] == "diagnostic_fail"


def test_one_bad_day_cannot_hide_behind_large_pooled_sample():
    rows = day_rows(cohort(n=300)) + day_rows(
        cohort(n=29), asof="2026-09-09", target="2026-09-10")
    assert diagnostic.summarize(rows)["status"] == "diagnostic_fail"


def test_gate_uses_held_scope_and_accepts_exact_coverage_boundary():
    frame = cohort(n=100)
    frame.loc[frame.index[30:], "is_held"] = False
    frame.loc[frame.index[30:], "candidate"] = frame.actual.iloc[30:] * 100.
    assert diagnostic.summarize(day_rows(frame, coverage=.8))["status"] == "diagnostic_pass"


def test_empty_rows_cannot_pass():
    assert diagnostic.summarize([])["status"] == "diagnostic_fail"


def test_no_held_sample_cannot_pass_with_good_all_stock_results():
    frame = cohort()
    frame["is_held"] = False
    assert diagnostic.summarize(day_rows(frame))["status"] == "diagnostic_fail"
