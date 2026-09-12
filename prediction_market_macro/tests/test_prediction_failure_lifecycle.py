"""A failed current prediction cannot masquerade as a successful scheduled decision.

The fake model is dispatched through real predict_all with temporary SQLite files.
Only source HTTP, decision execution and publication transport are substituted.
"""
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from prediction_market_macro.config.registry import REGISTRY
from prediction_market_macro.jobs import tick
from prediction_market_macro.ops import decide_all, exits, pnl, predict_all, trading_kalshi
from prediction_market_macro.tests.test_predict_maintenance_coverage import prediction_book


SERIES = {"KXCPI", "KXU3"}


def fail_models(monkeypatch, failed):
    module_name = predict_all.SERIES_DISPATCH["KXCPI"][0]
    module = sys.modules[module_name]
    original = module.predict
    attempted = []

    def predict(db, now, period, *, series, params):
        attempted.append(series)
        if series in failed:
            raise RuntimeError(f"fixture source unavailable for {series}")
        return original(db, now, period, series=series, params=params)

    monkeypatch.setattr(module, "predict", predict)
    return attempted


def seed_fresh_previous_predictions(conn):
    previous = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    for series in SERIES:
        conn.execute(
            "INSERT INTO preds(series,period,asof,model_version,dist_json,ladder_json,"
            "inputs_json,data_horizon,created_ts) VALUES(?,?,?,?,?,NULL,'{}',?,?)",
            (series, "2026-08", previous, REGISTRY[series].model + "/previous",
             '{"probs":{"yes":1.0}}', previous, previous))
    conn.commit()
    return previous


def assert_committed_prediction_result(conn, failed):
    path = next(row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main")
    with sqlite3.connect(path) as reader:
        succeeded = {row[0] for row in reader.execute(
            "SELECT series FROM preds WHERE model_version LIKE '%/test'")}
        errors = [row[0] for row in reader.execute(
            "SELECT message FROM alerts WHERE source='predict_all' AND level='error'")]
    assert succeeded == SERIES - failed
    assert len(errors) == len(failed)
    assert all(any(series + "/2026-08" in error for error in errors) for series in failed)
    assert all("fixture source unavailable" in error for error in errors)


@pytest.mark.parametrize("failed", [set(), {"KXCPI"}, SERIES])
def test_strict_prediction_collects_failures_and_commits_successes(
        prediction_book, monkeypatch, failed):
    conn, _, _ = prediction_book
    attempted = fail_models(monkeypatch, failed)
    if failed:
        with pytest.raises(predict_all.PredictionError) as captured:
            predict_all.run(conn, SimpleNamespace(), fail_on_error=True)
        error = captured.value
        assert error.succeeded == len(SERIES - failed)
        assert isinstance(error.failures, tuple) and len(error.failures) == len(failed)
        assert all(any(series + "/2026-08" in item for item in error.failures) for series in failed)
    else:
        assert predict_all.run(conn, SimpleNamespace(), fail_on_error=True) == 2
    assert set(attempted) == SERIES
    assert_committed_prediction_result(conn, failed)


@pytest.mark.parametrize("failed", [{"KXCPI"}, SERIES])
@pytest.mark.parametrize("explicit_false", [False, True])
def test_default_prediction_retains_integer_return_contract(
        prediction_book, monkeypatch, failed, explicit_false):
    conn, _, _ = prediction_book
    fail_models(monkeypatch, failed)
    kwargs = {"fail_on_error": False} if explicit_false else {}
    result = predict_all.run(conn, SimpleNamespace(), **kwargs)
    assert type(result) is int and result == len(SERIES - failed)
    assert_committed_prediction_result(conn, failed)


@pytest.mark.parametrize("failed", [{"KXCPI"}, SERIES])
def test_scheduled_task_with_fresh_old_preds_stays_late_on_current_prediction_failure(
        prediction_book, monkeypatch, failed):
    conn, _, _ = prediction_book
    previous = seed_fresh_previous_predictions(conn)
    attempted = fail_models(monkeypatch, failed)
    downstream = []
    for module, function in ((decide_all, "run"), (exits, "shadow_run"), (exits, "run")):
        monkeypatch.setattr(module, function,
                            lambda *args, _name=module.__name__+function, **kwargs:
                            downstream.append(_name))
    monkeypatch.setattr(tick, "_top_up_stale_quotes", lambda *args: {})
    monkeypatch.setattr(tick, "_export_frontend", lambda *args: None)
    monkeypatch.setattr(pnl, "mark_all", lambda *args: 0)
    monkeypatch.setattr(trading_kalshi, "sync", lambda *args: {})
    due = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    conn.execute("INSERT INTO runs(lane,series,period,task,due_ts)"
                 " VALUES('release_day','KXCPI','2026-08','decide',?)", (due,))
    conn.commit()
    md = SimpleNamespace(snapshot_series=lambda _: 0,
                         snapshot_tickers=lambda *args, **kwargs: {"refreshed": [], "failed": {}})

    tick._drain_due(conn, SimpleNamespace(), md)

    row = conn.execute("SELECT status,done_ts,note FROM runs WHERE task='decide'").fetchone()
    assert row["status"] == "late" and row["done_ts"] is None
    assert "KXCPI/2026-08" in row["note"]
    assert downstream == []
    assert set(attempted) == SERIES
    assert conn.execute("SELECT COUNT(*) FROM preds WHERE asof=?", (previous,)).fetchone()[0] == 2
    assert_committed_prediction_result(conn, failed)


@pytest.mark.parametrize("failed", [{"KXCPI"}, SERIES])
def test_failed_held_prediction_still_marks_and_exports_without_exiting(
        prediction_book, monkeypatch, failed):
    conn, _, before = prediction_book
    seed_fresh_previous_predictions(conn)
    fail_models(monkeypatch, failed)
    ts = datetime.now(timezone.utc).isoformat()
    for series in SERIES:
        decision = conn.execute(
            "INSERT INTO decisions(ts_utc,series,period,structure_json,kind,inputs_json,"
            "model_version,gate_snapshot) VALUES(?,?,'2026-08','{}','open','{}',?,'{}')",
            (ts, series, REGISTRY[series].model + "/previous"))
        conn.execute("INSERT INTO fills(decision_id,ts_utc,ticker,side,price,count,fee_usd)"
                     " VALUES(?,?,?,'yes',.5,1,.01)", (decision.lastrowid, ts, series + "-LEG"))
    conn.commit()
    downstream, events = [], []
    monkeypatch.setattr(exits, "run", lambda *args, **kwargs: downstream.append("exit"))
    monkeypatch.setattr(exits, "shadow_run", lambda *args, **kwargs: downstream.append("shadow"))
    original_mark = pnl.mark_all

    def snapshot(tickers, **kwargs):
        assert tickers == {series + "-LEG" for series in SERIES}
        now = datetime.now(timezone.utc).isoformat()
        for ticker in tickers:
            conn.execute("INSERT INTO quotes VALUES(?,?,.4,.42,100,100)", (now, ticker))
        conn.commit()
        events.append("quotes")
        return {"refreshed": list(tickers), "failed": {}}

    def mark(db):
        events.append("marks")
        return original_mark(db)

    def export(db, settings):
        rows = db.execute("SELECT mark_status,quote_ts FROM marks").fetchall()
        assert len(rows) == 2 and all(row["mark_status"] == "marked" for row in rows)
        assert all(row["quote_ts"] for row in rows)
        events.append("export")

    monkeypatch.setattr(pnl, "mark_all", mark)
    monkeypatch.setattr(tick, "_export_frontend", export)
    monkeypatch.setattr(trading_kalshi, "sync", lambda *args: events.append("sync"))
    with pytest.raises(predict_all.PredictionError):
        tick._maintain_positions(conn, SimpleNamespace(), SimpleNamespace(snapshot_tickers=snapshot))
    assert events == ["quotes", "marks", "sync", "export"]
    assert downstream == []
    assert [tuple(row) for row in conn.execute("SELECT * FROM coverage ORDER BY series")] == before
    assert_committed_prediction_result(conn, failed)


@pytest.mark.parametrize("task", ["daily_refresh", "health", "pred_freshness"])
@pytest.mark.parametrize("failed_step", ["predict_all", "decide_all", "exits"])
def test_recent_failed_refresh_is_not_reported_done_or_restarted_every_minute(
        prediction_book, tmp_path, monkeypatch, task, failed_step):
    from prediction_market_macro.ops import refresh
    conn, _, _ = prediction_book
    stamp = {"ts": datetime.now(timezone.utc).isoformat(),
             "steps": {"predict_all": "ok", "decide_all": "ok", "exits": "ok"}}
    stamp["steps"][failed_step] = "FAIL fixture critical failure"
    (tmp_path / "refresh_last.json").write_text(json.dumps(stamp))
    settings = SimpleNamespace(output_dir=tmp_path)
    runs = []
    monkeypatch.setattr(refresh, "run", lambda: runs.append("full refresh"))
    with pytest.raises(RuntimeError, match="critical refresh steps failed: " + failed_step):
        tick._exec_task(conn, settings, None,
                        {"task": task, "series": "KXCPI", "period": "2026-08"})
    due = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    conn.execute("INSERT INTO runs(lane,series,period,task,due_ts)"
                 " VALUES('daily','KXCPI','2026-08',?,?)", (task, due))
    conn.commit()
    for _ in range(2):
        tick._drain_due(conn, settings, None)
    row = conn.execute("SELECT status,done_ts,note FROM runs").fetchone()
    assert row["status"] == "late" and row["done_ts"] is None
    assert failed_step in row["note"]
    assert runs == []


@pytest.mark.parametrize("task", ["daily_refresh", "health", "pred_freshness"])
@pytest.mark.parametrize("failed_step", ["predict_all", "decide_all", "exits"])
def test_fallback_refresh_checks_critical_step_results(
        prediction_book, tmp_path, monkeypatch, task, failed_step):
    from prediction_market_macro.ops import refresh
    conn, _, _ = prediction_book
    calls = []

    def failed_refresh():
        calls.append("refresh")
        return {"ts": datetime.now(timezone.utc).isoformat(),
                "steps": {failed_step: "FAIL fixture critical failure"}}

    monkeypatch.setattr(refresh, "run", failed_refresh)
    with pytest.raises(RuntimeError, match="critical refresh steps failed: " + failed_step):
        tick._exec_task(conn, SimpleNamespace(output_dir=tmp_path), None,
                        {"task": task, "series": "KXCPI", "period": "2026-08"})
    assert calls == ["refresh"]


@pytest.mark.parametrize("task", ["daily_refresh", "health", "pred_freshness"])
@pytest.mark.parametrize("steps", [None, {"predict_all": "ok", "decide_all": "ok", "exits": "ok"},
                                   {"optional_report": "FAIL fixture noncritical failure"}])
def test_recent_compatible_refresh_stamp_still_covers_daily_tasks(
        prediction_book, tmp_path, monkeypatch, task, steps):
    from prediction_market_macro.ops import refresh
    conn, _, _ = prediction_book
    stamp = {"ts": datetime.now(timezone.utc).isoformat()}
    if steps is not None:
        stamp["steps"] = steps
    (tmp_path / "refresh_last.json").write_text(json.dumps(stamp))
    monkeypatch.setattr(refresh, "run", lambda: pytest.fail("fresh refresh should be reused"))
    assert tick._exec_task(conn, SimpleNamespace(output_dir=tmp_path), None,
                           {"task": task, "series": "KXCPI", "period": "2026-08"}) == "covered_by_daily_refresh"
