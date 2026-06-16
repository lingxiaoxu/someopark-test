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
