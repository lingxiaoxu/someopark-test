"""Kalshi token-bucket rate limiter (plan 01 §7, 12 §7).

Kalshi meters two INDEPENDENT budgets — Read and Write — each a token bucket
that refills continuously at the tier's per-second budget. Most requests cost
10 tokens; cancels cost 2. Above the Basic tier the Write bucket holds two
seconds of budget (burst = 2× the per-second rate); Read buckets and Basic-tier
Write buckets hold one second. 429s carry no Retry-After, so callers apply
exponential backoff (the bucket refills in milliseconds).

This is the client-side enforcer so we never *intend* to exceed the budget; the
server's 429 is the backstop.
"""
from __future__ import annotations

import threading
import time
import math
import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from dataclasses import dataclass

# Per-second token budgets by tier (plan 12 §7).
TIER_BUDGETS: dict[str, tuple[int, int]] = {  # tier -> (read/s, write/s)
    "basic": (200, 100),
    "advanced": (300, 300),
    "premier": (1000, 1000),
    "paragon": (2000, 2000),
    "prime": (4000, 4000),
}
DEFAULT_COST = 10
CANCEL_COST = 2


class TokenBucket:
    """Continuously-refilling token bucket. Thread-safe."""

    def __init__(self, rate: float, capacity: float, *, clock=time.monotonic):
        self.rate = float(rate)
        self.capacity = float(capacity)
        self._tokens = float(capacity)
        self._clock = clock
        self._ts = clock()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._ts) * self.rate)
        self._ts = now

    def try_take(self, n: int = DEFAULT_COST) -> bool:
        """Take n tokens if available (non-blocking). False ⇒ caller must back off."""
        with self._lock:
            self._refill()
            if self._tokens >= n:
                self._tokens -= n
                return True
            return False

    def take(self, n: int = DEFAULT_COST, *, timeout: float = 30.0, poll: float = 0.01) -> bool:
        """Block until n tokens are available or timeout. True if taken."""
        deadline = self._clock() + timeout
        while True:
            if self.try_take(n):
                return True
            with self._lock:
                self._refill()
                deficit = n - self._tokens
                wait = max(poll, deficit / self.rate) if self.rate > 0 else poll
            if self._clock() + wait > deadline:
                return False
            time.sleep(wait)

    def drain(self) -> None:
        """Empty the bucket. A 429 means the budget is spent for every caller."""
        with self._lock:
            self._refill()
            self._tokens = 0.0

    @property
    def available(self) -> float:
        with self._lock:
            self._refill()
            return self._tokens


@dataclass
class KalshiRateLimiter:
    """Read + Write buckets for a Kalshi tier (plan 12 §7)."""

    tier: str = "basic"

    def __post_init__(self):
        read_rate, write_rate = TIER_BUDGETS.get(self.tier.lower(), TIER_BUDGETS["basic"])
        # Read + Basic-tier Write hold 1s; Write above Basic holds 2s of budget.
        write_cap = write_rate * (2 if self.tier.lower() != "basic" else 1)
        self.read = TokenBucket(read_rate, read_rate)
        self.write = TokenBucket(write_rate, write_cap)

    def acquire_read(self, cost: int = DEFAULT_COST, **kw) -> bool:
        return self.read.take(cost, **kw)

    def acquire_write(self, cost: int = DEFAULT_COST, **kw) -> bool:
        return self.write.take(cost, **kw)

    def acquire_cancel(self, **kw) -> bool:
        return self.write.take(CANCEL_COST, **kw)


def backoff_delays(base: float = 0.05, factor: float = 2.0, n: int = 6) -> list[float]:
    """Exponential backoff schedule for 429s (no Retry-After header, plan 12 §7)."""
    return [base * (factor ** i) for i in range(n)]


# ── Polymarket US ────────────────────────────────────────────────────────────
# Polymarket US meters ONE read budget per account and, unlike Kalshi, does send
# Retry-After on a 429 (measured 2026-09-14: 1–10 s). The server therefore tells
# us exactly how long to wait; client-side pacing exists so we stop spending the
# budget on 429s instead of on data. The 2026-09-14 18:17 upcoming sweep spent
# 198 of its 255 book reads on RateLimitError and priced 19 of 85 fixtures.
#
# Measured against a live key while production was also drawing on it: roughly
# 0.4 reads/s sustained, ~3 reads of burst. Those are the opening guess only —
# the budget is shared with whatever else holds the key, so the limiter adapts
# (multiplicative decrease on a 429, slow additive recovery) rather than trusting
# a constant.
#
# Soccer's live and refresh processes share the state below. A bulk calendar
# caller leaves RESERVE tokens for live reads. This does not coordinate other
# applications or hosts using the account, nor preempt an in-flight request.
POLYMARKET_US_READ_RATE = 0.4     # tokens/s sustained
POLYMARKET_US_READ_BURST = 3.0    # tokens of burst
POLYMARKET_US_BULK_RESERVE = 1.0  # tokens a bulk caller leaves for live callers
POLYMARKET_US_MAX_RATE = 2.0
POLYMARKET_US_MIN_RATE = 0.1


def retry_after_seconds(exc, *, now: datetime | None = None) -> float | None:
    """Seconds the server asked us to wait, or None if this is not a 429.

    Duck-typed on the HTTP response so this module keeps no provider import.
    """
    response = getattr(exc, "response", None)
    if getattr(response, "status_code", None) != 429:
        return None
    headers = getattr(response, "headers", None) or {}
    try:
        raw = headers.get("retry-after")
    except AttributeError:
        return 1.0
    try:
        seconds = float(raw)
        return max(0.0, seconds) if math.isfinite(seconds) else 1.0
    except (TypeError, ValueError):
        try:
            until = parsedate_to_datetime(raw)
            if until.tzinfo is None:
                return 1.0
            return max(0.0, (until - (now or datetime.now(timezone.utc))).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 1.0


class BudgetExhausted(RuntimeError):
    """The caller's deadline passed before the budget could pay for the request."""


class PolymarketUSRateLimiter:
    """Polymarket US read pacing for callers sharing one Soccer data directory.

    ``run`` paces the call, and on a 429 waits exactly as long as the server
    asked, drains the bucket so sibling callers wait too, and lowers the assumed
    rate. Sustained success walks the rate back up. ``state_path`` enables a
    short flock around state transactions across processes; HTTP and waits do
    not hold the file lock. Without a path, state is isolated in memory for tests.
    """

    def __init__(self, rate: float = POLYMARKET_US_READ_RATE,
                 burst: float = POLYMARKET_US_READ_BURST, *,
                 clock=time.monotonic, sleep=time.sleep, attempts: int = 4,
                 state_path: str | Path | None = None, wall_clock=time.time):
        self._bucket = TokenBucket(rate, burst, clock=clock)
        self._clock = clock
        self._sleep = sleep
        self._wall_clock = wall_clock
        self._attempts = max(1, int(attempts))
        self._lock = threading.Lock()
        self._cooldown_until = 0.0
        self._cooldown_wall_until = 0.0
        self._state_path = Path(state_path) if state_path is not None else None
        self._wins = 0
        self.rate_limited = 0
        self.budget_exhausted = 0

    def _read_state(self) -> None:
        try:
            with self._state_path.open('r', encoding='utf-8') as stream:
                raw = stream.read(8193)
        except FileNotFoundError:
            return
        try:
            if len(raw) > 8192:
                raise ValueError('oversized state')
            state = json.loads(raw)
            if state.get('version') != 1:
                raise ValueError('unknown state version')
            values = {key: float(state[key]) for key in
                      ('rate', 'capacity', 'tokens', 'updated_at', 'cooldown_until', 'cooldown_wall_until')}
            if not all(math.isfinite(value) for value in values.values()):
                raise ValueError('non-finite state')
            if (values['capacity'] != self._bucket.capacity or
                    not POLYMARKET_US_MIN_RATE <= values['rate'] <= POLYMARKET_US_MAX_RATE or
                    not 0 <= values['tokens'] <= values['capacity'] or
                    values['updated_at'] < 0 or values['cooldown_until'] < 0 or values['cooldown_wall_until'] < 0 or
                    type(state['wins']) is not int or not 0 <= state['wins'] < 20):
                raise ValueError('invalid state range')
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise BudgetExhausted('Polymarket US shared budget state invalid') from exc
        self._bucket.rate = values['rate']
        self._wins = state['wins']
        self._cooldown_wall_until = values['cooldown_wall_until']
        now = self._clock()
        if values['updated_at'] > now:
            # Monotonic clocks restart on reboot. Do not carry an old absolute
            # deadline forever or mint a fresh burst immediately on restart.
            self._bucket._tokens = 0.0
            self._bucket._ts = now
            remaining = max(0.0, self._cooldown_wall_until - self._wall_clock())
            self._cooldown_until = now + max(1.0 / self._bucket.rate, remaining)
        else:
            self._bucket._tokens = values['tokens']
            self._bucket._ts = values['updated_at']
            self._cooldown_until = values['cooldown_until']

    def _write_state(self) -> None:
        # A distinct, stable lock inode guards the independently replaced
        # state file. Replacing the lock inode itself would split the lock domain.
        self._bucket.available  # refill with the same monotonic clock before saving
        payload = json.dumps({'version': 1, 'rate': self._bucket.rate,
            'capacity': self._bucket.capacity, 'tokens': self._bucket._tokens,
            'updated_at': self._bucket._ts, 'cooldown_until': self._cooldown_until,
            'cooldown_wall_until': self._cooldown_wall_until,
            'wins': self._wins}, sort_keys=True, allow_nan=False).encode('utf-8')
        fd, temporary = tempfile.mkstemp(prefix=self._state_path.name + '.',
                                        suffix='.lock', dir=self._state_path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._state_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @contextmanager
    def _state_lock(self, *, timeout: float = 5.0):
        with self._lock:
            if self._state_path is None:
                yield
                return
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = self._state_path.with_name(self._state_path.name + '.lock')
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            deadline = self._clock() + timeout
            try:
                os.fchmod(fd, 0o600)
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        remaining = deadline - self._clock()
                        if remaining <= 0:
                            raise BudgetExhausted('Polymarket US shared budget lock unavailable')
                        self._sleep(min(0.01, remaining))
                self._read_state()
                try:
                    yield
                finally:
                    self._write_state()
            finally:
                os.close(fd)

    # ── adaptation ───────────────────────────────────────────────────────────
    def _slow_down(self, *, lower_rate: bool, retry_after: float) -> None:
        until = self._clock() + retry_after
        wall_until = self._wall_clock() + retry_after
        with self._state_lock(timeout=max(5.0, retry_after)):
            self._wins = 0
            self.rate_limited += 1
            if lower_rate:
                self._bucket.rate = max(POLYMARKET_US_MIN_RATE, self._bucket.rate * 0.7)
            # The deadline survives this run even when its last attempt failed or
            # its own budget cannot wait. Every later caller shares this cooldown.
            self._cooldown_until = max(self._cooldown_until, until)
            self._cooldown_wall_until = max(self._cooldown_wall_until, wall_until)
            self._bucket.drain()

    def _speed_up(self) -> None:
        with self._state_lock():
            self._bucket.available  # account elapsed time at the previous rate
            self._wins += 1
            if self._wins < 20:
                return
            self._wins = 0
            self._bucket.rate = min(POLYMARKET_US_MAX_RATE, self._bucket.rate + 0.05)

    @property
    def rate(self) -> float:
        with self._state_lock():
            return self._bucket.rate

    # ── pacing ───────────────────────────────────────────────────────────────
    def _acquire(self, *, bulk: bool, timeout: float) -> bool:
        """Take one token. A bulk caller leaves the reserve for live callers."""
        deadline = self._clock() + timeout
        required = 1.0 + (POLYMARKET_US_BULK_RESERVE if bulk else 0.0)
        while True:
            with self._state_lock(timeout=max(0.0, deadline - self._clock())):
                now = self._clock()
                if now >= deadline:
                    return False
                cooldown = max(0.0, self._cooldown_until - now)
                tokens = self._bucket.available
                if cooldown == 0.0 and tokens >= required and self._bucket.try_take(1):
                    return True
                refill = (max(0.0, required - tokens) / self._bucket.rate
                          if self._bucket.rate > 0 else 0.25)
                wait = max(0.01, cooldown, refill)
            # Both priorities use the injected clock/sleep pair. TokenBucket.take
            # uses real time.sleep, which cannot advance an injected test clock.
            self._sleep(min(wait, max(0.0, deadline - self._clock())))

    def run(self, call, *, bulk: bool = True, timeout: float = 30.0):
        """Bound admission/retry waiting, not an already started SDK request.

        The SDK's HTTP timeout still governs in-flight I/O. No background request
        is spawned or cancelled, and no shared client's timeout is modified.
        """
        deadline = self._clock() + timeout
        first_429 = True
        for attempt in range(self._attempts):
            remaining = deadline - self._clock()
            if remaining <= 0 or not self._acquire(bulk=bulk, timeout=remaining):
                self.budget_exhausted += 1
                raise BudgetExhausted("Polymarket US read budget unavailable before deadline")
            if self._clock() >= deadline:
                # Shared-state fsync/lock scheduling may consume the last margin
                # after a token was reserved. Never dispatch an expired permit.
                self.budget_exhausted += 1
                raise BudgetExhausted("Polymarket US read deadline passed before dispatch")
            try:
                result = call()
            except Exception as exc:  # noqa: BLE001 — only a 429 is ours to handle
                wait = retry_after_seconds(exc)
                if wait is None:
                    raise
                self._slow_down(lower_rate=first_429, retry_after=wait)
                first_429 = False
                if attempt == self._attempts - 1 or self._clock() + wait > deadline:
                    raise
                self._sleep(wait)
                continue
            self._speed_up()
            return result
        raise BudgetExhausted("Polymarket US read budget unavailable before deadline")


_polymarket_us_limiter: PolymarketUSRateLimiter | None = None
_polymarket_us_lock = threading.Lock()


def polymarket_us_limiter() -> PolymarketUSRateLimiter:
    """Per-process instance backed by Soccer's cross-process budget state.

    These private .lock files are covered by the existing data/*.lock ignore;
    neither file contains credentials, an account identifier, or market data.
    """
    global _polymarket_us_limiter
    with _polymarket_us_lock:
        if _polymarket_us_limiter is None:
            from prediction_market_soccer.config import CONFIG
            _polymarket_us_limiter = PolymarketUSRateLimiter(
                state_path=CONFIG.paths.data / '.polymarket_us_read_budget_state.lock')
        return _polymarket_us_limiter
