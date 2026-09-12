"""Held-book prediction refreshes must not rewrite the release lifecycle."""
from datetime import datetime, timedelta, timezone
import sys
from types import ModuleType, SimpleNamespace

import pytest

from prediction_market_macro.config.registry import REGISTRY
from prediction_market_macro.ingest import cleveland_nowcast
from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.model.common import Categorical, Pred
from prediction_market_macro.ops import predict_all
from prediction_market_macro.research import param_select


@pytest.fixture()
def prediction_book(tmp_path, monkeypatch):
    conn = init_db(tmp_path / "predictions.db")
    calls = []
    specs = {name: REGISTRY[name] for name in ("KXCPI", "KXU3")}
    previous = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    for series in specs:
        conn.execute(
            "INSERT INTO contracts(ticker,series,event_ticker,period,status,first_seen_ts)"
            " VALUES(?,?,?,?,?,?)", (series + "-LEG", series, series + "-EVENT", "26AUG",
                                      "active", previous))
        conn.execute("INSERT INTO coverage(series,period,state,updated_ts) VALUES(?,?,?,?)",
                     (series, "2026-08", "frozen", previous))
    conn.commit()
    before = [tuple(row) for row in conn.execute("SELECT * FROM coverage ORDER BY series")]
    module = ModuleType("_macro_maintenance_model_test")

    def predict(db, now, period, *, series, params):
        calls.append(series)
        return Pred(series, period, Categorical({"yes": 1.0}), now,
                    specs[series].model + "/test", {}, now)

    module.predict = predict
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(predict_all, "REGISTRY", specs)
    monkeypatch.setattr(predict_all, "SERIES_DISPATCH",
                        {series: (module.__name__, "predict") for series in specs})
    monkeypatch.setattr(cleveland_nowcast, "refresh_if_stale", lambda *args: None)
    monkeypatch.setattr(param_select, "current", lambda *args: {})
    return conn, calls, before


def test_held_series_only_refresh_preserves_frozen_lifecycle(prediction_book):
    conn, calls, before = prediction_book
    assert predict_all.run(conn, SimpleNamespace(), only_series={"KXCPI"},
                           update_coverage=False) == 1
    assert calls == ["KXCPI"]
    assert [tuple(row) for row in conn.execute("SELECT * FROM coverage ORDER BY series")] == before
    rows = conn.execute("SELECT series,period,model_version FROM preds").fetchall()
    assert len(rows) == 1
    assert tuple(rows[0]) == ("KXCPI", "2026-08", "cpi/test")


def test_position_maintenance_uses_the_non_lifecycle_prediction_path(prediction_book, monkeypatch):
    from prediction_market_macro.jobs import tick
    from prediction_market_macro.ops import exits, ledger, pnl, trading_kalshi
    conn, calls, before = prediction_book
    monkeypatch.setattr(ledger, "open_positions", lambda db:
                        [{"series": "KXCPI", "fills": [{"ticker": "KXCPI-LEG"}]}])
    monkeypatch.setattr(tick, "_drain_freezes", lambda db: 0)
    monkeypatch.setattr(tick, "_export_frontend", lambda *args: None)
    monkeypatch.setattr(exits, "shadow_run", lambda *args, **kwargs: 0)
    monkeypatch.setattr(exits, "run", lambda *args, **kwargs: 0)
    monkeypatch.setattr(pnl, "mark_all", lambda *args: 0)
    monkeypatch.setattr(trading_kalshi, "sync", lambda *args: {})

    def snapshot(tickers, **kwargs):
        assert tickers == {"KXCPI-LEG"}
        return {"refreshed": ["KXCPI-LEG"], "failed": {}}

    tick._maintain_positions(conn, SimpleNamespace(), SimpleNamespace(snapshot_tickers=snapshot))
    assert calls == ["KXCPI"]
    assert [tuple(row) for row in conn.execute("SELECT * FROM coverage ORDER BY series")] == before
    assert conn.execute("SELECT COUNT(*) FROM preds").fetchone()[0] == 1


def test_default_prediction_path_keeps_existing_lifecycle_updates(prediction_book):
    conn, calls, _ = prediction_book
    conn.execute("UPDATE coverage SET state='scheduled'")
    conn.commit()
    assert predict_all.run(conn, SimpleNamespace()) == 2
    assert set(calls) == {"KXCPI", "KXU3"}
    assert {r[0] for r in conn.execute("SELECT state FROM coverage")} == {"predicted"}


def test_default_prediction_preserves_completed_freeze(prediction_book):
    conn, calls, before = prediction_book
    assert predict_all.run(conn, SimpleNamespace()) == 2
    assert set(calls) == {"KXCPI", "KXU3"}
    assert [tuple(row) for row in conn.execute("SELECT * FROM coverage ORDER BY series")] == before


def test_event_prediction_does_not_undo_done_freeze_when_deciding_fails(
        prediction_book, monkeypatch):
    from prediction_market_macro.jobs import tick
    from prediction_market_macro.ops import decide_all
    conn, calls, _ = prediction_book
    due = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    conn.execute("UPDATE coverage SET state='scheduled'")
    for series in ("KXCPI", "KXU3"):
        conn.execute(
            "INSERT INTO runs(lane,series,period,task,due_ts) VALUES(?,?,?,'freeze',?)",
            ("release_day", series, "2026-08", due))
    conn.execute(
        "INSERT INTO runs(lane,series,period,task,due_ts)"
        " VALUES('release_day','KXU3','2026-08','decide',?)", (due,))
    conn.commit()
    assert tick._drain_freezes(conn) == 2
    before = [tuple(row) for row in conn.execute("SELECT * FROM coverage ORDER BY series")]
    monkeypatch.setattr(tick, "_top_up_stale_quotes", lambda *args: {})

    def fail_decision(*args):
        raise RuntimeError("decision interrupted before frozen books")

    monkeypatch.setattr(decide_all, "run", fail_decision)
    tick._drain_due(conn, SimpleNamespace(), SimpleNamespace(snapshot_series=lambda _: 0))
    assert set(calls) == {"KXCPI", "KXU3"}
    assert conn.execute("SELECT COUNT(*) FROM preds").fetchone()[0] == 2
    assert {r[0] for r in conn.execute("SELECT status FROM runs WHERE task='freeze'")} == {"done"}
    assert conn.execute("SELECT status FROM runs WHERE task='decide'").fetchone()[0] == "late"
    assert [tuple(row) for row in conn.execute("SELECT * FROM coverage ORDER BY series")] == before


def test_empty_series_filter_writes_no_predictions_or_coverage(prediction_book):
    conn, calls, before = prediction_book
    assert predict_all.run(conn, SimpleNamespace(), only_series=set(), update_coverage=False) == 0
    assert calls == []
    assert conn.execute("SELECT COUNT(*) FROM preds").fetchone()[0] == 0
    assert [tuple(row) for row in conn.execute("SELECT * FROM coverage ORDER BY series")] == before
