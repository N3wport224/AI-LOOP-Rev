"""Process supervision: the engine loop and the webhook daemon as supervised worker threads.

* Both workers share one ``StateStore`` (its connection is lock-guarded) and one stop event.
* A worker that crashes is restarted with exponential backoff. More than
  ``supervisor_max_restarts`` crashes inside ``supervisor_restart_window_seconds`` means it's broken:
  a broken **webhook** worker is disabled (the engine's polling sync still records and delivers
  orders); a broken **engine** worker shuts the whole supervisor down.
* The engine and the webhook share one ``PowerManager``: the Mac stays awake while either is working
  and may sleep when both are idle (``agent.power``).
* Systemic failures never end the process: the engine quarantines itself and self-heals
  (``agent.recovery``). If the supervisor does exit, launchd's ``KeepAlive`` restarts it.
* SIGTERM, SIGINT and SIGHUP request a graceful shutdown: stop accepting work, let the current
  cycle and any in-flight fulfilment finish (up to ``shutdown_timeout_seconds``), flush the SQLite
  WAL into the database (``PRAGMA wal_checkpoint(TRUNCATE)``), write an operational checkpoint
  to state, and release the process lock.
* SIGUSR1, or ``agent.evolution.hot_reload.request_reload()`` (after a self-evolution merge or a
  rollback), requests a **graceful reload**: the same drain, then the process re-executes itself
  with the webhook's listening socket inherited, so the port never closes (``agent.evolution.hot_reload``).
* Every 30 s the supervisor also checks the post-evolution canary (rollback on regressions).
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from agent.config import Config
from agent.engine import CycleReport, Engine, ProcessLock
from agent.evolution import hot_reload

log = logging.getLogger("automonetize.supervisor")

SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
RELOAD_SIGNALS = (signal.SIGUSR1,)
CANARY_EVERY_SECONDS = 30.0


@dataclass
class Worker:
    name: str
    target: Callable[[threading.Event], Any]
    critical: bool = True
    thread: threading.Thread | None = None
    crashes: deque = field(default_factory=deque)
    last_error: str = ""
    finished: bool = False  # exited cleanly on its own (e.g. engine reached max_cycles)
    disabled: bool = False
    next_start: float = 0.0
    starts: int = 0


class Supervisor:
    def __init__(
        self,
        config: Config,
        engine: Engine | None = None,
        webhook: bool = True,
        interval: float | None = None,
        max_cycles: int | None = None,
        on_cycle: Callable[[CycleReport], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        backoff_base: float = 1.0,
        poll_seconds: float = 0.2,
        exec_fn: Callable[..., Any] | None = None,
    ):
        self.config = config
        self.engine = engine or Engine(config)
        self.state = self.engine.state
        self.stop_event = threading.Event()
        self.engine._stop_event = self.stop_event  # one switch stops everything
        self.monotonic = monotonic
        self.backoff_base = backoff_base
        self.poll_seconds = poll_seconds
        self.stop_reason = ""
        self.reloading = False
        self.exec_fn = exec_fn  # None = os.execve; tests pass a fake
        self.listen_socket: Any = None
        self._next_canary = 0.0
        self.lock = ProcessLock(config.data_dir / "agent.lock")
        self._previous_handlers: dict[int, Any] = {}
        self.workers: list[Worker] = [
            Worker("engine", lambda stop: self.engine.run_forever(
                interval=interval, max_cycles=max_cycles, on_cycle=on_cycle, install_signal_handlers=False), critical=True)
        ]
        if config.product_factory and getattr(self.engine, "_full_plan", False):  # not for test engines with custom plans
            self.workers.append(Worker("factory", self._factory_target(), critical=False))
        leads_public = (config.lead_magnet_enabled or config.api_enabled) and bool(config.lead_capture_base)
        if webhook and (config.stripe_webhook_secret or leads_public):
            from tools.storefront.webhook_listener import WebhookServer

            self.webhook_server = WebhookServer(self.engine.tools, power=self.engine.power)
            self.workers.append(Worker("webhook", self.webhook_server.run, critical=False))
        elif webhook:
            log.warning("STRIPE_WEBHOOK_SECRET not set: webhook listener disabled, polling sync only")

    def _factory_target(self) -> Callable[[threading.Event], Any]:
        """The product factory runs on its own toolkit (and API budget), every factory_interval_seconds."""
        def target(stop: threading.Event) -> None:
            from strategies.product_factory import run_worker
            from tools import build_toolkit
            from tools.circuit_breaker import CircuitBreaker

            cfg = self.config
            breaker = CircuitBreaker(max_actions_per_cycle=1000, max_api_calls_per_cycle=cfg.max_api_calls_per_cycle,
                                     max_consecutive_errors=1000)
            tools = build_toolkit(cfg, self.state, breaker)
            run_worker(tools, stop, self.engine.is_stopped)
        return target

    # -- signals -----------------------------------------------------------------------
    def _on_signal(self, signum: int, _frame: Any) -> None:
        # Only set flags here: the main loop does the actual shutdown work.
        if signum in RELOAD_SIGNALS:
            hot_reload.request_reload(signal.Signals(signum).name)
            return
        self.stop_reason = self.stop_reason or signal.Signals(signum).name
        self.stop_event.set()

    def install_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for sig in SIGNALS + RELOAD_SIGNALS:
            self._previous_handlers[sig] = signal.signal(sig, self._on_signal)

    def restore_signal_handlers(self) -> None:
        for sig, handler in self._previous_handlers.items():
            signal.signal(sig, handler)
        self._previous_handlers.clear()

    # -- workers -------------------------------------------------------------------------
    def _start(self, w: Worker) -> None:
        def body() -> None:
            try:
                w.target(self.stop_event)
                w.finished = not self.stop_event.is_set()
            except BaseException as exc:  # noqa: BLE001 - recorded and handled by the supervisor
                import traceback

                w.last_error = repr(exc)
                self.state.log_error(f"supervisor:{w.name}", f"worker crashed: {exc!r}", traceback.format_exc())

        w.finished = False
        w.last_error = ""
        w.starts += 1
        w.thread = threading.Thread(target=body, name=f"automonetize-{w.name}", daemon=True)
        w.thread.start()

    def _check(self, w: Worker) -> None:
        if w.disabled or w.thread is None or w.thread.is_alive() or self.stop_event.is_set():
            return
        if w.finished:
            if w.critical:
                self.request_stop(f"{w.name} finished")
            return
        now = self.monotonic()
        if w.next_start == 0.0:  # crash just observed: schedule a restart
            window = self.config.supervisor_restart_window_seconds
            w.crashes.append(now)
            while w.crashes and now - w.crashes[0] > window:
                w.crashes.popleft()
            if len(w.crashes) > self.config.supervisor_max_restarts:
                if w.critical:
                    self.request_stop(f"{w.name} crashed {len(w.crashes)} times in {window}s: {w.last_error}")
                else:
                    w.disabled = True
                    self.state.log_error(f"supervisor:{w.name}", f"disabled after {len(w.crashes)} crashes; polling covers orders")
                return
            delay = min(60.0, self.backoff_base * 2 ** (len(w.crashes) - 1))
            w.next_start = now + delay
            log.warning("%s crashed (%s); restarting in %.1fs", w.name, w.last_error, delay)
        if now >= w.next_start:
            w.next_start = 0.0
            self._start(w)

    # -- lifecycle -------------------------------------------------------------------------
    def request_stop(self, reason: str) -> None:
        self.stop_reason = self.stop_reason or reason
        self.stop_event.set()

    def request_reload(self, reason: str) -> None:
        """Drain like a stop, then re-execute with the new code (``run`` does the exec)."""
        self.reloading = True
        self.request_stop(f"reload: {reason}")

    def _tick_canary(self) -> None:
        now = self.monotonic()
        if now < self._next_canary:
            return
        self._next_canary = now + CANARY_EVERY_SECONDS
        try:
            hot_reload.canary_tick(self.state, self.config)
        except Exception as exc:  # noqa: BLE001 - the watchdog must not take the supervisor down
            self.state.log_error("evolution", f"canary check failed: {exc!r}")

    def start(self) -> None:
        if not self.lock.acquire():
            raise RuntimeError(f"another agent instance holds {self.lock.path}")
        self.state.set("supervisor", {"pid": os.getpid(), "started_at": self.state.now(),
                                      "workers": [w.name for w in self.workers]})
        hot_reload.RELOAD.clear()
        if getattr(self, "webhook_server", None) is not None:
            # Created here (not per worker start) so it survives worker restarts and reloads.
            self.listen_socket = hot_reload.listening_socket(self.webhook_server.host, self.webhook_server.port)
            self.webhook_server.sock = self.listen_socket
        self.install_signal_handlers()
        for w in self.workers:
            self._start(w)

    def run(self) -> str:
        """Start everything, supervise until stopped, shut down gracefully. Returns the stop reason."""
        self.start()
        try:
            while not self.stop_event.is_set():
                if hot_reload.RELOAD.is_set():
                    self.request_reload(hot_reload.reload_reason())
                    break
                for w in self.workers:
                    self._check(w)
                self._tick_canary()
                self.stop_event.wait(self.poll_seconds)
        finally:
            self.shutdown()
        if self.reloading:
            hot_reload.RELOAD.clear()
            self.state.log_action(int(self.state.get("iteration", 0)), None, "supervisor:reload", "ok", self.stop_reason)
            hot_reload.reexec(self.listen_socket, self.exec_fn or os.execve)  # doesn't return (unless a test fakes it)
        elif self.listen_socket is not None:
            self.listen_socket.close()
        return self.stop_reason

    def shutdown(self) -> None:
        self.stop_event.set()
        self.engine.request_stop()
        deadline = self.monotonic() + self.config.shutdown_timeout_seconds
        for w in self.workers:
            if w.thread is not None:
                w.thread.join(timeout=max(0.1, deadline - self.monotonic()))
        stuck = [w.name for w in self.workers if w.thread is not None and w.thread.is_alive()]
        checkpoint = {
            "at": self.state.now(),
            "reason": self.stop_reason or "stopped",
            "iteration": int(self.state.get("iteration", 0)),
            "workers": {w.name: {"starts": w.starts, "crashes": len(w.crashes), "disabled": w.disabled} for w in self.workers},
            "unfinished": stuck,
        }
        try:
            self.state.set("last_shutdown", checkpoint)
            self.state.set("supervisor", None)
            self.state.set("pid", None)
            self.state.checkpoint()  # flush WAL into the main db file
        finally:
            self.engine.power.release_all()  # never leave the Mac pinned awake
            self.lock.release()
            self.restore_signal_handlers()
        log.info("supervisor stopped: %s", checkpoint["reason"])
