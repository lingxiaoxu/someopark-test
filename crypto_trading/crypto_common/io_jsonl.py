"""Daily-rotating jsonl writers (Plan 08 §4 storage conventions).

Raw streams append to <dir>/<YYYY-MM-DD>.jsonl (line-buffered, crash-safe-ish);
when the UTC day rolls over, the finished file is gzipped in place. Raw files
are immutable once gzipped — cleaning happens at load time.
"""
from __future__ import annotations

import gzip
import json
import os
import shutil
import tempfile
from collections import OrderedDict
from pathlib import Path

from crypto_trading.crypto_common.timeutils import utc_day


def gzip_in_place(path: Path) -> None:
    if not path.exists():
        return
    archive = Path(str(path) + ".gz")
    # Publish only a complete archive, and never overwrite an older archive
    # if a leftover raw file reappears after a crash or a separate recorder.
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.",
                                     suffix=".tmp", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        with open(path, "rb") as src, gzip.open(tmp_path, "wb") as dst:
            shutil.copyfileobj(src, dst)
        os.link(tmp_path, archive)  # exclusive publication: EEXIST preserves both originals
        path.unlink()
    finally:
        tmp_path.unlink(missing_ok=True)


class DailyJsonlWriter:
    """Keyed set of daily-rotating jsonl appenders under one root directory."""

    def __init__(self, root: Path, *, max_open_files: int = 64):
        if isinstance(max_open_files, bool) or not isinstance(max_open_files, int) or max_open_files < 1:
            raise ValueError("max_open_files must be a positive integer")
        self.root = Path(root)
        self.max_open_files = max_open_files
        self._files: OrderedDict[Path, tuple[str, object]] = OrderedDict()
        # Keep rotation metadata after eviction/reconnect without keeping the
        # descriptor. Reopening an evicted symbol on the same day appends.
        self._days: dict[Path, str] = {}

    @property
    def open_file_count(self) -> int:
        return len(self._files)

    def write(self, subdir: str | Path, obj: dict) -> None:
        payload = json.dumps(obj, separators=(",", ":")) + "\n"
        d = self.root / subdir
        day = utc_day()
        cur = self._files.get(d)
        previous_day = self._days.get(d)
        if previous_day is not None and previous_day != day:
            if cur is not None:
                self._files.pop(d)
                cur[1].close()
                cur = None
            gzip_in_place(d / f"{previous_day}.jsonl")
        if cur is None or cur[0] != day:
            if len(self._files) >= self.max_open_files:
                _, (_, oldest) = self._files.popitem(last=False)
                oldest.close()
            d.mkdir(parents=True, exist_ok=True)
            fh = open(d / f"{day}.jsonl", "a", buffering=1)
            self._files[d] = (day, fh)
            self._days[d] = day
        else:
            fh = cur[1]
            self._files.move_to_end(d)
        try:
            fh.write(payload)
        except BaseException:
            self._files.pop(d, None)
            fh.close()
            raise

    def close(self) -> None:
        files = list(self._files.values())
        self._files.clear()
        first_error = None
        for _, fh in files:
            try:
                fh.close()
            except Exception as exc:
                first_error = first_error or exc
        if first_error is not None:
            raise first_error
