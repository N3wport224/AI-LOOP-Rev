"""Exception types shared by every tool."""

from __future__ import annotations


class ToolError(Exception):
    """Base class for tool-level failures."""


class SandboxViolation(ToolError):
    """A path, command or argument tried to escape the sandbox."""


class CircuitOpenError(ToolError):
    """A circuit breaker or budget refused the call."""


class RobotsDisallowed(ToolError):
    """robots.txt forbids fetching the URL."""


class HttpError(ToolError):
    def __init__(self, status: int, url: str, body: str = "", retry_after: float | None = None):
        super().__init__(f"HTTP {status} for {url}")
        self.status = status
        self.url = url
        self.body = body
        self.retry_after = retry_after  # seconds from a Retry-After header, when the server sent one


class OperationalFailure(ToolError):
    """Raised after every retry attempt has been exhausted."""

    def __init__(self, message: str, attempts: int, last_exception: BaseException | None):
        super().__init__(message)
        self.attempts = attempts
        self.last_exception = last_exception


# Errors that retrying cannot fix: they are policy decisions, not transient faults.
NON_RETRYABLE: tuple[type[BaseException], ...] = (SandboxViolation, CircuitOpenError, RobotsDisallowed)
