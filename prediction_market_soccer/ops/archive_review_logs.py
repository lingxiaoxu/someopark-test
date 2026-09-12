"""Archive closed-day in-play review logs to the external drive, gzipped.

The per-tick review log is the research record for in-play signals (three
analysis/ scripts read it), so nothing is ever deleted without a verified
copy on the drive. Volume exploded on 2026-09-11: the quote receipts embed
each venue's full event payload (identity_evidence), so a 465-line file is
246 MB where a normal day is 1 MB. gzip returns ~18x.

Policy
  * Files for the last --keep-days days stay local and uncompressed, so the
    analysis scripts keep working on the recent window with no code change.
  * Older files are gzipped, copied to the drive under the SAME repo-mirrored
    path the other backup scripts use, byte-verified there, and only then
    removed locally.
  * The drive's pre-existing uncompressed copies are replaced by the .gz.
  * Drive absent → the run is a no-op (fail open). Nothing is ever removed
    locally unless a verified drive copy exists.

Usage
    python -m prediction_market_soccer.ops.archive_review_logs --check
    python -m prediction_market_soccer.ops.archive_review_logs
    python -m prediction_market_soccer.ops.archive_review_logs --restore 20260911
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

MOD = Path(__file__).resolve().parents[1]
LOGS = MOD / "data" / "logs"
DRIVE = Path("/Volumes/Someo Park PRO-BLADE/code/someopark-test/prediction_market_soccer/data/logs")
PATTERNS = ("inplay_review_", "inplay_review_advance_")
STAMP = MOD / "data" / "runtime" / "archive_review_logs.stamp"


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _day_of(path: Path) -> str | None:
    stem = path.name[:-len(".jsonl")] if path.name.endswith(".jsonl") else path.stem
    tail = stem.rsplit("_", 1)[-1]
    return tail if len(tail) == 8 and tail.isdigit() else None


def _targets(keep_days: int) -> list[Path]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).strftime("%Y%m%d")
    out = []
    for path in sorted(LOGS.glob("inplay_review*.jsonl")):
        day = _day_of(path)
        if day and day < cutoff:
            out.append(path)
    return out


def archive(keep_days: int, *, check: bool) -> dict:
    if not DRIVE.parent.parent.parent.exists():
        return {"state": "skip", "reason": "drive not mounted"}
    targets = _targets(keep_days)
    if not targets:
        return {"state": "ok", "archived": 0, "reason": "nothing older than keep-days"}
    if check:
        return {"state": "check", "would_archive": [p.name for p in targets],
                "bytes": sum(p.stat().st_size for p in targets)}
    DRIVE.mkdir(parents=True, exist_ok=True)
    done, freed, failed = [], 0, []
    for src in targets:
        try:
            size = src.stat().st_size
            src_sha = _sha(src)
            tmp = src.with_suffix(".jsonl.gz.tmp")
            with src.open("rb") as fin, gzip.open(tmp, "wb", compresslevel=6) as fout:
                shutil.copyfileobj(fin, fout, 1 << 20)
            # verify the gzip round-trips to the exact original bytes
            h = hashlib.sha256()
            with gzip.open(tmp, "rb") as fin:
                for chunk in iter(lambda: fin.read(1 << 20), b""):
                    h.update(chunk)
            if h.hexdigest() != src_sha:
                tmp.unlink(missing_ok=True)
                failed.append(f"{src.name}: gzip roundtrip mismatch")
                continue
            dst = DRIVE / (src.name + ".gz")
            shutil.copyfile(tmp, dst)
            if _sha(dst) != _sha(tmp):
                dst.unlink(missing_ok=True)
                tmp.unlink(missing_ok=True)
                failed.append(f"{src.name}: drive copy mismatch")
                continue
            tmp.unlink(missing_ok=True)
            stale = DRIVE / src.name          # the uncompressed mirror copy, now superseded
            if stale.exists():
                stale.unlink()
            src.unlink()                       # local removal ONLY after a verified drive copy
            done.append(src.name)
            freed += size
        except Exception as exc:  # noqa: BLE001 — one bad file must not abort the run
            failed.append(f"{src.name}: {type(exc).__name__}: {str(exc)[:80]}")
    STAMP.parent.mkdir(parents=True, exist_ok=True)
    STAMP.write_text(datetime.now(timezone.utc).isoformat())
    return {"state": "ok" if not failed else "partial", "archived": len(done),
            "freed_mb": round(freed / 2**20, 1), "files": done, "failed": failed}


def restore(day: str) -> dict:
    out = []
    for name in (f"inplay_review_{day}.jsonl", f"inplay_review_advance_{day}.jsonl"):
        gz = DRIVE / (name + ".gz")
        if not gz.exists():
            continue
        dst = LOGS / name
        with gzip.open(gz, "rb") as fin, dst.open("wb") as fout:
            shutil.copyfileobj(fin, fout, 1 << 20)
        out.append(f"{name} ({dst.stat().st_size / 2**20:.1f} MB)")
    return {"restored": out or "nothing on the drive for that day"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-days", type=int, default=2,
                    help="days kept local and uncompressed for the analysis scripts")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--restore", metavar="YYYYMMDD")
    ap.add_argument("--once-per-day", action="store_true",
                    help="no-op when the stamp is younger than 20h (for pipeline calls)")
    args = ap.parse_args()
    if args.restore:
        print(f"[archive_review_logs] {restore(args.restore)}")
        return
    if args.once_per_day and STAMP.exists() and time.time() - STAMP.stat().st_mtime < 20 * 3600:
        print("[archive_review_logs] skip (ran within 20h)")
        return
    res = archive(args.keep_days, check=args.check)
    print(f"[archive_review_logs] {res}")
    if res.get("failed"):
        sys.exit(1)


if __name__ == "__main__":
    main()
