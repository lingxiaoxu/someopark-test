"""Version isolation and incumbent comparisons, using only small temporary fixtures."""
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from VolumePrediction import prod_model_rnn as pmr
from VolumePrediction import shadow_rnn as shadow


SESSIONS = ["2026-09-03", "2026-09-04", "2026-09-08", "2026-09-09", "2026-09-10"]


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def prediction(version, asof="2026-09-04", factor=1.1, n=40):
    actual = np.arange(100., 100. + n)
    return pd.DataFrame({
        "date": asof, "ticker": [f"T{i}" for i in range(n)],
        "pred_V": actual * factor, "pred_v": np.log(actual * factor),
        "pred_eta": np.log(factor), "model_version": version,
        "trained_through": "2026-09-04",
        "generated_at": "2026-09-04T17:34:00-04:00",
    })


def write_prediction(path, frame):
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False)


@pytest.fixture
def env(tmp_path, monkeypatch):
    art = tmp_path / "outputs"
    current = "rnn_current"
    state = {"blend": {"enabled": True, "rnn_version": current,
                       "rnn_full_coverage": True}}
    actual = pd.DataFrame({"ticker": [f"T{i}" for i in range(40)],
                           "dollar_volume": np.arange(100., 140.)})
    raw = ["2026-09-04", "2026-09-08"]
    svc = SimpleNamespace(art=art, registry=SimpleNamespace(load=lambda: state),
                          _raw_dates=lambda: raw,
                          _load_day=lambda date: actual.copy())
    for version in (current, "rnn_candidate", "rnn_other"):
        write_json(art / "registry" / "artifacts" / version / "meta.json",
                   {"version": version, "trained_through": "2026-09-04",
                    "seq_tail_date": "2026-09-04"})
    monkeypatch.setattr(pmr, "next_trading_day",
                        lambda asof: SESSIONS[SESSIONS.index(asof) + 1])
    monkeypatch.setattr(shadow, "_ab_mu", lambda: (None, "test_no_econ"))
    return SimpleNamespace(svc=svc, raw=raw, state=state, actual=actual, art=art)


def test_default_follows_current_and_explicit_versions_are_isolated(env):
    globals_before = shadow.CANDIDATE, shadow.SHADOW_DIR, shadow.TRACK_CSV
    formal = shadow._context(service=env.svc)
    assert formal.version == "rnn_current" and formal.serving
    assert formal.directory == env.art / "shadow_rnn"
    for version in ("rnn_candidate", "rnn_current"):
        ctx = shadow._context(version=version, service=env.svc)
        assert ctx.directory == env.art / "shadow_rnn" / "candidates" / version
        assert ctx.serving == (version == "rnn_current")
    assert shadow._context(version="rnn_current", isolated=False,
                           service=env.svc).directory == formal.directory
    assert globals_before == (shadow.CANDIDATE, shadow.SHADOW_DIR, shadow.TRACK_CSV)
    env.state.clear()
    assert shadow._context(service=env.svc).version == shadow.CANDIDATE.name


@pytest.mark.parametrize("kwargs", [
    {"version": "../escape"},
    {"version": "rnn_candidate", "isolated": False},
    {"version": "rnn_candidate", "shadow_dir": "formal"},
])
def test_candidate_cannot_escape_into_formal_outputs(env, kwargs):
    if kwargs.get("shadow_dir") == "formal":
        kwargs["shadow_dir"] = env.art / "shadow_rnn"
    with pytest.raises(ValueError):
        shadow._context(service=env.svc, **kwargs)


def test_candidate_rolls_while_incumbent_is_live_and_rerun_is_idempotent(env, monkeypatch):
    formal = env.art / "shadow_rnn" / "rnn_pred_2026-09-04.parquet"
    write_prediction(formal, prediction("rnn_current"))
    original = formal.read_bytes()
    incumbent_meta = env.art / "registry/artifacts/rnn_current/meta.json"
    original_meta = incumbent_meta.read_bytes()
    called = []

    def serve(art, target, update_state=False):
        assert Path(art).name == "rnn_candidate" and update_state is True
        called.append(target)
        meta_path = Path(art) / "meta.json"
        meta = json.loads(meta_path.read_text())
        assert meta["seq_tail_date"] == SESSIONS[SESSIONS.index(target) - 1]
        meta["seq_tail_date"] = target
        write_json(meta_path, meta)
        frame = prediction("rnn_candidate", SESSIONS[SESSIONS.index(target) - 1])
        frame["generated_at"] = "2026-09-09T01:00:00"  # legacy naive ET is preserved with zone
        return frame

    monkeypatch.setattr(pmr, "serve", serve)
    monkeypatch.setattr(shadow, "evaluate", lambda *args, **kwargs: None)
    assert shadow.run_daily(version="rnn_candidate", service=env.svc) == 0
    assert called == ["2026-09-08", "2026-09-09"]
    assert shadow.run_daily(version="rnn_candidate", service=env.svc) == 0
    assert len(called) == 2
    directory = env.art / "shadow_rnn/candidates/rnn_candidate"
    assert len(list(directory.glob("rnn_pred_*.parquet"))) == 2
    frame = pd.read_parquet(directory / "rnn_pred_2026-09-08.parquet")
    assert frame.generated_at.iloc[0].endswith("-04:00")
    assert formal.read_bytes() == original and incumbent_meta.read_bytes() == original_meta


def test_actual_serving_version_only_reads_matching_predictions(env, monkeypatch):
    env.raw[:] = ["2026-09-08"]
    path = env.art / "shadow_rnn/rnn_pred_2026-09-08.parquet"
    frame = prediction("rnn_current", "2026-09-08")
    write_prediction(path, frame)
    meta = env.art / "registry/artifacts/rnn_current/meta.json"
    before = meta.read_bytes()
    monkeypatch.setattr(pmr, "serve", lambda *a, **k: pytest.fail("formal shadow rolled state"))
    assert shadow.run_daily(service=env.svc) == 0
    assert shadow.run_daily(version="rnn_current", service=env.svc) == 0
    copied = env.art / "shadow_rnn/candidates/rnn_current/rnn_pred_2026-09-08.parquet"
    pd.testing.assert_frame_equal(pd.read_parquet(copied), frame)
    assert meta.read_bytes() == before


@pytest.mark.parametrize("fault", ["missing", "wrong_version", "wrong_date"])
def test_formal_missing_or_wrong_predictions_fail_without_serving(env, monkeypatch, fault):
    env.raw[:] = ["2026-09-08"]
    if fault != "missing":
        frame = prediction("wrong" if fault == "wrong_version" else "rnn_current",
                           "2026-09-04" if fault == "wrong_date" else "2026-09-08")
        write_prediction(env.art / "shadow_rnn/rnn_pred_2026-09-08.parquet", frame)
    monkeypatch.setattr(pmr, "serve", lambda *a, **k: pytest.fail("unsafe state roll"))
    assert shadow.run_daily(service=env.svc) == 2


def test_candidate_repeated_explicit_day_reads_own_cache(env, monkeypatch):
    ctx = shadow._context(version="rnn_candidate", service=env.svc)
    write_json(ctx.art / "meta.json", {"version": ctx.version, "trained_through": "2026-09-04",
                                       "seq_tail_date": "2026-09-08"})
    frame = prediction(ctx.version)
    write_prediction(ctx.directory / "rnn_pred_2026-09-04.parquet", frame)
    monkeypatch.setattr(pmr, "serve", lambda *a, **k: pytest.fail("candidate rolled twice"))
    pd.testing.assert_frame_equal(shadow.serve_candidate("2026-09-04", context=ctx), frame)
    (ctx.directory / "rnn_pred_2026-09-04.parquet").unlink()
    with pytest.raises(FileNotFoundError, match="prediction missing"):
        shadow.serve_candidate("2026-09-04", context=ctx)


def test_candidate_comparison_uses_common_valid_held_sample_and_real_incumbent(env, monkeypatch):
    ctx = shadow._context(version="rnn_candidate", service=env.svc)
    candidate, incumbent = prediction(ctx.version), prediction("rnn_current", factor=1.05)
    candidate.loc[0, "pred_V"] = np.nan
    incumbent.loc[1, "pred_V"] = np.inf
    floor = env.actual.set_index("ticker").dollar_volume * 1.2
    floor.loc["T2"] = 0
    write_prediction(ctx.directory / "rnn_pred_2026-09-04.parquet", candidate)
    write_prediction(env.art / "history/volume_forecast_2026-09-04.parquet", incumbent)
    write_prediction(env.art / "history/counterfactual_noblend_2026-09-04.parquet",
                     prediction("lgbm_counterfactual", factor=1.3))
    write_json(env.art / "adapters/aeus_advice_2026-09-04.json",
               {"holdings": [{"ticker": f"T{i}"} for i in range(40)]})
    monkeypatch.setattr(shadow, "_ma5_floor", lambda svc, date: floor)
    row = shadow.evaluate("2026-09-08", context=ctx)
    assert row["candidate_version"] == "rnn_candidate"
    assert row["incumbent_version"] == "rnn_current"
    assert row["n_held_expected"] == 38
    assert row["n_held"] == row["n_held_common"] == row["n_common"] == 37
    assert row["held_sample_scope"] == "common"
    assert row["candidate_held_mape"] == 10 and row["incumbent_held_mape"] == 5
    assert row["rnn_wins"] is True  # candidate beats cf, but loses the live RNN
    assert row["candidate_beats_incumbent"] is False
    assert row["paired_incumbent_held_n"] == 37
    assert row["paired_incumbent_held_winrate"] == 0
    assert row["forecast_generated_at"] == "2026-09-04T17:34:00-04:00"
    assert row["candidate_trained_through"] == "2026-09-04"


def test_evaluation_does_not_use_stale_production_or_wrong_candidate(env):
    ctx = shadow._context(version="rnn_candidate", service=env.svc)
    path = ctx.directory / "rnn_pred_2026-09-04.parquet"
    write_prediction(path, prediction(ctx.version))
    write_prediction(env.art / "history/volume_forecast_2026-09-03.parquet",
                     prediction("rnn_current", "2026-09-03"))
    assert shadow.evaluate("2026-09-08", context=ctx) is None
    assert shadow.evaluate("2026-09-09", context=ctx) is None
    write_prediction(path, prediction("wrong"))
    with pytest.raises(ValueError, match="version mismatch"):
        shadow.evaluate("2026-09-08", context=ctx)


def test_tracking_is_version_scoped_and_schema_migration_aligns_columns(env):
    one = shadow._context(version="rnn_candidate", service=env.svc)
    two = shadow._context(version="rnn_other", service=env.svc)
    row = {"actual_date": "2026-09-08", "candidate_version": one.version, "rnn_held_mape": 12}
    shadow.append_track(row, context=one)
    shadow.append_track({"rnn_held_mape": 13, "candidate_version": one.version,
                         "actual_date": "2026-09-09", "new_column": "yes"}, context=one)
    frame = pd.read_csv(one.track)
    assert frame.actual_date.tolist() == ["2026-09-08", "2026-09-09"]
    assert frame.rnn_held_mape.tolist() == [12, 13]
    assert shadow._already_tracked("2026-09-08", context=one)
    assert not shadow._already_tracked("2026-09-08", context=two)
    assert not (env.art / "shadow_rnn/rnn_ab_tracking.csv").exists()
    with pytest.raises(ValueError, match="candidate_version"):
        shadow.append_track(row, context=two)


def test_formal_evaluation_preserves_old_model_history_after_switch(env, monkeypatch):
    formal = env.art / "shadow_rnn"
    old_path = formal / "rnn_pred_2026-09-03.parquet"
    write_prediction(old_path, prediction("rnn_retired", "2026-09-03"))
    write_prediction(formal / "rnn_pred_2026-09-04.parquet", prediction("rnn_current"))
    before = old_path.read_bytes()
    calls = []

    def evaluate(actual, **kwargs):
        calls.append((actual, kwargs["pred_date"], kwargs["context"].version))
        return {"actual_date": actual, "pred_date": kwargs["pred_date"],
                "candidate_version": kwargs["context"].version}

    monkeypatch.setattr(shadow, "evaluate", evaluate)
    assert shadow.run_daily(eval_only=True, service=env.svc) == 0
    assert calls == [("2026-09-08", "2026-09-04", "rnn_current")]
    assert old_path.read_bytes() == before
    assert shadow.run_daily(eval_only=True, service=env.svc) == 0
    assert len(calls) == 1


def test_all_five_strategy_holdings_and_ssrs_actual_etfs_are_included(env):
    root = env.art / "adapters"
    write_json(root / "pairs_mrpt_advice_2026-09-04.json",
               {"positions": [{"s1": "MR1", "s2": "MR2"}]})
    write_json(root / "pairs_mtfs_advice_2026-09-03.json",
               {"positions": [{"s1": "MT1", "s2": "MT2"}]})
    write_json(root / "aiss_advice_2026-09-04.json", {"holdings": [{"ticker": "AI"}]})
    write_json(root / "aeus_advice_2026-09-04.json", {"holdings": [{"ticker": "AE"}]})
    write_json(root / "aeus_advice_2026-09-08.json", {"holdings": [{"ticker": "FUTURE"}]})
    write_json(root / "ssrs_advice_2026-09-04.json",
               {"etfs": [{"etf": "XLK", "shares": 5}, {"etf": "WATCH", "shares": 0}]})
    assert shadow._held_tickers("2026-09-04", artifacts_dir=env.art) == {
        "MR1", "MR2", "MT1", "MT2", "AI", "AE", "XLK"}
