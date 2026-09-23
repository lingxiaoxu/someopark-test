"""One conservative public-event GET budget across local collector processes.

This is an application budget, not a claim about Kalshi's undocumented anonymous
quota. No credentials or order writes enter this file. The lock is never held
while sleeping or making HTTP requests. An urgent collector lease is bounded so
an exited process cannot strand ordinary readers.
"""
from __future__ import annotations
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import urlsplit

MIN_INTERVAL = 0.20                 # at most 5 public event starts/second, no burst
MAX_INTERVAL = 2.0
QUIET_RECOVERY_SECONDS = 300.0
URGENT_LEASE_SECONDS = 2.0
NORMAL_MAX_WAIT_SECONDS = 2.0


class SharedPublicReadBudget:
    def __init__(self, base_url, *, root=None, priority='normal', clock=time.time, sleeper=time.sleep):
        if priority not in ('normal', 'live'):
            raise ValueError('Unknown public read priority')
        self.origin = urlsplit(base_url).netloc
        if not self.origin:
            raise ValueError('Public read budget requires an origin')
        if root is None:
            from crypto_trading.crypto_common.config import CRYPTO_ROOT
            root = CRYPTO_ROOT / 'logs' / 'runtime' / 'kalshi_public_reads'
        self.root = Path(root)
        self.key = hashlib.sha256(self.origin.encode()).hexdigest()[:24]
        self.path = self.root / (self.key + '.json')
        self.lock = self.root / (self.key + '.lock')
        self.priority = priority
        self.clock = clock
        self.sleep = sleeper

    @contextmanager
    def _state(self):
        self.root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock, os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, 'a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            now = self.clock()
            if self.path.exists():
                data = json.loads(self.path.read_text())
                if data.get('version') != 1 or data.get('origin') != self.origin:
                    raise ValueError('Invalid shared public read state identity')
                for key in ('next_at', 'not_before', 'urgent_until', 'interval', 'last_429', 'recovered_at', 'last_seen',
                            'normal_wait_started', 'normal_wait_until'):
                    value = data.get(key)
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                        raise ValueError('Invalid shared public read clock/rate')
                if not MIN_INTERVAL <= data['interval'] <= MAX_INTERVAL:
                    raise ValueError('Invalid public read pacing interval')
                if data['last_seen'] > now + 0.5 or data['next_at'] > now + MAX_INTERVAL + 1:
                    raise ValueError('Shared read clock moved backwards; refusing to bypass budget')
            else:
                data = dict(version=1, origin=self.origin, next_at=0., not_before=0., urgent_until=0.,
                            interval=MIN_INTERVAL, last_429=0., recovered_at=now, last_seen=now,
                            normal_wait_started=0., normal_wait_until=0.)
            before = dict(data)
            yield data, now
            data["last_seen"] = now
            if data != before or not self.path.exists():
                out, name = tempfile.mkstemp(prefix=self.key + '.', dir=self.root)
                try:
                    with os.fdopen(out, 'w') as stream:
                        json.dump(data, stream, sort_keys=True, allow_nan=False)
                        stream.flush(); os.fsync(stream.fileno())
                    os.replace(name, self.path)
                finally:
                    if os.path.exists(name):
                        os.unlink(name)

    def acquire(self, *, timeout=45.0):
        started = self.clock()
        while True:
            with self._state() as (state, now):
                if now - max(state['last_429'], state['recovered_at']) >= QUIET_RECOVERY_SECONDS:
                    state['interval'] = max(MIN_INTERVAL, state['interval'] * 0.8)
                    state['recovered_at'] = now
                if self.priority != 'live':
                    if state['normal_wait_until'] <= now:
                        state['normal_wait_started'] = now
                    state['normal_wait_until'] = now + URGENT_LEASE_SECONDS
                aged_normal = (state['normal_wait_until'] > now and
                               now - state['normal_wait_started'] >= NORMAL_MAX_WAIT_SECONDS)
                if self.priority == 'live':
                    state['urgent_until'] = max(state['urgent_until'], now + URGENT_LEASE_SECONDS)
                due = max(state['next_at'], state['not_before'])
                if self.priority != 'live' and not aged_normal:
                    due = max(due, state['urgent_until'])
                elif self.priority == 'live' and aged_normal:
                    due = max(due, now + 0.05)
                wait = max(0., due - now)
                if wait == 0:
                    state['next_at'] = now + state['interval']
                    if self.priority != 'live':
                        state['normal_wait_until'] = state['normal_wait_started'] = 0.
                    return now - started
            if self.clock() + wait > started + timeout:
                raise TimeoutError('Shared public read budget timed out before dispatch')
            # Recheck after waiting: another process may have published a 429.
            self.sleep(max(0.005, min(wait, 0.5)))

    def defer(self, delay):
        if not math.isfinite(delay) or delay < 0:
            raise ValueError('Invalid shared public retry delay')
        with self._state() as (state, now):
            state['interval'] = min(MAX_INTERVAL, max(MIN_INTERVAL, state['interval'] * 1.5))
            state['last_429'] = now
            deadline = now + delay
            if not math.isfinite(deadline):
                raise ValueError('Invalid shared public retry deadline')
            state['not_before'] = max(state['not_before'], deadline)
