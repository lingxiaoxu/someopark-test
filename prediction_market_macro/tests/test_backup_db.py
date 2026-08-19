"""The backup exists to protect data Kalshi has already deleted, so the property that
matters is not "a file appeared" — it is "the old ones are still there years later".

A plain rolling window passes the first test and fails the second, which is why prune()
is pinned here rather than eyeballed.
"""
from __future__ import annotations

import gzip
import sqlite3
from datetime import datetime, timedelta, timezone

from prediction_market_macro.ops import backup_db


def _mk(tmp_path, days):
    for d in days:
        (tmp_path / f"macro_{d}.db.gz").write_bytes(b"x")
    return tmp_path


def _names(p):
    return sorted(x.name for x in p.glob("macro_*.db.gz"))


def test_recent_snapshots_are_all_kept(tmp_path):
    now = datetime.now(timezone.utc)
    days = [(now - timedelta(days=i)).strftime("%Y%m%d") for i in range(10)]
    _mk(tmp_path, days)
    backup_db.prune(tmp_path, keep_daily=14)
    assert len(_names(tmp_path)) == 10


def test_first_of_each_month_survives_forever(tmp_path):
    """The irreplaceable part is the OLD end. A rolling window deletes exactly that."""
    old = ["20240103", "20240117", "20240129", "20240205", "20240221", "20250612"]
    _mk(tmp_path, old)
    backup_db.prune(tmp_path, keep_daily=14)
    assert _names(tmp_path) == ["macro_20240103.db.gz", "macro_20240205.db.gz",
                                "macro_20250612.db.gz"], "monthly anchors were pruned"


def test_month_anchor_is_the_earliest_not_the_alphabetically_first(tmp_path):
    _mk(tmp_path, ["20240109", "20240102", "20240128"])
    backup_db.prune(tmp_path, keep_daily=14)
    assert _names(tmp_path) == ["macro_20240102.db.gz"]


def test_prune_on_an_empty_dir_is_not_an_error(tmp_path):
    assert backup_db.prune(tmp_path) == []


def test_snapshot_is_a_readable_database_and_run_is_idempotent(tmp_path, monkeypatch):
    src = tmp_path / "macro.db"
    c = sqlite3.connect(src)
    c.execute("CREATE TABLE candles(ticker TEXT, end_ts INT)")
    c.execute("INSERT INTO candles VALUES('KXWTIW-X', 1750000000)")
    c.commit()
    c.close()

    dest = tmp_path / "out"
    monkeypatch.setenv("MACRO_BACKUP_DIR", str(dest))
    now = datetime(2026, 8, 19, tzinfo=timezone.utc)
    backup_db.run(src, now=now)
    snap = dest / "macro_20260819.db.gz"
    assert snap.exists()

    first = snap.stat().st_mtime_ns
    backup_db.run(src, now=now)                    # same day ⇒ must not rewrite
    assert snap.stat().st_mtime_ns == first

    plain = tmp_path / "restored.db"
    plain.write_bytes(gzip.decompress(snap.read_bytes()))
    r = sqlite3.connect(plain).execute("SELECT ticker FROM candles").fetchone()
    assert r[0] == "KXWTIW-X", "snapshot did not round-trip"
