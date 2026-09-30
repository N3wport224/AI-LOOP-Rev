import pytest

from tools.circuit_breaker import CircuitBreaker, RateLimiter
from tools.errors import CircuitOpenError, OperationalFailure, SandboxViolation
from tools.recovery import retry_with_adjustment


def test_action_budget_per_cycle():
    b = CircuitBreaker(max_actions_per_cycle=2)
    assert b.allow_action() and b.allow_action()
    assert not b.allow_action()
    b.begin_cycle()
    assert b.allow_action()


def test_api_budget_raises_when_exhausted():
    b = CircuitBreaker(max_api_calls_per_cycle=2)
    b.check_api_call()
    b.check_api_call()
    with pytest.raises(CircuitOpenError):
        b.check_api_call()
    b.begin_cycle()
    b.check_api_call()


def test_consecutive_errors_trip_and_success_resets():
    b = CircuitBreaker(max_consecutive_errors=3)
    assert not b.record_failure("a")
    assert not b.record_failure("b")
    b.record_success()
    assert b.consecutive_errors == 0
    assert not b.record_failure()
    assert not b.record_failure()
    assert b.record_failure("boom") is True
    assert b.tripped and "boom" in b.trip_reason
    assert not b.allow_action()
    with pytest.raises(CircuitOpenError):
        b.check_api_call()
    assert b.health()["status"] == "TRIPPED"
    b.reset()
    assert not b.tripped and b.allow_action()


def test_health_levels():
    b = CircuitBreaker(max_consecutive_errors=4)
    assert b.health()["status"] == "OK"
    b.record_failure()
    assert b.health()["status"] == "WARN"
    b.record_failure()
    b.record_failure()
    assert b.health()["status"] == "DEGRADED"


class FakeTime:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def clock(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def test_rate_limiter_blocks_until_token_refills():
    ft = FakeTime()
    rl = RateLimiter(rate_per_minute=60, burst=2, clock=ft.clock, sleep=ft.sleep)
    rl.acquire("h")
    rl.acquire("h")
    assert not rl.try_acquire("h")
    rl.acquire("h")  # must wait ~1s
    assert ft.slept and ft.slept[0] == pytest.approx(1.0)
    # separate keys have separate buckets
    assert rl.try_acquire("other")


def test_rate_limiter_max_wait():
    ft = FakeTime()
    rl = RateLimiter(rate_per_minute=1, burst=1, clock=ft.clock, sleep=ft.sleep)
    rl.acquire()
    with pytest.raises(CircuitOpenError):
        rl.acquire(max_wait=5)


def test_retry_adjusts_params_and_logs_each_failure():
    seen, errors = [], []

    def fn(p):
        seen.append(dict(p))
        if p["size"] > 25:
            raise ValueError("too big")
        return p["size"]

    result = retry_with_adjustment(
        fn, {"size": 100}, attempts=3,
        adjust=lambda n, exc, p: {**p, "size": p["size"] // 2},
        on_error=lambda n, exc, tb: errors.append((n, tb)),
    )
    assert result == 25
    assert [s["size"] for s in seen] == [100, 50, 25]
    assert len(errors) == 2 and "ValueError" in errors[0][1]


def test_retry_gives_up_after_three_attempts():
    calls = []
    with pytest.raises(OperationalFailure) as info:
        retry_with_adjustment(lambda p: calls.append(1) or (_ for _ in ()).throw(RuntimeError("x")), {})
    assert len(calls) == 3
    assert info.value.attempts == 3
    assert isinstance(info.value.last_exception, RuntimeError)


def test_retry_does_not_retry_policy_violations():
    calls = []

    def fn(p):
        calls.append(1)
        raise SandboxViolation("nope")

    with pytest.raises(SandboxViolation):
        retry_with_adjustment(fn, {})
    assert len(calls) == 1
