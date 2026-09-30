"""Start / pause / kill the agent from the GUI, and report what's running.

The GUI is a separate process from the agent. They share the SQLite state (WAL, lock retries),
which carries the pause and stop flags the engine checks every cycle, and the supervisor's
PID. The process itself is managed through launchd when the job is installed (so KeepAlive and
logs behave as usual), otherwise as a detached ``automonetize supervise --headless`` child.

* **Start**: clears a pause, a kill-switch stop or a quarantine, then makes sure a supervisor
  is running (``launchctl bootstrap`` + ``kickstart``, or spawn).
* **Pause / Sleep**: the engine skips its cycles and holds no power assertion, so the Mac can
  sleep. The webhook listener keeps fulfilling orders and capturing leads.
* **Kill switch**: engages the emergency stop (survives restarts) and stops the supervisor.
  Under launchd the job is booted out, otherwise KeepAlive would bring it straight back.
"""

from __future__ import annotations

import json
import os
import platform
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from agent.config import Config
from agent.state import StateStore
from agent.tunnel import read_env_file

LABEL = "com.automonetize.agent"


def probe_webhook(config: Config, timeout: float = 1.5) -> dict[str, Any]:
    """GET the local listener's /healthz (never through the tunnel)."""
    url = f"http://127.0.0.1:{config.webhook_port}/healthz"
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback: never via a proxy
        with opener.open(url, timeout=timeout) as resp:  # noqa: S310 - fixed loopback URL
            return {"healthy": resp.status == 200, "events": json.loads(resp.read() or b"{}").get("events", {})}
    except (urllib.error.URLError, OSError, ValueError):
        return {"healthy": False, "events": {}}


def engine_state(state: StateStore, config: Config) -> tuple[str, str]:
    """(running | paused | stopped | quarantined | offline | tripped, reason) from the shared flags."""
    if config.stop_file.exists():
        return "stopped", "kill switch / stop file"
    stop = state.get("emergency_stop")
    if stop:
        return "stopped", stop.get("reason", "")
    paused = state.get("paused")
    if paused:
        return "paused", paused.get("reason", "")
    q = state.get("quarantine")
    if q and q.get("active"):
        return "quarantined", f"until {q.get('until')}: {q.get('reason', '')}"
    if (state.get("breaker") or {}).get("tripped"):
        return "tripped", (state.get("breaker") or {}).get("trip_reason", "")
    if state.get("offline_since"):
        return "offline", f"since {state.get('offline_since')}"
    return "running", ""


class ServiceController:
    def __init__(self, config: Config, state: StateStore, workdir: Path, env_file: Path,
                 runner: Callable[..., Any] = subprocess.run, spawner: Callable[..., Any] = subprocess.Popen,
                 killer: Callable[[int, int], None] = os.kill, system: str | None = None, home: Path | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self.config = config
        self.state = state
        self.workdir = workdir
        self.env_file = env_file
        self.runner = runner
        self.spawner = spawner
        self.killer = killer
        self.system = system or platform.system()
        self.home = home or Path.home()
        self.sleep = sleep
        self.clock = clock

    # -- inspection -------------------------------------------------------------------
    @property
    def domain(self) -> str:
        return f"gui/{os.getuid()}"

    @property
    def plist(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{LABEL}.plist"

    def launchd_managed(self) -> bool:
        return self.system == "Darwin" and self.plist.exists()

    def _alive(self, pid: int) -> bool:
        if pid <= 0 or pid == os.getpid():
            return False
        try:
            self.killer(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists, owned by someone else

    def supervisor_pid(self) -> int | None:
        rec = self.state.get("supervisor") or {}
        pid = int(rec.get("pid") or self.state.get("pid") or 0)
        return pid if self._alive(pid) else None

    def status(self) -> dict[str, Any]:
        pid = self.supervisor_pid()
        rec = self.state.get("supervisor") or {}
        mode = "launchd" if self.launchd_managed() else ("process" if pid else "none")
        es, reason = engine_state(self.state, self.config)
        return {"running": pid is not None, "pid": pid, "since": rec.get("started_at") if pid else None,
                "workers": rec.get("workers", []) if pid else [], "mode": mode,
                "engine": es if pid else ("stopped" if es == "stopped" else "not running"), "engine_flag": es, "reason": reason}

    # -- flags (the engine reads these every cycle) --------------------------------------
    def _engine(self):
        from agent.engine import Engine
        from agent.power import NullBackend, PowerManager

        return Engine(self.config, state=self.state, strategies=[], online_check=lambda: True,
                      power=PowerManager(NullBackend()))

    def pause(self, reason: str = "paused from the control panel") -> dict[str, Any]:
        self._engine().pause(reason)
        return {"ok": True, "message": "Engine paused. The webhook and lead capture stay up; the Mac may sleep."}

    def unpause(self) -> dict[str, Any]:
        self._engine().unpause()
        return {"ok": True, "message": "Engine resumed: the next cycle runs on schedule."}

    # -- process ----------------------------------------------------------------------------
    def _launchctl(self, *args: str) -> subprocess.CompletedProcess:
        return self.runner(["launchctl", *args], capture_output=True, text=True, check=False)

    def start(self) -> dict[str, Any]:
        self._engine().resume()  # clears pause, kill-switch stop, quarantine and a tripped breaker
        if self.supervisor_pid():
            return {"ok": True, "message": "Engine running (flags cleared)."}
        return self._launch()

    def _launch(self) -> dict[str, Any]:
        if self.launchd_managed():
            self._launchctl("bootstrap", self.domain, str(self.plist))  # fails harmlessly if already loaded
            proc = self._launchctl("kickstart", f"{self.domain}/{LABEL}")
            if proc.returncode != 0:
                return {"ok": False, "message": f"launchctl kickstart failed: {(proc.stderr or proc.stdout).strip()[:300]}"}
            return {"ok": True, "message": "Started through launchd."}
        env = {**os.environ, **read_env_file(self.env_file), "AUTOMONETIZE_ENV_FILE": str(self.env_file)}
        log = open(self.config.data_dir / "supervisor.log", "ab")  # noqa: SIM115 - handed to the child
        try:
            self.spawner([sys.executable, "-m", "dashboard.cli", "supervise", "--headless"], cwd=str(self.workdir), env=env,
                         stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
        finally:
            log.close()
        return {"ok": True, "message": "Started a background supervisor (logs: data/supervisor.log)."}

    def kill(self) -> dict[str, Any]:
        self._engine().emergency_stop("kill switch (control panel)")
        pid = self.supervisor_pid()
        steps = ["emergency stop engaged"]
        if self.launchd_managed():
            proc = self._launchctl("bootout", f"{self.domain}/{LABEL}")
            steps.append("launchd job booted out" if proc.returncode == 0 else "launchd job was not loaded")
        if pid:
            try:
                self.killer(pid, signal.SIGTERM)
                steps.append(f"SIGTERM sent to supervisor {pid}")
            except ProcessLookupError:
                pass
        return {"ok": True, "message": "; ".join(steps) + ". Press Start to bring it back."}

    def restart(self) -> dict[str, Any]:
        if self.launchd_managed():
            proc = self._launchctl("kickstart", "-k", f"{self.domain}/{LABEL}")
            return {"ok": proc.returncode == 0, "message": "Restarted through launchd (new settings loaded)."}
        pid = self.supervisor_pid()
        if pid:
            self.killer(pid, signal.SIGTERM)
            deadline = self.clock() + self.config.shutdown_timeout_seconds + 10
            while self._alive(pid) and self.clock() < deadline:
                self.sleep(0.5)
            if self._alive(pid):
                return {"ok": False, "message": f"supervisor {pid} didn't exit; try again or use the kill switch"}
        result = self._launch()  # keeps pause/stop flags as they are: a restart only reloads settings
        return {**result, "message": "Restarted: new settings loaded. " + result["message"]}

    def act(self, action: str) -> dict[str, Any]:
        handlers = {"start": self.start, "pause": self.pause, "resume": self.unpause, "kill": self.kill,
                    "restart": self.restart}
        if action not in handlers:
            return {"ok": False, "message": f"unknown action {action!r}"}
        result = handlers[action]()
        self.state.log_action(int(self.state.get("iteration", 0)), None, f"gui:{action}", "ok" if result["ok"] else "failed",
                              result["message"][:300])
        return result
