"""Self-healing policies: the engine should never halt permanently on an operational anomaly.

* ``Backoff``: exponential backoff with full jitter, capped.
* ``PlatformBackoff``: per-platform cooldown after a transient failure (5xx, 429, network),
  persisted in state so it survives restarts. The main loop never waits on it; the platform is
  just skipped until its ``next_at``.
* ``retry_sqlite``: retries an operation when SQLite reports the database locked or busy, with
  randomised jitter, up to 5 attempts.
* ``Quarantine``: when the circuit breaker trips on consecutive systemic errors, raise an alert
  and cool down (2h, doubling on repeat, capped at 24h) instead of stopping forever. After the
  cooldown, a self-diagnostic runs (DB integrity, write/read, disk space, network). If it passes,
  the breaker resets and a clean cycle is attempted; if it fails, the quarantine extends.
  **Manual stops** (``automonetize stop``, the ``EMERGENCY_STOP`` file) are never lifted
  automatically.
"""

from __future__ import annotations

import logging
import platform
import random
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable, TypeVar

from tools.errors import HttpError, OperationalFailure

if TYPE_CHECKING:  # pragma: no cover
    from agent.config import Config
    from agent.state import StateStore

log = logging.getLogger("automonetize.recovery")
T = TypeVar("T")

TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 522, 524}
LOCKED_MARKERS = ("database is locked", "database table is locked", "database is busy")


@dataclass
class Backoff:
    base_seconds: float = 60.0
    cap_seconds: float = 24 * 3600.0
    factor: float = 2.0

    def delay(self, failures: int, rng: random.Random | None = None) -> float:
        """Delay after the ``failures``-th consecutive failure: full jitter over base * factor^(n-1)."""
        ceiling = min(self.cap_seconds, self.base_seconds * self.factor ** max(0, failures - 1))
        r = rng or random
        return ceiling / 2 + r.uniform(0, ceiling / 2)  # never less than half: avoids hammering


def is_transient(exc: BaseException) -> bool:
    """5xx / 429 / timeouts / connection errors (also when wrapped in OperationalFailure)."""
    if isinstance(exc, OperationalFailure) and exc.last_exception is not None:
        return is_transient(exc.last_exception)
    if isinstance(exc, HttpError):
        return exc.status in TRANSIENT_STATUS
    return isinstance(exc, (ConnectionError, TimeoutError, OSError))


class PlatformBackoff:
    """Per-platform cooldown after failures, persisted in ``kv`` (key ``backoff:<platform>``)."""

    def __init__(self, state: "StateStore", backoff: Backoff | None = None, rng: random.Random | None = None):
        self.state = state
        self.backoff = backoff or Backoff(base_seconds=15 * 60, cap_seconds=24 * 3600)
        self.rng = rng

    def _key(self, platform_name: str) -> str:
        return f"backoff:{platform_name}"

    def blocked_until(self, platform_name: str) -> datetime | None:
        rec = self.state.get(self._key(platform_name))
        if not rec:
            return None
        until = datetime.fromisoformat(rec["next_at"])
        return until if until > self.state.clock() else None

    def failure(self, platform_name: str, exc: BaseException) -> datetime:
        rec = self.state.get(self._key(platform_name)) or {"failures": 0}
        failures = int(rec["failures"]) + 1
        # Permanent errors (401/403/404...) also back off, but start at the long end: retrying a bad
        # token every cycle helps nobody.
        effective = failures if is_transient(exc) else failures + 6
        wait = self.backoff.delay(effective, self.rng)
        retry_after = getattr(exc, "retry_after", None)
        if retry_after:
            wait = max(wait, float(retry_after))
        until = (self.state.clock() + timedelta(seconds=wait)).replace(microsecond=0)
        self.state.set(self._key(platform_name), {
            "failures": failures, "next_at": until.isoformat(timespec="seconds"),
            "last_error": repr(exc)[:300], "transient": is_transient(exc),
        })
        return until

    def success(self, platform_name: str) -> None:
        if self.state.get(self._key(platform_name)):
            self.state.set(self._key(platform_name), None)


def is_locked(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and any(m in str(exc).lower() for m in LOCKED_MARKERS)


def retry_sqlite(fn: Callable[[], T], attempts: int = 5, base_delay: float = 0.05, max_delay: float = 2.0,
                 sleep: Callable[[float], None] = time.sleep, rng: random.Random | None = None) -> T:
    """Run ``fn``; on "database is locked", retry with randomised exponential jitter.

    SQLite's own ``busy_timeout`` already waits inside each attempt; this covers the rare case
    where that isn't enough (a long checkpoint, a CLI process holding the write lock)."""
    r = rng or random
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            if not is_locked(exc) or attempt == attempts:
                raise
            ceiling = min(max_delay, base_delay * 2 ** (attempt - 1))
            delay = r.uniform(ceiling / 2, ceiling)
            log.warning("sqlite locked (attempt %d/%d), retrying in %.2fs", attempt, attempts, delay)
            sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


# ----------------------------------------------------------------------------- quarantine
def self_diagnostic(state: "StateStore", config: "Config", online_check: Callable[[], bool]) -> dict[str, Any]:
    """Cheap health checks run before leaving quarantine."""
    checks: dict[str, Any] = {}
    try:
        checks["db_integrity"] = state.conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    except sqlite3.Error as exc:
        checks["db_integrity"] = f"error: {exc}"
    try:
        probe = f"diag-{time.time_ns()}"
        state.set("diagnostic_probe", probe)
        checks["db_write"] = state.get("diagnostic_probe") == probe
    except sqlite3.Error as exc:
        checks["db_write"] = f"error: {exc}"
    try:
        free = shutil.disk_usage(config.data_dir).free
        checks["disk_free_mb"] = free // (1024 * 1024)
        checks["disk_ok"] = free > 100 * 1024 * 1024
    except OSError as exc:
        checks["disk_ok"] = f"error: {exc}"
    checks["network"] = bool(online_check())
    checks["passed"] = all(checks[k] is True for k in ("db_integrity", "db_write", "disk_ok", "network"))
    return checks


def send_alert(state: "StateStore", config: "Config", title: str, message: str) -> None:
    """Record an alert where a human will see it without watching a terminal: the error log, a file
    in ``data/``, and a macOS notification when running on a Mac."""
    state.log_error("alert", f"{title}: {message}", kind="alert")
    try:
        path = config.data_dir / "ALERTS.log"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{state.now()} {title}: {message}\n")
    except OSError:
        pass
    if getattr(config, "alert_notifications", True) and platform.system() == "Darwin" and shutil.which("osascript"):
        script = f'display notification {_applescript_str(message[:200])} with title {_applescript_str("AutoMonetize: " + title)}'
        try:
            subprocess.run(["osascript", "-e", script], timeout=10, check=False, capture_output=True)
        except (OSError, subprocess.SubprocessError):
            pass


def _applescript_str(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Quarantine:
    """Automatic cooldown instead of a permanent emergency stop for *systemic* trips."""

    KEY = "quarantine"

    def __init__(self, state: "StateStore", config: "Config"):
        self.state = state
        self.config = config

    @property
    def record(self) -> dict[str, Any] | None:
        return self.state.get(self.KEY)

    def enter(self, reason: str) -> datetime:
        prev = self.record or {}
        # Escalate if the last quarantine ended recently: 2h, 4h, 8h ... capped.
        recent = prev.get("ended_at") and self.state.clock() - datetime.fromisoformat(prev["ended_at"]) < timedelta(hours=24)
        count = int(prev.get("count", 0)) + 1 if recent else 1
        hours = min(self.config.quarantine_max_hours, self.config.quarantine_hours * 2 ** (count - 1))
        until = self.state.clock() + timedelta(hours=hours)
        self.state.set(self.KEY, {"active": True, "reason": reason, "count": count,
                                  "since": self.state.now(), "until": until.isoformat(timespec="seconds")})
        send_alert(self.state, self.config, "quarantine",
                   f"{reason}. Cooling down for {hours:g}h, then self-diagnostic and automatic resume.")
        return until

    def active(self) -> bool:
        rec = self.record
        return bool(rec and rec.get("active"))

    def due(self) -> bool:
        rec = self.record
        return bool(rec and rec.get("active") and self.state.clock() >= datetime.fromisoformat(rec["until"]))

    def extend(self, why: str) -> datetime:
        rec = dict(self.record or {})
        hours = min(self.config.quarantine_max_hours, self.config.quarantine_hours)
        until = self.state.clock() + timedelta(hours=hours)
        rec.update(until=until.isoformat(timespec="seconds"), last_diagnostic=why)
        self.state.set(self.KEY, rec)
        send_alert(self.state, self.config, "quarantine extended", why)
        return until

    def release(self, diagnostic: dict[str, Any]) -> None:
        rec = dict(self.record or {})
        rec.update(active=False, ended_at=self.state.now(), last_diagnostic=diagnostic)
        self.state.set(self.KEY, rec)
        send_alert(self.state, self.config, "resumed", "self-diagnostic passed; circuit breaker reset, running a clean cycle")
