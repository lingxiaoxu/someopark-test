"""Disk-hygiene guards from the 2026-10-02 write audit: what they delete, and what they never touch."""
import json
import os
import shutil
import sqlite3
import time
from collections import namedtuple

import pytest

from prediction_market_soccer.ops import export_stage, forward_epoch_update, version_workflow


def _epoch(root, name, phase, mtime, backup=True):
    rec = root / "book_candidates" / name / "recovery"
    rec.mkdir(parents=True)
    if phase is not None:
        (rec / "method-journal.json").write_text(json.dumps({"phase": phase}))
    if backup:
        db = rec / "database-before.db"
        db.write_bytes(b"x")
        os.utime(db, (mtime, mtime))
    return rec


def test_reaper_keeps_newest_verified_and_every_unfinished_or_unmanaged(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(forward_epoch_update, "MOD", tmp_path)
    old = _epoch(tmp_path / "data", "epoch_a", "verified", 1_000)
    mid = _epoch(tmp_path / "data", "epoch_b", "verified", 2_000)
    new = _epoch(tmp_path / "data", "epoch_c", "verified", 3_000)
    unfinished = _epoch(tmp_path / "data", "epoch_d", "activating", 500)
    no_journal = _epoch(tmp_path / "data", "epoch_e", None, 400)
    stranger = tmp_path / "data" / "book_candidates" / "leakfix-x"
    stranger.mkdir()
    with open(stranger / "big.db", "wb") as f:      # sparse: logical >1G, ~no disk
        f.truncate(2**30 + 1)

    forward_epoch_update._reap_superseded_recovery_dbs()

    assert not (old / "database-before.db").exists()
    assert not (mid / "database-before.db").exists()
    assert (new / "database-before.db").exists()
    assert (unfinished / "database-before.db").exists()
    assert (no_journal / "database-before.db").exists()
    assert all((r / "method-journal.json").exists() for r in (old, mid, new, unfinished))
    assert (stranger / "big.db").exists()
    assert "盲区目录 leakfix-x" in capsys.readouterr().out


def test_reaper_is_a_noop_without_book_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(forward_epoch_update, "MOD", tmp_path)
    forward_epoch_update._reap_superseded_recovery_dbs()


def test_orphan_sweep_is_age_gated(tmp_path):
    stale = tmp_path / ".refresh-stage-old"
    fresh = tmp_path / ".refresh-stage-new"
    other = tmp_path / "output"
    for d in (stale, fresh, other):
        d.mkdir()
        (d / "f").write_text("x")
    past = time.time() - (export_stage.STAGE_ORPHAN_HOURS + 1) * 3600
    os.utime(stale, (past, past))
    os.utime(other, (past, past))

    export_stage._sweep_orphan_stages(tmp_path)

    assert not stale.exists()
    assert fresh.exists() and other.exists()


def test_stage_clone_is_an_independent_copy(tmp_path):
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "sub" / "a.json").write_text('{"v": 1}')
    dst = tmp_path / "dst"
    shutil.copytree(src, dst, copy_function=export_stage._clone_or_copy)
    assert (dst / "sub" / "a.json").read_text() == '{"v": 1}'
    (dst / "sub" / "a.json").write_text('{"v": 2}')
    assert (src / "sub" / "a.json").read_text() == '{"v": 1}'


def test_stage_clone_falls_back_to_a_real_copy(tmp_path, monkeypatch):
    import ctypes

    def unavailable(*a, **k):
        raise OSError("no clonefile")
    monkeypatch.setattr(ctypes, "CDLL", unavailable)
    (tmp_path / "a").write_text("x")
    export_stage._clone_or_copy(tmp_path / "a", tmp_path / "b")
    assert (tmp_path / "b").read_text() == "x"


def test_backup_headroom_refuses_below_twice_db_size(tmp_path, monkeypatch):
    path = tmp_path / "soccer.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    size = os.path.getsize(path)
    usage = namedtuple("usage", "total used free")

    monkeypatch.setattr(shutil, "disk_usage", lambda p: usage(0, 0, 2 * size - 1))
    with pytest.raises(RuntimeError, match="refusing whole-db backup"):
        version_workflow._require_backup_headroom(conn, tmp_path / "recovery")

    monkeypatch.setattr(shutil, "disk_usage", lambda p: usage(0, 0, 2 * size))
    version_workflow._require_backup_headroom(conn, tmp_path / "recovery")


def test_reaper_handles_noindex_and_legacy_recovery_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(forward_epoch_update, "MOD", tmp_path)

    def epoch(name, dirname, mtime):
        rec = tmp_path / "data" / "book_candidates" / name / dirname
        rec.mkdir(parents=True)
        (rec / "method-journal.json").write_text(json.dumps({"phase": "verified"}))
        db = rec / "database-before.db"
        db.write_bytes(b"x")
        os.utime(db, (mtime, mtime))
        return db
    legacy = epoch("epoch_v21", "recovery", 1_000)
    newest = epoch("epoch_v22", forward_epoch_update.RECOVERY_DIRNAME, 2_000)
    forward_epoch_update._reap_superseded_recovery_dbs()
    assert not legacy.exists() and newest.exists()


def test_activation_writes_its_backup_under_a_noindex_dir(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from prediction_market_soccer.util import forward_methods as FM
    monkeypatch.setattr(forward_epoch_update, "MOD", tmp_path)
    monkeypatch.setattr(forward_epoch_update, "_conn", lambda: None)
    current = {"epoch_id": "e1", "model_version": "m", "method_version": "v", "book_version_id": "b1", "manifest": {"compatible_epoch_ids": [], "rules": {"r": 1}}}
    monkeypatch.setattr(FM, "active_epoch", lambda conn: current)
    monkeypatch.setattr(FM, "build_manifest", lambda **kw: {"rules": {"r": 1}, "manifest_id": "x"})
    seen = {}
    monkeypatch.setattr(version_workflow, "head", lambda conn, dirs: "h")
    monkeypatch.setattr(version_workflow, "activate_forward_method",
                        lambda conn, **kw: seen.setdefault("recovery_dir", kw["recovery_dir"]))
    forward_epoch_update.activate(SimpleNamespace(method_version="v", durable="epoch_test"))
    assert seen["recovery_dir"].name.endswith(".noindex")
    assert seen["recovery_dir"].parent.name == "epoch_test"
