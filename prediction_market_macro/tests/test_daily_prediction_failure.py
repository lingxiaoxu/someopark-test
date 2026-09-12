"""Daily refresh keeps independent work running after strict prediction failure."""
import importlib
import sys
from types import ModuleType, SimpleNamespace

import pytest

from prediction_market_macro.ops import decide_all, pnl, predict_all, refresh, trading_kalshi
from prediction_market_macro.tests.test_predict_maintenance_coverage import prediction_book


@pytest.mark.parametrize("failed", [set(), {"KXCPI"}, {"KXCPI", "KXU3"}])
def test_daily_refresh_skips_trading_after_any_prediction_failure(
        prediction_book, tmp_path, monkeypatch, failed):
    conn, _, _ = prediction_book
    # An otherwise fresh old prediction must not make a failed new batch tradable.
    assert predict_all.run(conn, SimpleNamespace()) == 2
    model = sys.modules["_macro_maintenance_model_test"]
    original = model.predict

    def predict(db, now, period, *, series, params):
        if series in failed:
            raise RuntimeError("test model failure")
        return original(db, now, period, series=series, params=params)

    monkeypatch.setattr(model, "predict", predict)
    s = SimpleNamespace(db_path=tmp_path / "predictions.db", output_dir=tmp_path,
                        fred_api_key="fake", polygon_api_key="fake")
    monkeypatch.setattr(refresh, "load_settings", lambda: s)
    monkeypatch.setattr(refresh, "init_db", lambda _: conn)
    monkeypatch.setattr(refresh, "FredPIT", lambda *args: SimpleNamespace(pull_core=lambda: {}))
    monkeypatch.setattr(refresh, "KalshiMD", lambda *args: SimpleNamespace(
        snapshot_series=lambda _: 0, sync_settlements=lambda _: 0))
    calls = []

    def returns(name, value):
        def call(*args, **kwargs):
            calls.append(name)
            return value
        return call

    # Every external collector is a local fake. This exercises the actual daily
    # step runner and its saved status record, including work after prediction.
    modules = {
        "ingest.nowcast": {"pull_gdpnow": 0},
        "ingest.aaa_daily": {"fetch_daily": 0},
        "ingest.eia": {"pull_storage": 0},
        "ingest.treasury": {"pull": 0},
        "ingest.ercot": {"refresh": 0, "mirror_weekly_burn": 0},
        "ingest.pjm": {"refresh": 0, "mirror_weekly_burn": 0, "mirror_weekly_demand": 0},
        "ingest.weather": {"pull": {"days": 0}},
        "ingest.fed_text": {"fetch_statements": 0},
        "venues.kalshi.account": {"refresh_bankroll": 0},
        "ops.archive_candles": {"run": 0},
        "model.registry": {"ensure_registered": 0},
        "research.param_argmin": {"daily": {}},
        "model.ts_foundation": {"shadow_run": 0},
        "model.bridge": {"shadow_run": 0},
        "model.ensemble": {"shadow_run": 0},
        "strategy.consistency": {"run": 0},
        "analysis.llm": {"apply_news_flags": 0, "statement_risk_pass": None},
        "research.health": {"daily_health": 0},
        "ops.frontend_export": {"run": 0},
        "ops.report": {"daily_pdf": 0},
        "ops.backup_db": {"run": 0},
    }
    for short_name, funcs in modules.items():
        name = "prediction_market_macro." + short_name
        module = ModuleType(name)
        for function, value in funcs.items():
            setattr(module, function, returns(f"{short_name}.{function}", value))
        parent, leaf = name.rsplit(".", 1)
        monkeypatch.setitem(sys.modules, name, module)
        monkeypatch.setattr(importlib.import_module(parent), leaf, module, raising=False)
    from prediction_market_macro.ingest import cleveland_nowcast
    from prediction_market_macro.research import param_select
    monkeypatch.setattr(cleveland_nowcast, "refresh", lambda *args: 0)
    monkeypatch.setattr(param_select, "refresh", lambda *args, **kwargs: {})
    for name in ("sync_to_db", "reconcile_actuals"):
        monkeypatch.setattr(refresh.calendars, name, lambda *args: 0)
    for name in ("pull_futures", "pull_fx", "pull_news"):
        monkeypatch.setattr(refresh.market_data, name, lambda *args: 0)
    monkeypatch.setattr(refresh.scheduler, "materialize", lambda *args: 0)
    monkeypatch.setattr(refresh.scheduler, "watchdog", lambda *args: [])
    monkeypatch.setattr(decide_all, "run", returns("decide", 0))
    monkeypatch.setattr(refresh, "_run_exit_steps", returns("exit_steps", None))
    monkeypatch.setattr(trading_kalshi, "sync", lambda *args: {})
    monkeypatch.setattr(trading_kalshi, "reconcile", lambda *args: {})
    monkeypatch.setattr(pnl, "mark_all", returns("marks", 0))
    monkeypatch.setattr(pnl, "settle_pass", returns("settle", 0))

    result = refresh._run()
    steps = result["steps"]
    if failed:
        assert steps["predict_all"].startswith("FAIL")
        assert "decide" not in calls and "exit_steps" not in calls
        for name in ("decide_all", "held_exit_inputs", "s2_shadow", "exits"):
            assert steps[name] == "SKIPPED (predict_all failed)"
    else:
        assert steps["predict_all"].startswith("ok")
        assert "decide" in calls and "exit_steps" in calls
    assert "marks" in calls and "settle" in calls
    assert "ops.frontend_export.run" in calls and "ops.report.daily_pdf" in calls
    assert "ops.backup_db.run" in calls
    assert conn.execute("SELECT COUNT(*) FROM alerts WHERE source='predict_all' AND level='error'").fetchone()[0] == len(failed)
    assert (tmp_path / "refresh_last.json").exists()
