"""Sandboxed tools the agent acts through, bundled into a :class:`Toolkit`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from tools.circuit_breaker import CircuitBreaker, RateLimiter
from tools.file_io import SandboxedFileIO
from tools.http_client import HttpClient, Transport
from tools.revenue_tracker import RevenueTracker
from tools.shell_runner import ShellRunner

if TYPE_CHECKING:  # pragma: no cover
    from agent.config import Config
    from agent.state import StateStore


@dataclass
class Toolkit:
    config: "Config"
    state: "StateStore"
    breaker: CircuitBreaker
    http: HttpClient
    files: SandboxedFileIO
    shell: ShellRunner
    revenue: RevenueTracker


def build_toolkit(
    config: "Config",
    state: "StateStore",
    breaker: CircuitBreaker,
    transport: Transport | None = None,
    sleep=None,
) -> Toolkit:
    import time

    sleep = sleep or time.sleep

    def error_sink(source: str, message: str, tb: str) -> None:
        state.log_error(source, message, tb)

    http = HttpClient(
        rate_limiter=RateLimiter(config.http_rate_per_minute, sleep=sleep),
        breaker=breaker,
        transport=transport,
        error_sink=error_sink,
        timeout=config.http_timeout,
        respect_robots=config.respect_robots_txt,
        backoff_seconds=config.http_backoff_seconds,
        sleep=sleep,
    )
    files = SandboxedFileIO(config.data_dir)
    shell = ShellRunner(config.workspace_dir, config.shell_allowlist, timeout=config.shell_timeout)
    revenue = RevenueTracker(
        state,
        daily_target_cents=config.daily_target_cents,
        http=http,
        gumroad_token=config.gumroad_access_token,
        fee_pct=config.platform_fee_pct,
        fee_fixed_cents=config.platform_fee_fixed_cents,
    )
    return Toolkit(config=config, state=state, breaker=breaker, http=http, files=files, shell=shell, revenue=revenue)


__all__ = ["Toolkit", "build_toolkit"]
