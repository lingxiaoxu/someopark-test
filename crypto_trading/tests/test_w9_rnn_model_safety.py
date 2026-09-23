"""Independent deployment checks; no writes outside pytest temporary paths."""
import ast
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from crypto_trading.crypto_strategies.w9_rnn_paper import model, training

REPO = Path(__file__).resolve().parents[2]
RESEARCH = REPO/"crypto_trading/trading_signals/reports/w9_rnn_pit_expanded_20260916T001013Z"


def test_numerical_training_policy_exactly_matches_audited_kernel():
    """The deployed model must remain the selected study, not a new variant."""
    def definitions(path):
        return {node.name: ast.dump(node, include_attributes=False)
                for node in ast.parse(path.read_text()).body
                if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
    old = definitions(RESEARCH/"recent_refit_model/research.py")
    new = definitions(REPO/"crypto_trading/crypto_strategies/w9_rnn_paper/training.py")
    for name in ("dataset", "VolumeLSTM", "fit_candidate", "save_model", "causal_only", "fit_fold"):
        assert new[name] == old[name], name


def test_saved_daily_weights_reproduce_research_predictions_and_ignore_future(monkeypatch):
    cutoff = pd.Timestamp("2026-09-15T14:01:00Z").timestamp()
    minute_frame = pd.read_parquet(RESEARCH/"history_audit/recorded_market_observable_1m.parquet",
                                  filters=[("bar_end_ts", ">=", cutoff-3600), ("bar_end_ts", "<=", cutoff+600)])
    loaded = model.LoadedModel(RESEARCH/"recent_refit_model/folds/2026-09-15")
    monkeypatch.setattr(training, "CUTOFF", cutoff)
    ordinary = loaded.predict_rows(minute_frame, cutoff).set_index(["asset", "decision_ts"]).sort_index()
    assert set(ordinary.index.get_level_values("asset")) == set(training.ASSETS)
    expected = pd.read_parquet(RESEARCH/"recent_refit_model/folds/2026-09-15/causal_predictions.parquet")
    expected = expected.set_index(["asset", "decision_ts"]).loc[ordinary.index]
    columns = ["pred_volume_tuned_rnn", "pred_volume_fixed_rnn", "pred_volume_ridge_contracts",
               "pred_abs_return_augmented", "pred_abs_return_baseline", "pred_abs_return_fixed_rnn",
               "prior_calibration_risk_q90_augmented"]
    assert np.isfinite(ordinary[columns]).all().all()
    for name in columns:
        np.testing.assert_allclose(ordinary[name], expected[name], rtol=1e-5, atol=2e-5, err_msg=name)
    poisoned = minute_frame.copy()
    future = poisoned.bar_end_ts+60 > cutoff
    poisoned.loc[future, "volume"] = 1e20
    poisoned.loc[future, "mid_close"] *= 10000
    poisoned.loc[future, "spread_bp"] = 99
    without_future = loaded.predict_rows(minute_frame.loc[~future], cutoff).set_index(["asset", "decision_ts"]).sort_index()
    with_poison = loaded.predict_rows(poisoned, cutoff).set_index(["asset", "decision_ts"]).sort_index()
    pd.testing.assert_frame_equal(ordinary, without_future)
    pd.testing.assert_frame_equal(ordinary, with_poison)
    assert not any(name.startswith("target_") for name in ordinary.columns)


def test_model_digest_binds_thresholds_not_just_neural_weights(tmp_path):
    np.savez(tmp_path/"weights.npz", fake=np.array([1.]))
    (tmp_path/"metadata.json").write_text(json.dumps({"model": "fixture"}))
    path = tmp_path/"risk_thresholds.json"
    path.write_text(json.dumps({"BTC": {"prior_calibration_risk_q90_augmented": 1.}}))
    before = model._model_digest(tmp_path)
    path.write_text(json.dumps({"BTC": {"prior_calibration_risk_q90_augmented": 1000.}}))
    after = model._model_digest(tmp_path)
    assert before != after, "risk threshold controls position size and belongs to model identity"
