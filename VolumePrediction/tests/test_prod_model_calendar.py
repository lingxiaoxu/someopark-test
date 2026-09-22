"""Serving calendar regression checks; synthetic data only, no model training."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pandas_market_calendars as mcal
import pytest

from VolumePrediction.features import pipeline as fp
from VolumePrediction import prod_model as pm


def _calendar(start="2025-01-01", end="2027-12-31"):
    return mcal.get_calendar("NYSE").schedule(start_date=start, end_date=end).index


def _panel(dates, tickers=("AAA",)):
    return pd.DataFrame(index=pd.MultiIndex.from_product(
        [dates, tickers], names=["date", "ticker"])).sort_index()


@pytest.mark.parametrize("target,announcement,bucket", [
    ("2026-09-17", "2026-08-10", "le_neg4"),
    ("2026-09-17", "2026-09-17", "zero"),
    ("2026-09-17", "2026-10-15", "ge_pos5"),
    ("2026-09-17", "2026-09-16", "neg1"),
    ("2026-09-17", "2026-09-18", "pos1"),
    ("2026-09-18", "2026-09-19", "pos1"),  # Saturday reacts Monday.
    ("2026-09-21", "2026-09-19", "zero"),
    ("2026-06-18", "2026-06-19", "pos1"),  # Juneteenth reacts Monday.
    ("2026-06-22", "2026-06-19", "zero"),
    ("2026-01-02", "2025-12-31", "neg1"),
    ("2026-12-31", "2027-01-01", "pos1"),
])
def test_single_date_matches_full_calendar_earnings(target, announcement, bucket):
    calendar = _calendar()
    histories = {"AAA": [announcement]}
    target = pd.Timestamp(target)
    full = fp.create_earnings_dummies(_panel(calendar), histories)
    single = fp.create_earnings_dummies(
        _panel([target]), histories, trading_calendar=calendar)
    pd.testing.assert_frame_equal(single, full.loc[[target]])
    assert single.iloc[0][f"earn_{bucket}"] == 1.0
    assert single.sum(axis=1).iloc[0] == 1.0


def test_padded_serving_calendar_handles_far_dates_and_missing_info():
    dates = {"PAST": ["2000-01-01"], "FUTURE": ["2030-01-01"],
             "TODAY": ["2026-09-17"]}
    actual = pm._calendar_and_earnings_features(
        ["PAST", "TODAY", "FUTURE", "NONE"], "2026-09-17", dates)
    assert actual.loc["PAST", "earn_le_neg4"] == 1
    assert actual.loc["FUTURE", "earn_ge_pos5"] == 1
    assert actual.loc["TODAY", "earn_zero"] == 1
    assert actual.loc["NONE"].filter(like="earn_").sum() == 0
    assert actual.filter(like="cal_").to_numpy().sum() == 0


def test_future_schedule_takes_precedence_over_previous_announcement():
    actual = pm._calendar_and_earnings_features(
        ["AAA"], "2026-09-17", {"AAA": ["2026-08-10"]},
        future_dates={"AAA": ["2026-10-15"]})
    assert actual.loc["AAA", "earn_ge_pos5"] == 1
    assert actual.loc["AAA", "earn_zero"] == 0


@pytest.mark.parametrize("target,expected", [
    ("2026-01-02", set()),
    ("2026-09-17", set()),
    ("2026-09-18", {"cal_triple_witching"}),
    ("2026-10-16", {"cal_double_witching"}),
    ("2026-06-18", {"cal_triple_witching"}),  # Juneteenth holiday -> prior session.
    ("2026-06-26", {"cal_russell_rebalance"}),
    ("2026-11-27", {"cal_is_early_close"}),
    ("2026-12-31", set()),
])
def test_serving_flags_match_complete_year_calendar(target, expected):
    full = fp.add_calendar_flags(_panel(_calendar()))
    actual = pm._calendar_and_earnings_features(["AAA"], target, {})
    flags = actual.filter(like="cal_").iloc[0]
    reference = full.loc[(pd.Timestamp(target), "AAA")]
    pd.testing.assert_series_equal(flags, reference, check_names=False)
    assert set(flags.index[flags == 1]) == expected


def test_truncated_training_panel_does_not_move_future_expiries_to_last_row():
    dates = _calendar("2026-01-01", "2026-09-04")
    actual = fp.add_calendar_flags(_panel(dates))
    last = actual.loc[(pd.Timestamp("2026-09-04"), "AAA")]
    assert last.sum() == 0
    reference = fp.add_calendar_flags(_panel(_calendar("2026-01-01", "2026-12-31")))
    pd.testing.assert_frame_equal(actual, reference.loc[actual.index])


@pytest.mark.parametrize("target,expected", [
    ("2026-09-17", set()),
    ("2026-09-18", {"cal_triple_witching"}),
    ("2026-06-18", {"cal_triple_witching"}),
    ("2026-11-27", {"cal_is_early_close"}),
])
def test_calendar_function_handles_single_day_request(target, expected):
    actual = fp.add_calendar_flags(_panel([pd.Timestamp(target)])).iloc[0]
    assert set(actual.index[actual == 1]) == expected


def test_invalid_calendar_rejected():
    with pytest.raises(ValueError, match="contain every panel date"):
        fp.create_earnings_dummies(
            _panel([pd.Timestamp("2026-09-17")]), {},
            trading_calendar=[pd.Timestamp("2026-09-18")])
    with pytest.raises(ValueError, match="not an NYSE trading day"):
        pm._calendar_and_earnings_features(["AAA"], "2026-09-19", {})


def test_missing_split_cache_stops_learned_features_before_raw_read(monkeypatch):
    from VolumePrediction.data import polygon_loader as pl
    from VolumePrediction.data import splits_loader as sl

    def unavailable():
        raise RuntimeError("VP splits refresh failed: no valid covering cache")

    def unexpected_raw_calendar(*args, **kwargs):
        pytest.fail("must reject unavailable split adjustments before reading raw data")

    monkeypatch.setattr(sl, "refresh", unavailable)
    monkeypatch.setattr(pl, "trading_days", unexpected_raw_calendar)
    with pytest.raises(RuntimeError, match="no valid covering cache"):
        pm._tech_and_ma5_from_raw({"AAA"}, "2026-09-04", "2026-09-21", pd.DataFrame())


def test_serve_passes_correct_flags_to_model_and_preserves_ticker_alignment(monkeypatch):
    from VolumePrediction.data import earnings_loader as el

    captured = {}

    class Model:
        def predict(self, features):
            captured["features"] = features.copy()
            return pd.Series(0.0, index=features.index)

    per = pd.DataFrame(index=pd.Index(["ZZZ", "AAA"], name="ticker"))
    for col in pm.TECH_COLS:
        per[f"{col}__z_next"] = 0.0
    per["ma5v_next"] = [np.log(200.0), np.log(100.0)]
    per["active"] = True
    feature_cols = pm.TECH_COLS + [f"earn_{b}" for b in fp.EARN_BUCKETS] + [
        "cal_is_early_close", "cal_triple_witching", "cal_double_witching",
        "cal_russell_rebalance"]
    meta = {"first_serve_date": "2026-09-17", "trained_through": "2026-09-16",
            "fund_cols": [], "feature_cols": feature_cols, "version": "synthetic"}
    monkeypatch.setattr(pm, "_load_artifact", lambda _: (Model(), per, meta))
    monkeypatch.setattr(el, "historical_dates", lambda _: {"AAA": ["2026-08-10"]})
    monkeypatch.setattr(el, "future_dates", lambda _: {"ZZZ": ["2026-10-15"]})
    result = pm.serve("/tmp/vp_tests/prod_model_calendar/not-read", "2026-09-17")
    features = captured["features"]
    assert features.loc["AAA", "earn_le_neg4"] == 1
    assert features.loc["ZZZ", "earn_ge_pos5"] == 1
    assert features.filter(like="cal_").to_numpy().sum() == 0
    assert (result["date"] == "2026-09-16").all()
    assert (result["model_version"] == "synthetic").all()
    np.testing.assert_allclose(result.set_index("ticker").loc[["AAA", "ZZZ"], "pred_V"],
                               [100.0, 200.0])
