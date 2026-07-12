"""smart_select tests — synthetic caches in tmp_path, no network."""
import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

from crypto_trading.crypto_common import config as _config
from crypto_trading.crypto_common import smart_select as ss


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(_config, "SIGNALS_DIR", tmp_path / "signals")
    cache = tmp_path / "signals" / "select_cache"
    cache.mkdir(parents=True)
    return cache


def _macro(n=200, seed=3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-01-01", periods=n, freq="D", tz="UTC")
    return pd.DataFrame({
        "btc_rvol": 40 + 5 * rng.standard_normal(n),
        "funding": 1e-4 + 5e-5 * rng.standard_normal(n),
        "basis_dispersion": 10 + 2 * rng.standard_normal(n),
        "btc_dominance": 55 + rng.standard_normal(n),
    }, index=idx)


def _seed_caches(cache, macro, *, good="alpha", bad="beta"):
    """Equity cache where `good` visibly outperforms + candidates file."""
    idx = macro.index
    rng = np.random.default_rng(11)
    eq = pd.DataFrame({
        good: 1000 * (1 + pd.Series(rng.normal(0.004, 0.01, len(idx)), index=idx)).cumprod(),
        bad: 1000 * (1 + pd.Series(rng.normal(-0.002, 0.01, len(idx)), index=idx)).cumprod(),
    })
    eq.to_parquet(cache / "batch_equity_cache.parquet")
    (cache / "top_candidates.json").write_text(json.dumps(
        {"top": [{"name": good, "version": "v1"}, {"name": bad, "version": "v1"}]}))


def test_no_candidates_keeps_current(sandbox):
    result = ss.smart_param_select(date(2026, 7, 1), _macro())
    assert result["smart_select_available"] is False
    assert result["switched"] is False


def test_selection_ranks_known_best_param(sandbox):
    macro = _macro()
    _seed_caches(sandbox, macro)
    result = ss.smart_param_select(date(2026, 7, 1), macro,
                                   current_state={"param_set": "beta",
                                                  "signal_version": "v1"})
    assert result["smart_select_available"] is True
    assert result["best_candidate"] == "alpha"
    assert result["composite_scores"]["alpha"] > result["composite_scores"]["beta"]
    # debounce: first best-day never switches (needs 3 consecutive)
    assert result["switched"] is False


def test_debounced_switch_after_three_best_days(sandbox):
    macro = _macro()
    _seed_caches(sandbox, macro)
    state = {"param_set": "beta", "signal_version": "v1",
             "health": {"_prev_best_param": "alpha", "_consecutive_best_days": 2,
                        "days_since_switch": 30},
             "switch_history": []}
    result = ss.smart_param_select(date(2026, 7, 1), macro, current_state=state)
    assert result["switched"] is True and result["param_set"] == "alpha"
    assert result["switch_reason"] == "param_switch_mcps_drift"


def test_monthly_switch_limit_blocks(sandbox):
    macro = _macro()
    _seed_caches(sandbox, macro)
    hist = [{"date": "2026-07-01", "from_version": "v1", "to_version": "v1"},
            {"date": "2026-07-02", "from_version": "v1", "to_version": "v1"}]
    state = {"param_set": "beta", "signal_version": "v1",
             "health": {"_prev_best_param": "alpha", "_consecutive_best_days": 10,
                        "days_since_switch": 30},
             "switch_history": hist}
    result = ss.smart_param_select(date(2026, 7, 15), macro, current_state=state)
    assert result["switched"] is False               # hard cap: 2/month


def test_regime_cluster_path(sandbox):
    """Centroids built → positioning available → cluster component exercised."""
    macro = _macro()
    _seed_caches(sandbox, macro)
    centroids = ss.build_centroids(macro, n_clusters=3)
    assert centroids is not None and centroids.shape[1] == 4
    pos = ss.macro_positioning(date(2026, 7, 1), macro)
    assert pos["available"] is True
    assert pos["nearest_cluster"] in (0, 1, 2)
    # cluster OOS feeds the composite when present
    (sandbox / "param_oos_by_macro_cluster.json").write_text(json.dumps({
        "alpha": {f"cluster_{pos['nearest_cluster']}": {"mean_oos_sharpe": 2.0}},
        "beta": {f"cluster_{pos['nearest_cluster']}": {"mean_oos_sharpe": -1.0}},
    }))
    result = ss.smart_param_select(date(2026, 7, 1), macro,
                                   current_state={"param_set": "alpha",
                                                  "signal_version": "v1"})
    assert result["best_candidate"] == "alpha"
    assert result["macro_positioning"]["available"] is True


def test_anomaly_detection_far_from_clusters(sandbox):
    macro = _macro()
    ss.build_centroids(macro, n_clusters=3)
    shocked = macro.copy()
    shocked.iloc[-1] = [200.0, 0.05, 100.0, 20.0]    # absurd regime vector
    pos = ss.macro_positioning(date(2026, 7, 19), shocked)
    assert pos["available"] and pos["anomaly"] is True
    assert pos["anomaly_action"] == "auto_conservative"


def test_save_state_persists(sandbox):
    macro = _macro()
    _seed_caches(sandbox, macro)
    result = ss.smart_param_select(date(2026, 7, 1), macro,
                                   current_state={"param_set": "alpha",
                                                  "signal_version": "v1"})
    updated = ss.save_state({}, {**result, "signal_date": date(2026, 7, 1)})
    on_disk = json.loads((sandbox / "selected_param_set.json").read_text())
    assert on_disk["param_set"] == updated["param_set"]
    assert on_disk["top_candidates"][0]["name"] == "alpha"
