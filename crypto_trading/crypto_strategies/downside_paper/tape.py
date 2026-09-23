"""Read-only bounded incremental readers for the existing Hyperliquid recorder.

No new subscriptions, collectors, API clients, or modifications to input files.
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import gzip
import json
from pathlib import Path


class JsonlTail:
    def __init__(self, initial_bytes=2_000_000):
        self.initial_bytes = initial_bytes
        self.cursors = {}
        self.errors = 0

    def seed_end(self, path):
        path = Path(path)
        if path.exists():
            st = path.stat()
            self.cursors[str(path)] = (st.st_ino, st.st_size)

    def read(self, path):
        path = Path(path)
        if not path.exists():
            return []
        st = path.stat()
        old = self.cursors.get(str(path))
        fresh = old is None or old[0] != st.st_ino or st.st_size < old[1]
        offset = max(0, st.st_size - self.initial_bytes) if fresh else old[1]
        with path.open("rb") as stream:
            stream.seek(offset)
            if fresh and offset:
                stream.readline()  # discard possible partial first row
            payload = stream.read(8_000_000)
            last_newline = payload.rfind(b"\n")
            complete = payload[:last_newline + 1] if last_newline >= 0 else b""
            end = stream.tell() - len(payload) + len(complete)
        self.cursors[str(path)] = (st.st_ino, end)
        result = []
        for line in complete.splitlines():
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    result.append(value)
            except (json.JSONDecodeError, UnicodeDecodeError):
                self.errors += 1
        return result


class ExistingTape:
    def __init__(self, root, assets=("BTC", "ETH", "SOL", "DOGE", "XRP")):
        self.root = Path(root)
        self.assets = assets
        self.tail = JsonlTail()
        self.rows = defaultdict(list)
        self.gz_loaded = set()

    def update(self, now):
        date = datetime.fromtimestamp(now, timezone.utc)
        days = [date.strftime("%Y-%m-%d")]
        if (date - date.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds() < 1200:
            days.insert(0, (date - timedelta(days=1)).strftime("%Y-%m-%d"))
        for kind in ("context", "book", "trades"):
            for asset in self.assets:
                key = kind, asset
                for day in days:
                    path = self.root / kind / asset / (day + ".jsonl")
                    incoming = self.tail.read(path)
                    compressed = Path(str(path) + ".gz")
                    if not path.exists() and compressed.exists() and str(compressed) not in self.gz_loaded:
                        with gzip.open(compressed, "rt") as stream:
                            for line in stream:
                                try:
                                    row = json.loads(line)
                                    if now - 1200 <= float(row.get("recv_ts", 0)) <= now:
                                        incoming.append(row)
                                except (ValueError, TypeError):
                                    self.tail.errors += 1
                        self.gz_loaded.add(str(compressed))
                    self.rows[key].extend(incoming)
                # A writer can append after this cycle's cutoff. Retain that row
                # for the next cycle; feature extraction applies recv <= decision.
                self.rows[key] = [r for r in self.rows[key] if now - 1200 <= float(r.get("recv_ts", 0))]

    def for_asset(self, asset):
        return [self.rows[(kind, asset)] for kind in ("context", "book", "trades")]
