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
