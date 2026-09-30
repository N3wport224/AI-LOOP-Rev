"""macOS power management: stay awake while working, sleep when idle, wake for the next cycle.

* ``PowerManager.hold(reason)`` is a reference-counted context manager. While at least one hold is
  open (an engine cycle, a webhook being verified and fulfilled), the Mac won't idle-sleep. The
  engine and webhook threads share one manager, so overlapping work takes one assertion, and it's
  released when the last hold closes. Between cycles nothing is held and the Mac can sleep normally.
* Backends, best first:

  1. ``IOKitBackend``: ``IOPMAssertionCreateWithName("PreventUserIdleSystemSleep")`` via ctypes,
     the same API ``caffeinate -i`` uses, without a child process.
  2. ``CaffeinateBackend``: ``caffeinate -i -w <our pid>``. ``-w`` means the assertion dies with
     this process even if it's killed with SIGKILL.
  3. ``NullBackend``: other systems, or ``power_assertions = false``. Hooks still run, so behaviour
     is identical and testable on Linux.

  An IOKit assertion is also dropped by the kernel when the process exits, so a crash can't leave
  the Mac unable to sleep.
* ``schedule_wake(at)`` asks ``pmset`` to wake the Mac for the next cycle. ``pmset schedule`` needs
  root, so it runs as ``sudo -n`` (never prompts) and only works after the one-line sudoers rule
  that ``automonetize setup-autonomous`` prints. Without it the Mac still wakes for the next cycle
  whenever it's awake for any other reason, and a laptop on power with sleep disabled never needs it.

Honest limit: a Mac that is asleep can't receive webhooks. Stripe retries failed deliveries for up
to 3 days and the ``sync_revenue`` poll catches anything missed, so orders are delayed, not lost.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import platform
import shutil
import subprocess
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Callable, Iterator, Protocol

log = logging.getLogger("automonetize.power")

ASSERTION_TYPE = "PreventUserIdleSystemSleep"
K_IOPM_ASSERTION_LEVEL_ON = 255
K_CFSTRING_ENCODING_UTF8 = 0x08000100
K_IORETURN_SUCCESS = 0
IOKIT_PATH = "/System/Library/Frameworks/IOKit.framework/IOKit"
COREFOUNDATION_PATH = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
MIN_WAKE_LEAD_SECONDS = 300  # don't schedule wakes for short intervals: the Mac rarely sleeps that fast


class Backend(Protocol):
    name: str

    def acquire(self, reason: str) -> None: ...

    def release(self) -> None: ...


class NullBackend:
    name = "none"

    def acquire(self, reason: str) -> None:
        pass

    def release(self) -> None:
        pass


class IOKitBackend:
    """``IOPMAssertionCreateWithName`` / ``IOPMAssertionRelease`` through ctypes."""

    name = "iokit"

    def __init__(self, loader: Callable[[str], Any] = ctypes.cdll.LoadLibrary):
        self.iokit = loader(IOKIT_PATH)
        self.cf = loader(COREFOUNDATION_PATH)
        self.cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        self.cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        self.cf.CFRelease.argtypes = [ctypes.c_void_p]
        self.iokit.IOPMAssertionCreateWithName.restype = ctypes.c_int32
        self.iokit.IOPMAssertionCreateWithName.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32),
        ]
        self.iokit.IOPMAssertionRelease.restype = ctypes.c_int32
        self.iokit.IOPMAssertionRelease.argtypes = [ctypes.c_uint32]
        self._id: int | None = None

    def _cfstr(self, text: str) -> int:
        ref = self.cf.CFStringCreateWithCString(None, text.encode("utf-8"), K_CFSTRING_ENCODING_UTF8)
        if not ref:
            raise OSError("CFStringCreateWithCString failed")
        return ref

    def acquire(self, reason: str) -> None:
        if self._id is not None:
            return
        kind, name = self._cfstr(ASSERTION_TYPE), self._cfstr(f"AutoMonetize: {reason}")
        try:
            assertion = ctypes.c_uint32(0)
            rc = self.iokit.IOPMAssertionCreateWithName(kind, K_IOPM_ASSERTION_LEVEL_ON, name, ctypes.byref(assertion))
        finally:
            self.cf.CFRelease(kind)
            self.cf.CFRelease(name)
        if rc != K_IORETURN_SUCCESS:
            raise OSError(f"IOPMAssertionCreateWithName returned {rc:#x}")
        self._id = assertion.value

    def release(self) -> None:
        if self._id is None:
            return
        rc = self.iokit.IOPMAssertionRelease(self._id)
        self._id = None
        if rc != K_IORETURN_SUCCESS:
            log.warning("IOPMAssertionRelease returned %#x", rc)


class CaffeinateBackend:
    """``caffeinate -i -w <pid>``: dies with us even on SIGKILL."""

    name = "caffeinate"

    def __init__(self, binary: str = "caffeinate", popen: Callable[..., Any] = subprocess.Popen):
        self.binary = binary
        self.popen = popen
        self._proc: Any = None

    def acquire(self, reason: str) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        self._proc = self.popen([self.binary, "-i", "-w", str(os.getpid())],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def release(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def select_backend(enabled: bool = True, system: str | None = None,
                   loader: Callable[[str], Any] = ctypes.cdll.LoadLibrary,
                   which: Callable[[str], str | None] = shutil.which) -> Backend:
    if not enabled or (system or platform.system()) != "Darwin":
        return NullBackend()
    try:
        return IOKitBackend(loader)
    except (OSError, AttributeError) as exc:
        log.info("IOKit unavailable (%s); falling back to caffeinate", exc)
    path = which("caffeinate")
    return CaffeinateBackend(path) if path else NullBackend()


class PowerManager:
    def __init__(self, backend: Backend | None = None, schedule_wake: bool = False,
                 runner: Callable[..., Any] = subprocess.run, system: str | None = None):
        self.backend = backend or NullBackend()
        self.wake_enabled = schedule_wake
        self.runner = runner
        self.system = system or platform.system()
        self._lock = threading.Lock()
        self._holds: dict[int, str] = {}
        self._next = 0
        self.acquired = 0          # number of times the assertion was taken (for tests and status)
        self.last_error = ""
        self.last_wake: datetime | None = None
        self.wake_denied = False   # sudo -n refused: stop trying until restart

    @property
    def active(self) -> int:
        with self._lock:
            return len(self._holds)

    def reasons(self) -> list[str]:
        with self._lock:
            return list(self._holds.values())

    @contextmanager
    def hold(self, reason: str) -> Iterator[None]:
        with self._lock:
            token = self._next
            self._next += 1
            first = not self._holds
            self._holds[token] = reason
            if first:
                try:
                    self.backend.acquire(reason)
                    self.acquired += 1
                except Exception as exc:  # noqa: BLE001 - staying awake is best effort, never fatal
                    self.last_error = repr(exc)
                    log.warning("power assertion failed: %r", exc)
        try:
            yield
        finally:
            with self._lock:
                self._holds.pop(token, None)
                if not self._holds:
                    try:
                        self.backend.release()
                    except Exception as exc:  # noqa: BLE001
                        self.last_error = repr(exc)
                        log.warning("power release failed: %r", exc)

    def release_all(self) -> None:
        with self._lock:
            self._holds.clear()
            try:
                self.backend.release()
            except Exception as exc:  # noqa: BLE001
                self.last_error = repr(exc)

    def schedule_wake(self, at: datetime, now: datetime | None = None) -> bool:
        """Ask ``pmset`` to wake the Mac at ``at`` (local time). Returns True if scheduled."""
        if not self.wake_enabled or self.wake_denied or self.system != "Darwin":
            return False
        now = now or datetime.now(at.tzinfo)
        if (at - now).total_seconds() < MIN_WAKE_LEAD_SECONDS:
            return False
        if self.last_wake is not None and abs((self.last_wake - at).total_seconds()) < 60:
            return True
        stamp = at.astimezone().strftime("%m/%d/%y %H:%M:%S")  # pmset wants local time
        try:
            proc = self.runner(["sudo", "-n", "/usr/bin/pmset", "schedule", "wake", stamp],
                               capture_output=True, text=True, timeout=15, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            self.last_error = repr(exc)
            return False
        if proc.returncode != 0:
            self.wake_denied = True
            self.last_error = (proc.stderr or proc.stdout or "").strip()[:300]
            log.info("pmset schedule wake unavailable (%s); add the sudoers rule from setup-autonomous", self.last_error)
            return False
        self.last_wake = at
        return True

    def status(self) -> dict[str, Any]:
        return {"backend": self.backend.name, "active_holds": self.active, "reasons": self.reasons(),
                "acquired": self.acquired, "wake_scheduling": self.wake_enabled and not self.wake_denied,
                "last_wake": self.last_wake.isoformat(timespec="seconds") if self.last_wake else None,
                "last_error": self.last_error}


_shared: PowerManager | None = None
_shared_lock = threading.Lock()


def shared(config: Any = None) -> PowerManager:
    """The process-wide manager, so engine and webhook threads share one reference count."""
    global _shared
    with _shared_lock:
        if _shared is None:
            enabled = bool(getattr(config, "power_assertions", True))
            _shared = PowerManager(select_backend(enabled), schedule_wake=bool(getattr(config, "schedule_wake", False)))
        return _shared


def reset_shared() -> None:
    global _shared
    with _shared_lock:
        if _shared is not None:
            _shared.release_all()
        _shared = None
