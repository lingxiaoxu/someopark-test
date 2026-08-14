"""WF→validate wiring tests — no real data. Verify the bridge writes the
artifacts validate consumes, the FAIL-safe path, and the new gates."""
import importlib

import numpy as np
import pandas as pd
import pytest

from crypto_trading.crypto_common import run_wf as rw
from crypto_trading.crypto_common import validate as val


# ── registry / interface presence ────────────────────────────────────────────

def test_all_equity_strategies_expose_wf_interface():
    for strat, modpath in rw._EQUITY_STRATEGIES.items():
        mod = importlib.import_module(modpath)
        assert callable(mod.wf_run_backtest), strat
        assert isinstance(mod.WF_PARAM_SETS, dict) and mod.WF_PARAM_SETS, strat
        assert callable(mod.wf_prices), strat


def test_registry_covers_all_four_strategies():
    assert set(rw.ALL_STRATEGIES) == {"basis_meanrev", "liq_reversion",
                                      "perp_rotation", "event_perp"}


# ── run_wf FAIL-safe (no folds → no artifacts) ────────────────────────────────

def test_run_wf_failsafe_writes_nothing_on_thin_data(tmp_path, monkeypatch):
    monkeypatch.setattr(rw, "WF_DIR", tmp_path)
    # a fake strategy module: 5 daily rows can't form is_days_min folds
    import types
    fake = types.ModuleType("fake_strat")
    idx = pd.date_range("2026-06-03", periods=5, freq="1D", tz="UTC")
    fake.wf_prices = lambda: pd.DataFrame({"price": range(5)}, index=idx)
    fake.WF_PARAM_SETS = {"a": {"x": 1}}
    fake.wf_run_backtest = lambda p, s, e: {"equity_curve": pd.Series(dtype=float)}
    monkeypatch.setitem(rw._EQUITY_STRATEGIES, "faketest", "fake_strat")
    monkeypatch.setitem(__import__("sys").modules, "fake_strat", fake)
    res = rw.run_wf("faketest")
    assert res["ok"] is False and "insufficient" in res["reason"]
    assert not list(tmp_path.glob("*.csv"))          # nothing written


# ── validate gates: FAIL-safe + evaluate-on-artifacts ────────────────────────

@pytest.fixture
def wf_dir(tmp_path, monkeypatch):
    d = tmp_path / "walk_forward"
    d.mkdir()
    monkeypatch.setattr(val._config, "SIGNALS_DIR", tmp_path)
    return d


def test_validate_liq_reversion_failsafe_when_missing(wf_dir):
    r = val.validate_liq_reversion()
    assert r["PASS"] is False and "insufficient data" in r["reason"]


def test_validate_liq_reversion_evaluates_present_artifact(wf_dir):
    # synthetic OOS equity: strong uptrend → passes Sharpe+DD gates
    idx = pd.date_range("2026-06-03", periods=40, freq="1D", tz="UTC")
    eq = pd.Series(1.003 ** np.arange(40), index=idx, name="equity")
    eq.index.name = "date"
    eq.to_frame().to_csv(wf_dir / "liq_reversion_oos_equity.csv")
    r = val.validate_liq_reversion()
    assert "oos_metrics" in r and "oos_sharpe" in r["gates"]      # gate ran
    assert r["PASS"] is True                                      # clean uptrend passes


def test_validate_event_perp_signal_gate(wf_dir):
    # <6 folds → insufficient
    pd.DataFrame({"fold": [0, 1, 2], "oos_ic": [0.2, 0.15, 0.1]}).to_csv(
        wf_dir / "event_perp_oos_ic.csv", index=False)
    r = val.validate_event_perp()
    assert r["PASS"] is False and "insufficient data" in r["reason"]

    # ≥6 positive folds above floor → PASS
    pd.DataFrame({"fold": range(8),
                  "oos_ic": [0.18, 0.12, 0.09, 0.20, 0.15, 0.11, 0.14, 0.17]}).to_csv(
        wf_dir / "event_perp_oos_ic.csv", index=False)
    r = val.validate_event_perp()
    assert r["PASS"] is True and r["oos_ic"]["n_folds"] == 8

    # ≥6 folds but mean IC below floor → FAIL (not insufficient)
    pd.DataFrame({"fold": range(8), "oos_ic": [0.01] * 8}).to_csv(
        wf_dir / "event_perp_oos_ic.csv", index=False)
    r = val.validate_event_perp()
    assert r["PASS"] is False and "insufficient" not in r.get("reason", "")


def test_run_wf_signal_ic_writes_csv(tmp_path, monkeypatch):
    monkeypatch.setattr(rw, "WF_DIR", tmp_path)
    from crypto_trading.crypto_strategies.event_perp import backtest as ebt
    monkeypatch.setattr(ebt, "_series_days", lambda s: [f"2026-07-{d:02d}" for d in range(1, 11)])
    monkeypatch.setattr(ebt, "run_dislocation_ic",
                        lambda series, **kw: {"IC_spearman_gapz_vs_fwd_convergence": 0.15, "n": 200})
    res = rw.run_wf("event_perp")
    assert res["ok"] and res["n_folds"] > 0
    assert (tmp_path / "event_perp_oos_ic.csv").exists()
