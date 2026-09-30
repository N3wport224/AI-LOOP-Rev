"""Request throttling for the API.

Two layers, because they answer different questions:

* **Token bucket per key** (in memory): smooths bursts. ``api_burst`` tokens, refilled at
  ``api_rate_per_second``. A request finding the bucket empty gets 429 ``rate_limited`` with the
  exact ``Retry-After``. Losing the buckets on restart is harmless: they refill in seconds.
* **Daily quota per key** (SQLite, ``api_usage``): what the plan includes. Survives restarts,
  resets at 00:00 UTC. Exhausted → 429 ``quota_exceeded`` with ``Retry-After`` = seconds to reset.

Plus a brake on **authentication failures per client IP** so nobody can guess keys at speed.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable


@dataclass
class TokenBucket:
    capacity: float
    rate: float            # tokens per second
    tokens: float
    updated: float

    def take(self, now: float, cost: float = 1.0) -> tuple[bool, float]:
        """Returns (allowed, seconds until a token is available)."""
        elapsed = max(0.0, now - self.updated)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.updated = now
        if self.tokens >= cost:
            self.tokens -= cost
            return True, 0.0
        return False, (cost - self.tokens) / self.rate if self.rate > 0 else math.inf

    def remaining(self) -> int:
        return int(self.tokens)


class RateLimiter:
    def __init__(self, capacity: int, rate: float, clock: Callable[[], float] = time.monotonic, max_keys: int = 50_000):
        self.capacity = float(capacity)
        self.rate = float(rate)
        self.clock = clock
        self.max_keys = max_keys
        self._buckets: dict[int, TokenBucket] = {}
        self._lock = threading.Lock()

    def allow(self, key_id: int) -> tuple[bool, float]:
        now = self.clock()
        with self._lock:
            bucket = self._buckets.get(key_id)
            if bucket is None:
                if len(self._buckets) >= self.max_keys:
                    self._buckets.clear()  # full buckets are the default anyway
                bucket = self._buckets[key_id] = TokenBucket(self.capacity, self.rate, self.capacity, now)
            return bucket.take(now)

    def forget(self, key_id: int) -> None:
        with self._lock:
            self._buckets.pop(key_id, None)


class FailureLimiter:
    """At most ``limit`` failed authentications per client per hour."""

    def __init__(self, limit: int, clock: Callable[[], float] = time.monotonic):
        self.limit = limit
        self.clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _window(self, ip: str, now: float) -> deque[float]:
        q = self._hits.setdefault(ip or "unknown", deque())
        while q and now - q[0] > 3600:
            q.popleft()
        return q

    def blocked(self, ip: str) -> float:
        """Seconds until this client may try again (0 when not blocked)."""
        now = self.clock()
        with self._lock:
            q = self._window(ip, now)
            return max(1.0, 3600 - (now - q[0])) if len(q) >= self.limit else 0.0

    def record(self, ip: str) -> None:
        now = self.clock()
        with self._lock:
            self._window(ip, now).append(now)
            if len(self._hits) > 20_000:
                for k in [k for k, v in self._hits.items() if not v][:10_000]:
                    del self._hits[k]
