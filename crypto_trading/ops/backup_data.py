"""Back up the IRREPLACEABLE self-recorded market data (user directive 2026-07-26:
never lose recorded data). See memory project-crypto-data-protection.

The recorded tape (poll books/trades/OI, WS, event strips, live index, offshore
liquidations) is self-recorded microstructure that CANNOT be re-fetched — and it
lives under a gitignored dir (disk-only, no git backup). This tars it, timestamped,
to a location OUTSIDE the repo so it survives a repo wipe. Candles/composite/
offshore-klines are NOT backed up here (re-fetchable from APIs).

    conda run -n someopark_run python -m crypto_trading.ops.backup_data \
        [--dest ~/crypto_data_backup] [--keep 5] [--dry-run]

Idempotent-ish: each run makes a new timestamped archive and prunes to --keep.
Run manually before risky ops, or from cron/launchd daily.
"""
from __future__ import annotations

import argparse
import subprocess
import time
from pathlib import Path

from crypto_trading.crypto_common.config import PRICE_DATA

# IRREPLACEABLE self-recorded trees (relative to PRICE_DATA) — see protection memo
IRREPLACEABLE = [
    "kalshi/perps/poll",
    "kalshi/perps/ws",
    "kalshi/event_strips",
    "index_proxy/live",
    "offshore/okx/liquidations",
]


def _dir_bytes(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.exists() else 0


def backup(dest: Path, keep: int, dry_run: bool = False) -> Path | None:
    dest = dest.expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    present = [d for d in IRREPLACEABLE if (PRICE_DATA / d).exists()]
    if not present:
        print("nothing to back up (no recorded data present)")
        return None
    total = sum(_dir_bytes(PRICE_DATA / d) for d in present)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    archive = dest / f"crypto_recorded_{stamp}.tar.gz"

    print(f"backing up {len(present)} irreplaceable trees ({total/1e9:.2f} GB) → {archive}")
    for d in present:
        print(f"  + {d}  ({_dir_bytes(PRICE_DATA / d)/1e6:.0f} MB)")
    if dry_run:
        print("(dry-run — no archive written)")
        return None

    # tar from PRICE_DATA so paths inside the archive are relative + restorable in place
    cmd = ["tar", "-czf", str(archive), "-C", str(PRICE_DATA), *present]
    subprocess.run(cmd, check=True)
    size = archive.stat().st_size
    print(f"done: {archive} ({size/1e9:.2f} GB compressed)")

    # prune old archives to --keep most recent
    archives = sorted(dest.glob("crypto_recorded_*.tar.gz"))
    for old in archives[:-keep] if keep > 0 else []:
        old.unlink()
        print(f"pruned old backup {old.name}")
    return archive


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dest", default="~/crypto_data_backup",
                    help="backup dir OUTSIDE the repo (default ~/crypto_data_backup)")
    ap.add_argument("--keep", type=int, default=5, help="keep this many recent archives")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    backup(Path(args.dest), args.keep, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
