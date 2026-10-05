"""Throttled durable checkpoints for the long-running paper observers.

2026-10-02 incident: W10, W9, the W9/W10 control, the W8 observer and the W8
demo mirror each rewrote (and fsynced) their WHOLE multi-MB state file every
1-5 s, mostly to persist heartbeat fields. Together that was ~1 TB/day of SSD
writes; twice in one day the disk filled and the machine hung.

A Checkpoint writes the state only when its *material* content changed, or at
least every `interval` seconds. What is material is decided by excluding a
fixed list of volatile paths (heartbeats, cycle counters, last-seen marks).
The bytes that get written, and the writer that writes them, are unchanged:
the only thing that moves is how often the file is refreshed. On a crash the
state rolls back by at most `interval` seconds of volatile fields only - every
decision, order, fill or settlement still reaches disk in the cycle it happens.

Volatile paths are '/'-separated keys; '*' matches any key (or any list
element) at that level, e.g. 'books/*/positions/*/mid_history'.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Callable, Iterable


def _compile(paths: Iterable[str]) -> dict:
    """Path list -> nested dict; a None leaf means 'drop this key'."""
    tree: dict = {}
    for p in paths:
        parts = [x for x in p.split("/") if x]
        node = tree
        for part in parts[:-1]:
            nxt = node.setdefault(part, {})
            if nxt is None:                  # a shorter path already drops this subtree
                break
            node = nxt
        else:
            node[parts[-1]] = None
    return tree


def _strip(value, tree):
    """Copy only along volatile paths; untouched subtrees are shared, not copied.
    A '*' level also walks list elements (e.g. 'markets/*/orders/*/checked_ts')."""
    if not tree:
        return value
    if isinstance(value, list):
        rule = tree.get("*")
        return [_strip(v, rule) for v in value] if isinstance(rule, dict) else value
    if not isinstance(value, dict):
        return value
    out = {}
    for key, sub in value.items():
        rule = tree[key] if key in tree else tree.get("*", ...)   # explicit key wins over '*'
        if rule is None:
            continue                         # volatile leaf
        out[key] = sub if rule is ... else _strip(sub, rule)
    return out


class Checkpoint:
    def __init__(self, write: Callable, *, volatile: Iterable[str] = (), interval: float = 60.0,
                 clock: Callable[[], float] = time.monotonic):
        self._write = write
        self._tree = _compile(volatile)
        self.interval = float(interval)
        self._clock = clock
        self._last_digest: str | None = None
        self._last_at: float | None = None
        self.writes = 0
        self.skips = 0

    def material_digest(self, state) -> str:
        payload = json.dumps(_strip(state, self._tree), sort_keys=True, default=str,
                             separators=(",", ":"), allow_nan=True)
        return hashlib.blake2b(payload.encode(), digest_size=16).hexdigest()

    def save(self, path, state, *, force: bool = False) -> bool:
        """Write `state` through the original writer if needed; return True if written."""
        digest = self.material_digest(state)
        now = self._clock()
        due = (force or self._last_at is None or digest != self._last_digest
               or now - self._last_at >= self.interval)
        if not due:
            self.skips += 1
            return False
        self._write(path, state)
        self._last_digest, self._last_at = digest, now
        self.writes += 1
        return True
