"""Autonomous Plan-and-Solve loop with ReAct-style reflection and strategy pivoting.

Each cycle:

1. **Guard**: refuse to run while a manual emergency stop is engaged. An *automatic* breaker trip
   quarantines the engine instead (``agent.recovery.Quarantine``): an alert, a cooldown, then a
   self-diagnostic and, if it passes, a clean cycle. The loop never halts permanently on its own.
2. **Observe**: load (or formulate) the active hypothesis and its verified revenue.
3. **Reflect**: deprecate the hypothesis if it has had ``pivot_after_iterations`` cycles without
   a cent of verified revenue, and formulate an alternative.
4. **Plan**: if the backlog is empty, queue the pipeline for the hypothesis.
5. **Act**: execute backlog tasks within the circuit breaker's per-cycle budget. Each task gets
   up to ``task_retry_attempts`` tries with adjusted parameters before it is recorded as an
   operational failure. A result that disproves the hypothesis triggers an immediate pivot.
"""

from __future__ import annotations

import fcntl
import logging
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

from agent.config import Config
from agent.hypotheses import formulate_next, pivot_reason, score_hypothesis
from agent.power import PowerManager
from agent.recovery import Quarantine, self_diagnostic
from agent.state import StateStore
from strategies import default_strategies
from strategies.base import Strategy, TaskContext, TaskResult
from tools import Toolkit, build_toolkit
from tools.circuit_breaker import CircuitBreaker
from tools.errors import CircuitOpenError
from tools.recovery import retry_with_adjustment

log = logging.getLogger("automonetize.engine")

# (task, priority) — lower priority runs first. Tasks without a registered handler are left out.
PLAN: list[tuple[str, int]] = [
    ("aggregate_leads", 10),
    ("discover_sources", 12),
    ("build_intel", 15),
    ("package_asset", 20),
    ("publish_listing", 25),
    ("publish_showcase", 30),
    ("tune_copy", 29),
    ("syndicate", 31),
    ("build_site", 32),
    ("track_hn", 33),
    ("publish_subscription", 34),
    ("publish_api_tier", 36),
    ("publish_dossier_tier", 37),
    ("stage_outreach", 35),
    ("dispatch_outreach", 40),
    ("sync_revenue", 45),
    ("sync_subscriptions", 46),
    ("run_dunning", 47),
    ("deliver_orders", 50),
    ("deliver_subscriptions", 51),
    ("nurture_leads", 52),
    ("collect_metrics", 55),
    ("run_satellites", 58),
    ("optimize_pricing", 60),
    ("sync_refunds", 46),
    ("sync_finance", 48),
    ("answer_support", 49),
    ("publish_bundle", 59),
    ("follow_up_buyers", 53),
    ("refresh_share_kit", 57),
    ("monthly_books", 96),
    ("backup_data", 97),
    ("report_owner", 98),
    ("evolve_code", 99),  # last: a merge reloads the process once the cycle is over
]
BUILTIN_TASKS = {"sync_revenue"}


class NetworkDown(Exception):
    """Raised when a task fails and the connectivity probe confirms the network is gone."""


@dataclass
class CycleReport:
    cycle: int
    status: str  # ran | stopped | paused | quarantined | offline | idle | crashed
    hypothesis_id: int | None = None
    hypothesis_key: str = ""
    actions: list[dict[str, Any]] = field(default_factory=list)
    pivots: list[str] = field(default_factory=list)
    message: str = ""


class Engine:
    def __init__(
        self,
        config: Config,
        state: StateStore | None = None,
        strategies: Iterable[Strategy] | None = None,
        toolkit: Toolkit | None = None,
        transport=None,
        sleep: Callable[[float], None] = time.sleep,
        online_check: Callable[[], bool] | None = None,
        power: PowerManager | None = None,
        wall_clock: Callable[[], float] = time.time,
    ):
        config.ensure_dirs()
        self.config = config
        self.state = state or StateStore(config.db_path)
        if toolkit is not None:
            self.breaker = toolkit.breaker
            self.tools = toolkit
        else:
            self.breaker = CircuitBreaker(
                # An old automonetize.toml may pin a cap below today's plan: never let that cut every
                # cycle short as new tasks are added.
                max_actions_per_cycle=max(config.max_actions_per_cycle, len(PLAN) + 10),
                max_api_calls_per_cycle=config.max_api_calls_per_cycle,
                max_consecutive_errors=config.max_consecutive_errors,
            )
            self.tools = build_toolkit(config, self.state, self.breaker, transport=transport, sleep=sleep)
        self._sleep = sleep
        self._load_breaker()
        self.handlers: dict[str, Strategy] = {}
        for strategy in strategies if strategies is not None else default_strategies():
            for task in strategy.tasks:
                self.handlers[task] = strategy
        if strategies is None:
            # Self-evolution is registered here, not in strategies/, which evolution may edit.
            from agent.evolution.task import EvolutionStrategy

            self.handlers["evolve_code"] = EvolutionStrategy()
            from agent.backup import Backups

            self.handlers["backup_data"] = Backups()
        self._stop_event = threading.Event()
        if online_check is None:
            from agent.connectivity import is_online

            online_check = lambda: is_online(config.network_check_hosts)  # noqa: E731
        self.online_check = online_check
        self.quarantine = Quarantine(self.state, config)
        if power is None:
            from agent import power as power_mod

            power = power_mod.shared(config)
        self.power = power
        self._wall = wall_clock

    # ------------------------------------------------------------------ emergency stop
    def _load_breaker(self) -> None:
        saved = self.state.get("breaker", {})
        self.breaker.consecutive_errors = int(saved.get("consecutive_errors", 0))
        self.breaker.total_failures = int(saved.get("total_failures", 0))
        if saved.get("tripped"):
            self.breaker.trip(saved.get("trip_reason", "restored from state"))

    def _save_breaker(self) -> None:
        self.state.set("breaker", self.breaker.health())

    def is_stopped(self) -> tuple[bool, str]:
        if self.config.stop_file.exists():
            return True, f"stop file present: {self.config.stop_file}"
        stop = self.state.get("emergency_stop")
        if stop:
            return True, f"emergency stop: {stop.get('reason', '')}"
        paused = self.state.get("paused")
        if paused:
            return True, f"paused: {paused.get('reason', '')} (since {paused.get('at', '?')})"
        if self.quarantine.active():
            rec = self.quarantine.record or {}
            return True, f"quarantined until {rec.get('until')}: {rec.get('reason', '')}"
        if self.breaker.tripped:
            return True, f"circuit breaker tripped: {self.breaker.trip_reason}"
        return False, ""

    def on_breaker_trip(self, reason: str) -> None:
        """Systemic failure: quarantine and self-heal, or (quarantine disabled) stop for a human."""
        self._save_breaker()
        if self.config.quarantine_hours <= 0:
            self.emergency_stop(reason)
            return
        until = self.quarantine.enter(reason)
        self.state.log_error("engine", f"QUARANTINE until {until.isoformat(timespec='seconds')}: {reason}", kind="circuit")
        log.error("quarantined until %s: %s", until, reason)

    def try_leave_quarantine(self, cycle: int) -> tuple[bool, str]:
        """Called when the cooldown is over. Returns (resumed, message)."""
        diag = self_diagnostic(self.state, self.config, self.online_check)
        if diag["passed"]:
            self.breaker.reset()
            self._save_breaker()
            self.quarantine.release(diag)
            self.state.log_action(cycle, None, "quarantine", "ok", f"self-diagnostic passed: {diag}")
            return True, "self-diagnostic passed"
        failed = [k for k in ("db_integrity", "db_write", "disk_ok", "network") if diag.get(k) is not True]
        if failed == ["network"]:
            # Just offline: not a reason to add hours. Try again next cycle.
            self.state.log_action(cycle, None, "quarantine", "skipped", "cooldown over, waiting for network")
            return False, "cooldown over; waiting for network"
        until = self.quarantine.extend(f"self-diagnostic failed: {', '.join(failed)} ({diag})")
        self.state.log_action(cycle, None, "quarantine", "failed", f"diagnostic failed ({', '.join(failed)}); extended")
        return False, f"self-diagnostic failed ({', '.join(failed)}); quarantined until {until.isoformat(timespec='seconds')}"

    def emergency_stop(self, reason: str) -> None:
        self.state.set("emergency_stop", {"reason": reason, "at": self.state.now()})
        self.config.stop_file.parent.mkdir(parents=True, exist_ok=True)
        self.config.stop_file.write_text(reason + "\n")
        self.state.log_error("engine", f"EMERGENCY STOP: {reason}", kind="circuit")
        self._save_breaker()
        log.error("emergency stop engaged: %s", reason)

    def pause(self, reason: str = "paused by operator") -> None:
        """Skip cycles until ``unpause``/``resume``: nothing held awake, webhook and lead capture stay up."""
        self.state.set("paused", {"reason": reason, "at": self.state.now()})
        self.state.log_action(self.current_cycle(), None, "pause", "ok", reason)

    def unpause(self) -> None:
        if self.state.get("paused"):
            self.state.set("paused", None)
            self.state.log_action(self.current_cycle(), None, "pause", "ok", "resumed")

    def resume(self) -> None:
        self.state.set("paused", None)
        if self.quarantine.active():
            self.quarantine.release({"manual": True})
        self.state.set("emergency_stop", None)
        if self.config.stop_file.exists():
            self.config.stop_file.unlink()
        self.breaker.reset()
        self._save_breaker()

    # ------------------------------------------------------------------ hypotheses
    def ensure_hypothesis(self) -> dict[str, Any] | None:
        hyp = self.state.active_hypothesis()
        if hyp is not None:
            return hyp
        proposal = formulate_next(self.state, self.config)
        if proposal is None:
            return None
        hid = self.state.create_hypothesis(proposal["key"], proposal["strategy"], proposal["description"], proposal["params"])
        self.state.log_action(self.current_cycle(), hid, "formulate_hypothesis", "ok", proposal["description"])
        log.info("new hypothesis #%s: %s", hid, proposal["description"])
        return self.state.get_hypothesis(hid)

    def pivot(self, hyp: dict[str, Any], reason: str) -> None:
        self.state.set_hypothesis_status(hyp["id"], "deprecated", reason)
        cancelled = self.state.cancel_tasks(hyp["id"])
        self.state.log_action(
            self.current_cycle(), hyp["id"], "deprecate_hypothesis", "ok", f"{reason} (cancelled {cancelled} tasks)"
        )
        log.warning("deprecated hypothesis #%s: %s", hyp["id"], reason)

    def planned_tasks(self) -> list[tuple[str, int]]:
        return [(t, p) for t, p in PLAN if t in BUILTIN_TASKS or t in self.handlers]

    # ------------------------------------------------------------------ cycle
    def current_cycle(self) -> int:
        return int(self.state.get("iteration", 0))

    def run_cycle(self) -> CycleReport:
        # Keep the Mac awake for the whole cycle; it may sleep again once the loop goes idle.
        with self.power.hold("engine cycle"):
            return self._run_cycle()

    def _run_cycle(self) -> CycleReport:
        cycle = self.state.incr("iteration")
        self.state.set("last_cycle_at", self.state.now())
        if self.state.get("started_at") is None:
            self.state.set("started_at", self.state.now())

        if (self.quarantine.due() and not self.config.stop_file.exists() and not self.state.get("emergency_stop")
                and not self.state.get("paused")):
            resumed, msg = self.try_leave_quarantine(cycle)
            if not resumed:
                return CycleReport(cycle, "quarantined", message=msg)
        stopped, why = self.is_stopped()
        if stopped:
            self.state.log_action(cycle, None, "cycle", "skipped", why)
            manual = self.config.stop_file.exists() or bool(self.state.get("emergency_stop"))
            if self.state.get("paused") and not manual:
                return CycleReport(cycle, "paused", message=why)
            return CycleReport(cycle, "quarantined" if self.quarantine.active() and not manual else "stopped", message=why)

        if not self.online_check():
            # Network down (wifi drop, laptop asleep, proxy gone): wait it out without burning
            # retries or counting failures toward the emergency stop.
            if not self.state.get("offline_since"):
                self.state.set("offline_since", self.state.now())
            self.state.log_action(cycle, None, "cycle", "skipped", "offline: waiting for network")
            return CycleReport(cycle, "offline", message="network unreachable")
        if self.state.get("offline_since"):
            self.state.log_action(cycle, None, "network", "ok", f"back online (offline since {self.state.get('offline_since')})")
            self.state.set("offline_since", None)

        self.breaker.begin_cycle()
        report = CycleReport(cycle, "ran")

        hyp = self.ensure_hypothesis()
        reason = pivot_reason(self.state, self.config, hyp) if hyp is not None else None
        if hyp is not None and reason:
            self.pivot(hyp, reason)
            report.pivots.append(f"{hyp['key']}: {reason}")
            hyp = self.ensure_hypothesis()
        if hyp is None:
            report.status = "idle"
            report.message = "hypothesis space exhausted; add niches to the config"
            self.state.log_action(cycle, None, "cycle", "skipped", report.message)
            return report
        self.state.set("objective", hyp["description"])

        report.hypothesis_id, report.hypothesis_key = hyp["id"], hyp["key"]
        if not self.state.pending_tasks(hyp["id"]):
            for task, priority in self.planned_tasks():
                self.state.add_task(hyp["id"], task, {}, priority)

        while self.breaker.allow_action():
            task = self.state.next_task(hyp["id"])
            if task is None:
                break
            outcome = self._execute(cycle, hyp, task)
            report.actions.append(outcome)
            if outcome.get("invalidates_hypothesis"):
                reason = f"{task['task']} disproved it: {outcome['summary']}"
                self.pivot(hyp, reason)
                report.pivots.append(f"{hyp['key']}: {reason}")
                break
            if outcome["status"] == "offline":
                report.status, report.message = "offline", "network lost mid-cycle; paused until it returns"
                break
            if outcome["status"] in ("circuit_open",) or self.breaker.tripped:
                break

        if self.breaker.tripped:
            self.on_breaker_trip(self.breaker.trip_reason)
            report.message = f"{'quarantined' if self.quarantine.active() else 'emergency stop'}: {self.breaker.trip_reason}"
        current = self.state.get_hypothesis(hyp["id"])
        # An aborted offline cycle isn't evidence about the niche: don't spend its pivot budget.
        if current["status"] == "active" and report.status != "offline":
            self.state.increment_hypothesis_iterations(hyp["id"])
            self.state.set(f"score:{hyp['id']}", score_hypothesis(self.state, self.state.get_hypothesis(hyp["id"])))
        self._save_breaker()
        return report

    def _dispatch(self, task: str, ctx: TaskContext) -> TaskResult:
        if task == "sync_revenue":
            return self._sync_revenue()
        strategy = self.handlers.get(task)
        if strategy is None:
            raise LookupError(f"no strategy handles task {task!r}")
        return strategy.run(task, ctx)

    def _sync_revenue(self) -> TaskResult:
        """Poll every configured storefront for orders (Stripe, Lemon Squeezy, Gumroad)."""
        revenue = self.tools.revenue
        reports = revenue.sync_storefronts(self.tools.storefronts)
        if revenue.gumroad_enabled():
            reports.append(revenue.sync_gumroad())
        today = revenue.daily_summary()
        if not reports:
            return TaskResult(True, f"no storefront credentials; today ${today['net_cents'] / 100:.2f} verified")
        parts = [f"{r.source}: {r.new} new (+${r.net_cents_added / 100:.2f})" for r in reports]
        return TaskResult(
            True,
            "; ".join(parts) + f" · today ${today['net_cents'] / 100:.2f}/${today['target_cents'] / 100:.2f}",
            metrics={"new_sales": sum(r.new for r in reports), "net_cents_added": sum(r.net_cents_added for r in reports)},
        )

    def _execute(self, cycle: int, hyp: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        name = task["task"]
        started = time.monotonic()
        attempts = {"n": 0}
        max_attempts = self.config.task_retry_attempts

        def attempt(payload: dict[str, Any]) -> TaskResult:
            attempts["n"] += 1
            return self._dispatch(name, TaskContext(self.tools, hyp, payload, attempt=attempts["n"]))

        def on_error(n: int, exc: BaseException, tb: str) -> None:
            # A failure might just be the network dropping mid-cycle. Re-probe before retrying: if
            # we're offline, retries are pointless and the failure isn't the task's fault.
            if not self.online_check():
                raise NetworkDown(f"network lost during {name}: {exc!r}") from exc
            self.state.log_error(f"task:{name}", f"attempt {n}/{max_attempts} failed: {exc!r}", tb)

        def adjust(n: int, exc: BaseException, payload: dict[str, Any]) -> dict[str, Any]:
            payload["retry"] = n
            payload["last_error"] = repr(exc)[:300]
            return payload

        outcome: dict[str, Any] = {"task": name, "task_id": task["id"]}
        try:
            result = retry_with_adjustment(
                attempt, dict(task["payload"]), attempts=max_attempts, adjust=adjust, on_error=on_error,
                sleep=self._sleep, label=f"task {name}",
            )
        except NetworkDown as exc:
            self.state.finish_task(task["id"], "pending", str(exc), 0)
            if not self.state.get("offline_since"):
                self.state.set("offline_since", self.state.now())
            self.state.log_action(cycle, hyp["id"], name, "skipped", f"offline: {exc}", time.monotonic() - started)
            outcome.update(status="offline", summary=str(exc))
            return outcome
        except CircuitOpenError as exc:
            # Budget exhausted: not the task's fault. Leave it queued for the next cycle.
            self.state.finish_task(task["id"], "pending", str(exc), attempts["n"])
            self.state.log_error(f"task:{name}", str(exc), kind="circuit")
            self.state.log_action(cycle, hyp["id"], name, "skipped", f"circuit open: {exc}", time.monotonic() - started)
            outcome.update(status="circuit_open", summary=str(exc))
            return outcome
        except Exception as exc:  # OperationalFailure or a non-retryable policy violation
            detail = str(exc)
            self.state.finish_task(task["id"], "failed", detail, attempts["n"])
            self.state.log_error(f"task:{name}", detail, kind="operational_failure")
            self.state.log_action(cycle, hyp["id"], name, "failed", detail, time.monotonic() - started)
            tripped = self.breaker.record_failure(f"{name}: {exc}")
            outcome.update(status="failed", summary=detail, tripped=tripped)
            return outcome

        self.breaker.record_success()
        status = "ok" if result.ok else "failed"
        self.state.finish_task(task["id"], "done" if result.ok else "failed", result.summary, attempts["n"])
        self.state.log_action(cycle, hyp["id"], name, status, result.summary, time.monotonic() - started)
        outcome.update(
            status=status, summary=result.summary, metrics=result.metrics,
            invalidates_hypothesis=result.invalidates_hypothesis, attempts=attempts["n"],
        )
        return outcome

    # ------------------------------------------------------------------ perpetual loop
    def request_stop(self, *_: Any) -> None:
        self._stop_event.set()

    def run_forever(
        self,
        interval: float | None = None,
        max_cycles: int | None = None,
        on_cycle: Callable[[CycleReport], None] | None = None,
        install_signal_handlers: bool = True,
    ) -> int:
        """Run cycles every ``interval`` seconds until stopped. Returns the number of cycles run."""
        interval = self.config.interval_seconds if interval is None else interval
        if install_signal_handlers and threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, self.request_stop)
            signal.signal(signal.SIGINT, self.request_stop)
        self.state.set("started_at", self.state.now())
        self.state.set("stopped_at", None)
        self.state.set("pid", os.getpid())
        ran = 0
        while not self._stop_event.is_set():
            try:
                report = self.run_cycle()
            except Exception as exc:  # noqa: BLE001 - the loop itself must survive
                import traceback

                self.state.log_error("engine", f"cycle crashed: {exc!r}", traceback.format_exc())
                if self.breaker.record_failure(f"cycle crash: {exc!r}"):
                    self.on_breaker_trip(self.breaker.trip_reason)
                self._save_breaker()
                report = CycleReport(self.current_cycle(), "crashed", message=repr(exc))
            self._canary(report.cycle if report.status != "crashed" else None)
            if on_cycle is not None:
                on_cycle(report)
            ran += 1
            if max_cycles is not None and ran >= max_cycles:
                break
            self.idle_wait(interval)
        self.state.set("pid", None)
        self.state.set("stopped_at", self.state.now())
        return ran


    def _canary(self, cycle_done: int | None) -> None:
        """After an evolution merge, watch the next cycle(s); roll back on a regression."""
        try:
            from agent.evolution.hot_reload import canary_tick

            canary_tick(self.state, self.config, cycle_done=cycle_done)
        except Exception as exc:  # noqa: BLE001 - never let the watchdog take the loop down
            self.state.log_error("evolution", f"canary check failed: {exc!r}")

    def idle_wait(self, interval: float, slice_seconds: float = 30.0) -> None:
        """Wait ``interval`` seconds of *wall-clock* time between cycles, holding no power assertion.

        ``Event.wait`` runs on the monotonic clock, which stops while a Mac sleeps: a plain
        one-hour wait that started before a 3-hour sleep would still have most of an hour left on
        wake. Waiting in short slices against the wall clock runs the overdue cycle within
        ``slice_seconds`` of waking. It also asks pmset to wake the Mac for the next cycle."""
        deadline = self._wall() + interval
        try:
            self.power.schedule_wake(self.state.clock() + timedelta(seconds=interval), now=self.state.clock())
        except Exception as exc:  # noqa: BLE001 - wake scheduling is best effort
            log.debug("schedule_wake failed: %r", exc)
        while not self._stop_event.is_set():
            remaining = deadline - self._wall()
            if remaining <= 0:
                return
            self._stop_event.wait(min(remaining, slice_seconds))


class ProcessLock:
    """Prevents two agent loops from sharing one state database."""

    def __init__(self, path: Path):
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "ProcessLock":
        if not self.acquire():
            raise RuntimeError(f"another agent instance holds {self.path}")
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


def uptime_seconds(state: StateStore) -> float:
    """Seconds since the loop started; frozen at the stop time once the loop has exited."""
    started = state.get("started_at")
    if not started:
        return 0.0
    stopped = state.get("stopped_at")
    end = datetime.fromisoformat(stopped) if stopped else state.clock()
    return max(0.0, (end - datetime.fromisoformat(started)).total_seconds())
