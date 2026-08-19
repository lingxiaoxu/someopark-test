"""ops/backup_db.py — off-repo snapshots of macro.db, because the order book in it
cannot be re-fetched.

WHY THIS EXISTS. Kalshi serves candlesticks for roughly the last 73-75 days and then
drops them permanently (measured across four series; see ops/archive_candles.py). Every
row in `candles` older than that window exists in exactly one place on earth: this file.
`data/macro.db` is gitignored, so it has no remote copy either — a disk failure or a bad
`rm` would delete book history that no amount of re-running can rebuild. Same lesson, and
the same remedy, as the crypto tape backup: copy it somewhere outside the repo, on a
schedule, and keep the old ones.

WHAT IT DOES NOT DO. This is a local second copy, not off-site. It survives an
accidental `git clean`, a repo reset, or a bad migration. It does not survive the disk.
Point `MACRO_BACKUP_DIR` at an external volume or a synced folder if you want that.

Snapshots use sqlite3's online backup API rather than a file copy: macro.db runs in WAL
mode and is written by the daily refresh, so `cp` can capture a torn page or silently
miss committed transactions parked in the -wal file. The backup API takes a consistent
point-in-time image of a live database.

RETENTION — deliberately asymmetric, because the thing being protected is old history:
  * every snapshot from the last KEEP_DAILY days
  * the FIRST snapshot of every calendar month, forever
A pure rolling window would, given enough time, throw away exactly the irreplaceable part.
"""
from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

KEEP_DAILY = 14


def backup_dir() -> Path:
    return Path(os.environ.get("MACRO_BACKUP_DIR",
                               Path.home() / "macro_db_backup")).expanduser()


def _snapshot(src: Path, dst: Path) -> None:
    """Consistent online copy of a live WAL database, then gzip it."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False,
                                     dir=str(dst.parent)) as t:
        tmp = Path(t.name)
    try:
        s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        d = sqlite3.connect(str(tmp))
        try:
            s.backup(d)                      # atomic w.r.t. concurrent writers
        finally:
            d.close()
            s.close()
        with open(tmp, "rb") as f, gzip.open(dst, "wb", compresslevel=6) as g:
            shutil.copyfileobj(f, g, length=1 << 22)
    finally:
        tmp.unlink(missing_ok=True)


def prune(dirpath: Path, keep_daily: int = KEEP_DAILY) -> list[str]:
    """Delete snapshots that are neither recent nor the first of their month."""
    snaps = sorted(dirpath.glob("macro_????????.db.gz"))
    if not snaps:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_daily)).strftime("%Y%m%d")
    first_of_month: dict[str, str] = {}
    for p in snaps:                                   # sorted ⇒ first seen is earliest
        first_of_month.setdefault(p.name[6:12], p.name)
    removed = []
    for p in snaps:
        day = p.name[6:14]
        if day >= cutoff or first_of_month[p.name[6:12]] == p.name:
            continue
        p.unlink()
        removed.append(p.name)
    return removed


def run(db_path, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    src, out = Path(db_path), backup_dir()
    out.mkdir(parents=True, exist_ok=True)
    dst = out / f"macro_{now:%Y%m%d}.db.gz"
    if not dst.exists():                              # idempotent within a day
        _snapshot(src, dst)
    removed = prune(out)
    kept = sorted(out.glob("macro_????????.db.gz"))
    return (f"{dst.name} {dst.stat().st_size / 1e6:.0f}MB"
            f" (src {src.stat().st_size / 1e6:.0f}MB); kept {len(kept)}"
            f", pruned {len(removed)}; oldest {kept[0].name if kept else '-'}")


def main() -> None:
    from prediction_market_macro.config.settings import load_settings
    print(run(load_settings(require_keys=False).db_path))


if __name__ == "__main__":
    main()
