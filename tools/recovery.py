"""Self-correcting retry: intercept, log the traceback, adjust parameters, try again."""

from __future__ import annotations

import time
import traceback
from typing import Any, Callable, TypeVar

from tools.errors import NON_RETRYABLE, OperationalFailure

T = TypeVar("T")

ErrorSink = Callable[[int, BaseException, str], None]
Adjuster = Callable[[int, BaseException, dict[str, Any]], dict[str, Any]]

DEFAULT_ATTEMPTS = 3


def retry_with_adjustment(
    fn: Callable[[dict[str, Any]], T],
    params: dict[str, Any] | None = None,
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    adjust: Adjuster | None = None,
    on_error: ErrorSink | None = None,
    no_retry: tuple[type[BaseException], ...] = NON_RETRYABLE,
    backoff_seconds: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
    label: str = "operation",
) -> T:
    """Call ``fn(params)`` up to ``attempts`` times.

    After each failure the traceback goes to ``on_error`` and ``adjust`` may return a
    modified copy of ``params`` for the next attempt (new headers, smaller batch, longer
    timeout...). Exceptions listed in ``no_retry`` propagate immediately. When every
    attempt fails an :class:`OperationalFailure` is raised.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    current = dict(params or {})
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn(current)
        except no_retry:
            raise
        except Exception as exc:  # noqa: BLE001 - interception is the point
            last = exc
            if on_error is not None:
                on_error(attempt, exc, traceback.format_exc())
            if attempt == attempts:
                break
            if adjust is not None:
                current = adjust(attempt, exc, dict(current))
            if backoff_seconds > 0:
                sleep(backoff_seconds * (2 ** (attempt - 1)))
    raise OperationalFailure(
        f"{label} failed after {attempts} attempts: {last!r}", attempts, last
    ) from last
