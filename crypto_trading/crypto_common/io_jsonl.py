"""Daily-rotating jsonl writers (Plan 08 §4 storage conventions).

Raw streams append to <dir>/<YYYY-MM-DD>.jsonl (line-buffered, crash-safe-ish);
when the UTC day rolls over, the finished file is gzipped in place. Raw files
are immutable once gzipped — cleaning happens at load time.
"""
from __future__ import annotations

import gzip
import json
import shutil
from pathlib import Path

from crypto_trading.crypto_common.timeutils import utc_day


def gzip_in_place(path: Path) -> None:
    if not path.exists():
        return
    with open(path, "rb") as src, gzip.open(str(path) + ".gz", "wb") as dst:
        shutil.copyfileobj(src, dst)
    path.unlink()


class DailyJsonlWriter:
    """Keyed set of daily-rotating jsonl appenders under one root directory."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._files: dict[Path, tuple[str, object]] = {}   # dir -> (day, fh)

    def write(self, subdir: str | Path, obj: dict) -> None:
        d = self.root / subdir
        day = utc_day()
        cur = self._files.get(d)
        if cur is None or cur[0] != day:
            if cur is not None:
                cur[1].close()
                gzip_in_place(d / f"{cur[0]}.jsonl")
            d.mkdir(parents=True, exist_ok=True)
            fh = open(d / f"{day}.jsonl", "a", buffering=1)
            self._files[d] = (day, fh)
        else:
            fh = cur[1]
        fh.write(json.dumps(obj, separators=(",", ":")) + "\n")

    def close(self) -> None:
        for _, fh in self._files.values():
            fh.close()
        self._files.clear()
