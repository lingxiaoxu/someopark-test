"""Publication must expose committed research without torn or regressed snapshots."""
import json
import multiprocessing as mp
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from prediction_market_macro.ingest.store import init_db
from prediction_market_macro.ops import frontend_export as fx
from prediction_market_macro.research import live_replay as lr


def _settings(path):
    path = Path(path)
    return SimpleNamespace(db_path=path / "test.db", output_dir=path, frontend_data=path)


def _store(conn, stamp, end):
    payload = {"window_end": end, "generated_at": stamp, "reconciliation": {"n_unexplained": 0}}
    conn.execute("INSERT OR REPLACE INTO experiments VALUES('live_replay',?,'*','live',?,?)",
                 (end, json.dumps(payload), stamp))
    conn.commit()
    return payload


def test_atomic_write_preserves_last_document_on_replace_failure(tmp_path, monkeypatch):
    path = tmp_path / "macro_test.json"
    fx._write(path, {"stable": True})
    def fail(*args):
        # Even during the final replacement the old document remains intact.
        assert json.loads(path.read_text()) == {"stable": True}
        raise OSError("disk failure")
    monkeypatch.setattr(fx.os, "replace", fail)
    with pytest.raises(OSError, match="disk failure"):
        fx._write(path, {"new": list(range(1000))})
    assert json.loads(path.read_text()) == {"stable": True}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["macro_test.json"]


def test_atomic_write_keeps_strict_json_sanitization(tmp_path):
    path = tmp_path / "macro_test.json"
    fx._write(path, {"nested": [float("nan"), float("inf"), "NaN"]})
    assert json.loads(path.read_text()) == {"nested": [None, None, "NaN"]}


@pytest.mark.parametrize("no_store", [False, True])
def test_replay_cli_publishes_only_after_store_without_unrelated_exports(tmp_path, monkeypatch, no_store):
    settings = _settings(tmp_path)
    prior = tmp_path / "macro_board.json"
    prior.write_text('{"unchanged":true}')
    monkeypatch.setattr(lr, "load_settings", lambda **kw: settings)
    monkeypatch.setattr("sys.argv", ["live_replay", "--no-lock", "--json"] + (["--no-store"] if no_store else []))
    def replay(conn, end=None, store=True):
        assert store is not no_store
        return (_store(conn, "2026-09-09T15:20:00+00:00", "2026-09-08")
                if store else {"window_end": "2026-09-08"})
    monkeypatch.setattr(lr, "run", replay)
    lr.main()
    published = tmp_path / "macro_livereplay.json"
    assert published.exists() is not no_store
    if not no_store:
        assert json.loads(published.read_text())["latest"]["window_end"] == "2026-09-08"
        with sqlite3.connect(settings.db_path) as conn:
            assert conn.execute("SELECT count(*) FROM experiments WHERE name='live_replay'").fetchone()[0] == 1
    assert prior.read_text() == '{"unchanged":true}'


def _slow_publish(path, ready, release):
    settings = _settings(path)
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    with fx._export_lock(settings):
        # Model a full exporter that has read the old state and is still publishing.
        row = conn.execute("SELECT created_ts FROM experiments ORDER BY created_ts DESC LIMIT 1").fetchone()
        ready.set()
        assert release.wait(10)
        fx._write(settings.frontend_data / "macro_livereplay.json", {"latest_ts": row[0]})
    conn.close()


def _targeted_publish(path, entered, finished):
    settings = _settings(path)
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    entered.set()
    fx.export_live_replay(conn, settings)
    finished.set()
    conn.close()


def test_shared_lock_orders_read_and_publish_across_processes(tmp_path):
    conn = init_db(tmp_path / "test.db")
    _store(conn, "2026-09-08T15:00:00+00:00", "2026-09-07")
    ctx = mp.get_context("fork")
    ready, release, entered, finished = [ctx.Event() for _ in range(4)]
    slow = ctx.Process(target=_slow_publish, args=(tmp_path, ready, release))
    fresh = ctx.Process(target=_targeted_publish, args=(tmp_path, entered, finished))
    slow.start()
    try:
        assert ready.wait(10)
        _store(conn, "2026-09-09T15:00:00+00:00", "2026-09-08")
        fresh.start()
        assert entered.wait(10)
        assert not finished.wait(0.2), "a second exporter bypassed the read/publish lock"
        release.set()
        assert finished.wait(10)
        doc = json.loads((tmp_path / "macro_livereplay.json").read_text())
        assert doc["latest_ts"] == "2026-09-09T15:00:00+00:00"
        assert [h["window_end"] for h in doc["history"]] == ["2026-09-07", "2026-09-08"]
    finally:
        release.set()
        slow.join(10)
        if fresh.pid:
            fresh.join(10)
        conn.close()
    assert slow.exitcode == fresh.exitcode == 0
