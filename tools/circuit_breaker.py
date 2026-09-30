"""Circuit breakers and rate limiters that bound what the agent can do."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from tools.errors import CircuitOpenError


@dataclass
class CircuitBreaker:
    """Per-cycle action/API budgets plus an emergency stop on consecutive errors.

    * ``max_actions_per_cycle`` caps how many tasks one cycle may execute.
    * ``max_api_calls_per_cycle`` caps outbound API calls in one cycle.
    * ``max_consecutive_errors`` trips the breaker (emergency stop) when that many
      failures happen back to back. A tripped breaker stays tripped until ``reset()``.
    """

    max_actions_per_cycle: int = 10
    max_api_calls_per_cycle: int = 60
    max_consecutive_errors: int = 5
    consecutive_errors: int = 0
    tripped: bool = False
    trip_reason: str = ""
    actions_this_cycle: int = 0
    api_calls_this_cycle: int = 0
    total_failures: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def begin_cycle(self) -> None:
        with self._lock:
            self.actions_this_cycle = 0
            self.api_calls_this_cycle = 0

    def allow_action(self) -> bool:
        with self._lock:
            if self.tripped or self.actions_this_cycle >= self.max_actions_per_cycle:
                return False
            self.actions_this_cycle += 1
            return True

    def check_api_call(self) -> None:
        """Consume one API call from the budget or raise :class:`CircuitOpenError`."""
        with self._lock:
            if self.tripped:
                raise CircuitOpenError(f"circuit tripped: {self.trip_reason}")
            if self.api_calls_this_cycle >= self.max_api_calls_per_cycle:
                raise CircuitOpenError(
                    f"API budget exhausted ({self.max_api_calls_per_cycle} calls/cycle)"
                )
            self.api_calls_this_cycle += 1

    def record_success(self) -> None:
        with self._lock:
            self.consecutive_errors = 0

    def record_failure(self, reason: str = "") -> bool:
        """Record a failure. Returns True if this failure tripped the breaker."""
        with self._lock:
            self.consecutive_errors += 1
            self.total_failures += 1
            if not self.tripped and self.consecutive_errors >= self.max_consecutive_errors:
                self.tripped = True
                self.trip_reason = (
                    f"{self.consecutive_errors} consecutive errors" + (f"; last: {reason}" if reason else "")
                )
                return True
            return False

    def trip(self, reason: str) -> None:
        with self._lock:
            self.tripped = True
            self.trip_reason = reason

    def reset(self) -> None:
        with self._lock:
            self.tripped = False
            self.trip_reason = ""
            self.consecutive_errors = 0

    def health(self) -> dict:
        with self._lock:
            if self.tripped:
                status = "TRIPPED"
            elif self.consecutive_errors >= max(1, self.max_consecutive_errors - 1):
                status = "DEGRADED"
            elif self.consecutive_errors:
                status = "WARN"
            else:
                status = "OK"
            return {
                "status": status,
                "tripped": self.tripped,
                "trip_reason": self.trip_reason,
                "consecutive_errors": self.consecutive_errors,
                "max_consecutive_errors": self.max_consecutive_errors,
                "actions_this_cycle": self.actions_this_cycle,
                "max_actions_per_cycle": self.max_actions_per_cycle,
                "api_calls_this_cycle": self.api_calls_this_cycle,
                "max_api_calls_per_cycle": self.max_api_calls_per_cycle,
                "total_failures": self.total_failures,
            }


class RateLimiter:
    """Token-bucket limiter keyed by e.g. hostname."""

    def __init__(
        self,
        rate_per_minute: float,
        burst: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if rate_per_minute <= 0:
            raise ValueError("rate_per_minute must be positive")
        self.rate_per_second = rate_per_minute / 60.0
        self.capacity = float(burst if burst is not None else max(1, int(rate_per_minute // 10) or 1))
        self._clock = clock
        self._sleep = sleep
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def _refill(self, key: str) -> float:
        now = self._clock()
        tokens, last = self._buckets.get(key, (self.capacity, now))
        tokens = min(self.capacity, tokens + (now - last) * self.rate_per_second)
        self._buckets[key] = (tokens, now)
        return tokens

    def try_acquire(self, key: str = "default") -> bool:
        with self._lock:
            tokens = self._refill(key)
            if tokens >= 1:
                self._buckets[key] = (tokens - 1, self._buckets[key][1])
                return True
            return False

    def wait_time(self, key: str = "default") -> float:
        with self._lock:
            tokens = self._refill(key)
            return 0.0 if tokens >= 1 else (1 - tokens) / self.rate_per_second

    def acquire(self, key: str = "default", max_wait: float = 120.0) -> None:
        """Block until a token is available, or raise if that would exceed ``max_wait``."""
        waited = 0.0
        while not self.try_acquire(key):
            delay = self.wait_time(key)
            if waited + delay > max_wait:
                raise CircuitOpenError(f"rate limit wait for {key!r} exceeds {max_wait}s")
            self._sleep(delay)
            waited += delay
