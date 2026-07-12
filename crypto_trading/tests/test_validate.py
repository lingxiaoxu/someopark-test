"""Validation-gate tests — synthetic WF/backtest fixtures in tmp_path, no
network. The FAIL-safe (insufficient data ⇒ FAIL, never vacuous PASS) is the
load-bearing behavior."""
import json

import numpy as np
import pandas as pd
import pytest

from crypto_trading.crypto_common import config as _config
from crypto_trading.crypto_common import validate as v


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(_config, "SIGNALS_DIR", tmp_path / "signals")
    monkeypatch.setattr(_config, "CRYPTO_ROOT", tmp_path)
    (tmp_path / "signals" / "walk_forward").mkdir(parents=True)
    return tmp_path


def _write_oos(tmp_path, strategy, daily_ret, n=120, benches=None, start="2026-03-01",
               vol=0.005):
    """Noisy synthetic OOS equity — constant returns would give a float-noise
    std and an astronomical Sharpe, defeating the floor test."""
    rng = np.random.default_rng(42)
    idx = pd.date_range(start, periods=n, freq="D", tz="UTC")
    eq = 1000 * (1 + pd.Series(rng.normal(daily_ret, vol, n), index=idx)).cumprod()
    df = pd.DataFrame({"date": idx, "equity": eq.values})
    for name, r in (benches or {}).items():
        df[name] = 1000 * (1 + pd.Series(rng.normal(r, vol, n),
                                         index=idx)).cumprod().values
    df.to_csv(tmp_path / "signals" / "walk_forward" / f"{strategy}_oos_equity.csv",
              index=False)


def _write_backtest(tmp_path, strategy, fee_scenario="projected"):
    d = tmp_path / "signals" / strategy / "backtests"
    d.mkdir(parents=True, exist_ok=True)
    (d / "backtest_x.json").write_text(json.dumps(
        {"fee_scenario": fee_scenario, "net_pnl": 1.0}))


# ── FAIL-safe: missing artifacts can never PASS ─────────────────────────────

def test_basis_no_artifacts_fails_with_insufficient_data(sandbox):
    report = v.validate_basis()
    assert report["PASS"] is False
    assert "insufficient data" in report["reason"]


def test_rotation_no_artifacts_fails(sandbox):
    report = v.validate_perp_rotation()
    assert report["PASS"] is False and "insufficient data" in report["reason"]


def test_basis_no_live_fills_fails_unless_waived(sandbox):
    _write_oos(sandbox, "basis_meanrev", 0.002)      # strong OOS
    _write_backtest(sandbox, "basis_meanrev")
    report = v.validate_basis()                      # slippage not evaluable
    assert report["PASS"] is False and "slippage" in report["reason"]
    report2 = v.validate_basis(allow_no_live=True)   # explicit paper waiver
    assert report2["PASS"] is True
    assert report2["gates"]["slippage"]["ok"] and "WAIVED" in report2["gates"]["slippage"]["detail"]


# ── basis gate logic ────────────────────────────────────────────────────────

def test_basis_low_sharpe_fails(sandbox):
    _write_oos(sandbox, "basis_meanrev", 0.00001)    # ~flat → Sharpe below floor
    _write_backtest(sandbox, "basis_meanrev")
    report = v.validate_basis(allow_no_live=True)
    assert report["PASS"] is False
    assert not report["gates"]["oos_sharpe"]["ok"]


def test_basis_zero_fee_backtest_flagged(sandbox):
    _write_oos(sandbox, "basis_meanrev", 0.002)
    _write_backtest(sandbox, "basis_meanrev", fee_scenario="zero")
    report = v.validate_basis(allow_no_live=True)
    assert report["PASS"] is False                   # projected-fee requirement bites
    assert not report["gates"]["fee_scenario"]["ok"]


# ── rotation gate logic ─────────────────────────────────────────────────────

def test_rotation_beats_both_benchmarks_passes(sandbox):
    _write_oos(sandbox, "perp_rotation", 0.003,
               benches={"btc_hodl": 0.001, "ew_basket": 0.0005})
    report = v.validate_perp_rotation()
    assert report["PASS"] is True
    assert report["beats"]["btc_hodl"]["sharpe"] and report["beats"]["ew_basket"]["cagr"]


def test_rotation_losing_to_one_benchmark_fails(sandbox):
    _write_oos(sandbox, "perp_rotation", 0.001,
               benches={"btc_hodl": 0.003, "ew_basket": 0.0005})   # loses to HODL
    report = v.validate_perp_rotation()
    assert report["PASS"] is False
    assert not report["gates"]["beat_benchmarks"]["ok"]


def test_rotation_missing_benchmarks_fails_safe(sandbox):
    _write_oos(sandbox, "perp_rotation", 0.003)      # no benchmark columns,
    report = v.validate_perp_rotation()              # no candle store in sandbox
    assert report["PASS"] is False and "benchmark" in report["reason"]
