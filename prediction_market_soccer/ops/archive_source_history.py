"""Archive superseded PIT source revisions out of the live database.

WHY. Every data refresh captures a complete baseline (``snapshot:<table>``) plus one
row-level revision per row (``table:<table>``); ``club_prior.build_all`` does this on every
``refresh_all``, which on a matchday is every few minutes. ``nt_recent`` has 12,417 rows and
had accumulated 5.57 MILLION revisions. ``source_history.project_asof`` rebuilds the
decision-time view by reading a baseline and every revision after it — but it fetches, hash
verifies and json-parses EVERY revision of the table before discarding the ones older than
the baseline. One ``pre_decision`` measured 371 s on 2026-09-13, which pushed every paper
decision past the 120 s observation-freshness rule: the paper path recorded zero legs all day.

WHAT THIS DOES. Revisions older than their table's latest COMPLETE baseline can never be
read again for a cutoff at or after that baseline — the baseline supersedes them. They are
still evidence, so they are MOVED, never deleted: into ``data/source_history_archive.db``
(same two tables, same rows, same hashes), verified row-for-row, and only then removed from
the live database. ``--restore`` puts them back.

WHAT IT COSTS. After archiving, a ``project_asof`` cutoff EARLIER than the watermark can no
longer be served, because its overlays now live in the archive. That must never be answered
with a silently different view, so the watermark is recorded in ``source_archive_watermark_v1``
and ``source_history.project_asof`` refuses such a cutoff outright (see the pinned guard).
Historical research re-attaches the archive with ``--restore``.

    python -m prediction_market_soccer.ops.archive_source_history --check
    python -m prediction_market_soccer.ops.archive_source_history --run
    python -m prediction_market_soccer.ops.archive_source_history --restore
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

MOD = Path(__file__).resolve().parents[1]
LIVE = MOD / "data" / "soccer.db"
ARCHIVE = MOD / "data" / "source_history_archive.db"
DRIVE = Path("/Volumes/Someo Park PRO-BLADE/code/someopark-test/prediction_market_soccer/data")

WATERMARK_DDL = """CREATE TABLE IF NOT EXISTS source_archive_watermark_v1 (
    id INTEGER PRIMARY KEY CHECK(id=1), watermark TEXT NOT NULL,
    archived_rows INTEGER NOT NULL, archived_at TEXT NOT NULL, archive_path TEXT NOT NULL)"""

# Rows strictly older than their own table's latest complete baseline.
SELECT_IDS = """
WITH base AS (SELECT substr(o.source,10) tbl, MAX(a.available_at) at
              FROM source_observation_v1 o JOIN source_availability_v1 a USING(revision_id)
              WHERE o.source LIKE 'snapshot:%' AND o.complete=1 GROUP BY o.source)
SELECT o.revision_id FROM source_observation_v1 o
JOIN source_availability_v1 a USING(revision_id)
JOIN base b ON b.tbl = substr(o.source,7)
WHERE o.source LIKE 'table:%' AND a.available_at < b.at"""

WATERMARK_SQL = """
SELECT MAX(at) FROM (SELECT MAX(a.available_at) at FROM source_observation_v1 o
  JOIN source_availability_v1 a USING(revision_id)
  WHERE o.source LIKE 'snapshot:%' AND o.complete=1 GROUP BY o.source)"""


def _open(path, *, readonly=False):
    uri = f"file:{path}?mode=ro" if readonly else str(path)
    # isolation_level=None: python's sqlite3 otherwise opens an implicit transaction on the
    # first DML, and the explicit BEGIN IMMEDIATE that guards the copy/delete then fails with
    # "cannot start a transaction within a transaction". Autocommit + explicit BEGIN keeps the
    # copy and the delete each in one transaction we actually control.
    conn = sqlite3.connect(uri, uri=readonly, timeout=120, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _counts(conn):
    return (conn.execute("SELECT COUNT(*) FROM source_observation_v1").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM source_availability_v1").fetchone()[0])


def check() -> dict:
    conn = _open(LIVE, readonly=True)
    total, _ = _counts(conn)
    n = conn.execute(f"SELECT COUNT(*) FROM ({SELECT_IDS})").fetchone()[0]
    wm = conn.execute(WATERMARK_SQL).fetchone()[0]
    conn.close()
    return {"total_rows": total, "archivable": n, "remaining": total - n, "watermark": wm,
            "archive_exists": ARCHIVE.exists()}


def run(*, dry: bool) -> dict:
    live = _open(LIVE)
    wm = live.execute(WATERMARK_SQL).fetchone()[0]
    if not wm:
        return {"state": "skip", "reason": "no complete baseline"}
    before = _counts(live)
    ids = [r[0] for r in live.execute(SELECT_IDS).fetchall()]
    if not ids:
        return {"state": "ok", "archived": 0, "reason": "nothing superseded"}
    if dry:
        return {"state": "check", "would_archive": len(ids), "watermark": wm}
    live.execute("ATTACH DATABASE ? AS arc", (str(ARCHIVE),))
    live.execute("CREATE TABLE IF NOT EXISTS arc.source_observation_v1 AS SELECT * FROM main.source_observation_v1 WHERE 0")
    live.execute("CREATE TABLE IF NOT EXISTS arc.source_availability_v1 AS SELECT * FROM main.source_availability_v1 WHERE 0")
    live.execute("CREATE INDEX IF NOT EXISTS arc.arc_obs_rev ON source_observation_v1(revision_id)")
    live.execute("CREATE INDEX IF NOT EXISTS arc.arc_obs_src ON source_observation_v1(source, entity_key)")
    live.execute("CREATE TEMP TABLE move(revision_id TEXT PRIMARY KEY)")
    live.executemany("INSERT OR IGNORE INTO move VALUES (?)", ((i,) for i in ids))
    # Copy first, verify, and only then delete — the live row is never the last copy.
    live.execute("BEGIN IMMEDIATE")
    live.execute("INSERT OR IGNORE INTO arc.source_observation_v1 SELECT o.* FROM main.source_observation_v1 o JOIN move USING(revision_id)")
    live.execute("INSERT OR IGNORE INTO arc.source_availability_v1 SELECT a.* FROM main.source_availability_v1 a JOIN move USING(revision_id)")
    live.commit()
    copied = live.execute("SELECT COUNT(*) FROM arc.source_observation_v1 o JOIN move USING(revision_id)").fetchone()[0]
    if copied != len(ids):
        live.execute("DETACH DATABASE arc")
        return {"state": "abort", "reason": f"archive holds {copied} of {len(ids)}; nothing deleted"}
    sample = live.execute("""SELECT COUNT(*) FROM main.source_observation_v1 m JOIN arc.source_observation_v1 a2 USING(revision_id)
                             WHERE m.payload_hash <> a2.payload_hash OR m.payload <> a2.payload""").fetchone()[0]
    if sample:
        live.execute("DETACH DATABASE arc")
        return {"state": "abort", "reason": f"{sample} archived payloads differ; nothing deleted"}
    live.execute("BEGIN IMMEDIATE")
    live.execute("DELETE FROM main.source_availability_v1 WHERE revision_id IN (SELECT revision_id FROM move)")
    live.execute("DELETE FROM main.source_observation_v1 WHERE revision_id IN (SELECT revision_id FROM move)")
    live.execute(WATERMARK_DDL)
    live.execute("INSERT OR REPLACE INTO source_archive_watermark_v1 VALUES (1,?,?,?,?)",
                 (wm, len(ids), datetime.now(timezone.utc).isoformat(), str(ARCHIVE)))
    live.commit()
    after = _counts(live)
    live.execute("DETACH DATABASE arc")
    live.close()
    return {"state": "ok", "archived": len(ids), "watermark": wm,
            "rows_before": before, "rows_after": after, "archive": str(ARCHIVE)}


def restore() -> dict:
    if not ARCHIVE.exists():
        return {"state": "skip", "reason": "no archive"}
    live = _open(LIVE)
    live.execute("ATTACH DATABASE ? AS arc", (str(ARCHIVE),))
    live.execute("BEGIN IMMEDIATE")
    live.execute("INSERT OR IGNORE INTO main.source_observation_v1 SELECT * FROM arc.source_observation_v1")
    live.execute("INSERT OR IGNORE INTO main.source_availability_v1 SELECT * FROM arc.source_availability_v1")
    live.execute("DELETE FROM source_archive_watermark_v1")
    live.commit()
    after = _counts(live)
    live.execute("DETACH DATABASE arc"); live.close()
    return {"state": "ok", "rows_after": after}


def copy_to_drive() -> dict:
    if not DRIVE.parent.exists():
        return {"state": "skip", "reason": "drive not mounted"}
    if not ARCHIVE.exists():
        return {"state": "skip", "reason": "no archive"}
    DRIVE.mkdir(parents=True, exist_ok=True)
    dst = DRIVE / ARCHIVE.name
    shutil.copyfile(ARCHIVE, dst)
    same = hashlib.sha256(ARCHIVE.read_bytes()).hexdigest() == hashlib.sha256(dst.read_bytes()).hexdigest()
    return {"state": "ok" if same else "mismatch", "path": str(dst),
            "mb": round(dst.stat().st_size / 2**20, 1)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--restore", action="store_true")
    ap.add_argument("--to-drive", action="store_true")
    args = ap.parse_args()
    if args.restore:
        print(f"[archive_source_history] {restore()}")
    elif args.run:
        res = run(dry=False)
        print(f"[archive_source_history] {res}")
        if res.get("state") != "ok":
            sys.exit(1)
    elif args.to_drive:
        print(f"[archive_source_history] {copy_to_drive()}")
    else:
        print(f"[archive_source_history] {check()}")


if __name__ == "__main__":
    main()
