"""Graceful hot reload, and the post-merge canary with automatic rollback.

**Reload.** Python can't safely swap already-imported modules under running threads, so a reload
replaces the whole interpreter image while keeping the process and its listening socket:

1. ``request_reload()`` sets ``RELOAD``. The supervisor notices within its poll interval (it also
   reloads on ``SIGUSR1``; ``SIGHUP``, ``SIGTERM`` and ``SIGINT`` still mean a graceful stop).
2. It drains: the engine finishes its cycle, the webhook listener stops accepting and waits for
   in-flight fulfilment (``on_shutdown``), the WAL is checkpointed, the process lock released.
3. It re-executes itself (``os.execve``, same PID, same arguments). The webhook's listening socket
   is **inherited** across the exec (``AUTOMONETIZE_LISTEN_FD``), so the port never closes:
   connections arriving during the switch wait in the kernel's backlog and are answered by the
   new code. The Cloudflare tunnel (a separate ``cloudflared`` process) is never touched, and
   launchd sees no exit.

**Canary.** After a merge, ``start_canary`` records the commit, the files it changed, the error
high-water mark and the tasks that were already failing. For ``evolution_canary_minutes`` (60),
and until at least one engine cycle has completed on the new code, ``check_canary`` (called after
every engine cycle and every 30 s by the supervisor) looks for:

* an engine cycle crash, or a supervised worker crash;
* an operational failure of a task that wasn't already failing before the merge;
* any error whose traceback runs through a file the evolution changed.

Any of these → ``rollback``: ``git revert`` of that commit (as the agent, ``[Auto-Evolution]
Rollback: …``), blacklist the patch, 24 h cooldown, reload. If the revert can't be applied
cleanly (someone changed the checkout since), evolution halts itself and raises an alert for a
human, and no further evolution happens until ``automonetize evolution resume``.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("automonetize.evolution")

RELOAD = threading.Event()
LISTEN_FD_ENV = "AUTOMONETIZE_LISTEN_FD"
_reason: list[str] = []
_canary_lock = threading.Lock()


# ----------------------------------------------------------------------------- reload
def request_reload(reason: str) -> None:
    _reason[:] = [reason]
    RELOAD.set()
    log.warning("reload requested: %s", reason)


def reload_reason() -> str:
    return _reason[0] if _reason else "reload requested"


def listening_socket(host: str, port: int, env: dict[str, str] | None = None) -> socket.socket:
    """The webhook's listening socket: inherited from the previous image after a reload, else new."""
    env = os.environ if env is None else env
    raw = env.pop(LISTEN_FD_ENV, None)
    if raw and raw.isdigit():
        try:
            sock = socket.socket(fileno=int(raw))
            if sock.type == socket.SOCK_STREAM and (port == 0 or sock.getsockname()[1] == port):
                os.set_inheritable(sock.fileno(), False)
                return sock
            sock.close()
        except OSError:
            pass
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    return socket.create_server((host, port), family=family, backlog=128)


def reexec(sock: socket.socket | None, exec_fn: Callable[..., Any] = os.execve) -> Any:
    """Replace this process with a fresh interpreter running the same command line."""
    env = dict(os.environ)
    if sock is not None:
        os.set_inheritable(sock.fileno(), True)
        env[LISTEN_FD_ENV] = str(sock.fileno())
    argv = [sys.executable, *getattr(sys, "orig_argv", [sys.executable, *sys.argv])[1:]]
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError):
            pass
    log.warning("re-executing %s", " ".join(argv))
    return exec_fn(sys.executable, argv, env)


# ----------------------------------------------------------------------------- canary
def repo_root(config: Any) -> Path:
    return Path(config.evolution_repo).resolve() if getattr(config, "evolution_repo", "") else Path(__file__).resolve().parents[2]


def evolution_log(config: Any, clock: Callable[[], datetime] | None = None):
    from agent.evolution.log import EvolutionLog

    return EvolutionLog(config.data_dir / "evolution_log.db", **({"clock": clock} if clock else {}))


def start_canary(elog: Any, state: Any, config: Any, attempt_id: int, sha: str, files: list[str], title: str) -> dict[str, Any]:
    now = state.clock()
    last_error = state._one("SELECT COALESCE(MAX(id), 0) AS n FROM errors")["n"]
    day_ago = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    failing = sorted({r["source"] for r in state._all("SELECT DISTINCT source FROM errors WHERE kind = 'operational_failure' "
                                                        "AND created_at >= ?", (day_ago,))})
    canary = {"status": "monitoring", "attempt_id": attempt_id, "commit_sha": sha, "title": title, "files": files,
              "merged_at": now.isoformat(timespec="seconds"),
              "until": (now + timedelta(minutes=int(config.evolution_canary_minutes))).isoformat(timespec="seconds"),
              "iteration": int(state.get("iteration", 0)), "error_id": int(last_error), "failing_before": failing}
    elog.set_kv("canary", canary)
    return canary


def canary_failure(state: Any, canary: dict[str, Any]) -> str | None:
    rows = state._all("SELECT id, source, kind, message, traceback FROM errors WHERE id > ? ORDER BY id", (canary["error_id"],))
    for e in rows:
        if e["source"] == "engine" and e["message"].startswith("cycle crashed"):
            return f"engine cycle crashed: {e['message'][:200]}"
        if e["source"].startswith("supervisor:") and "crashed" in e["message"]:
            return f"{e['source']} {e['message'][:200]}"
        if e["kind"] == "operational_failure" and e["source"] not in canary["failing_before"]:
            return f"new operational failure in {e['source']}: {e['message'][:200]}"
        if any(f in (e["traceback"] or "") for f in canary["files"]):
            return f"exception in evolved code ({e['source']}): {e['message'][:200]}"
    return None


def check_canary(elog: Any, state: Any, cycle_done: int | None = None) -> tuple[str, str]:
    """("none" | "monitoring" | "passed" | "failed", reason). ``cycle_done``: the engine just
    finished that cycle (the canary passes only after a whole cycle ran on the new code)."""
    canary = elog.get_kv("canary")
    if not canary or canary.get("status") != "monitoring":
        return (canary or {}).get("status", "none"), (canary or {}).get("reason", "")
    failure = canary_failure(state, canary)
    if failure:
        return "failed", failure
    if cycle_done is not None and cycle_done > int(canary["iteration"]):
        canary["cycles"] = int(canary.get("cycles", 0)) + 1
        elog.set_kv("canary", canary)
    now = state.clock()
    ran = int(canary.get("cycles", 0)) >= 1
    if now >= datetime.fromisoformat(canary["until"]) and ran:
        canary.update(status="passed", reason=f"no regressions in {canary['until'][11:16]} window")
        elog.set_kv("canary", canary)
        state.log_action(int(state.get("iteration", 0)), None, "evolution:canary", "ok",
                         f"{canary['commit_sha'][:12]} healthy after the canary window")
        return "passed", canary["reason"]
    return "monitoring", f"watching {canary['commit_sha'][:12]} until {canary['until']}"


def rollback(elog: Any, state: Any, config: Any, reason: str, run: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    """Revert the canary's commit, blacklist the patch, cool down. Returns what happened."""
    from agent.evolution.evolver import Evolver

    canary = elog.get_kv("canary") or {}
    sha, aid = canary.get("commit_sha"), canary.get("attempt_id")
    attempt = elog.get(aid) if aid else None
    ev = Evolver(repo_root(config), elog, config, run=run, checks=[], clock=state.clock)
    canary.update(status="rolling_back", reason=reason)
    elog.set_kv("canary", canary)
    try:
        if ev.dirty():
            raise RuntimeError("the checkout has uncommitted changes")
        ev.git("revert", "--no-commit", sha)
        ev.git("commit", "-q", "-m", f"[Auto-Evolution] Rollback: {canary.get('title', sha)}\n\nCanary failed: {reason}\n\n"
                                     f"Reverts {sha}.\nEvolution-Attempt: {aid}\n")
        new_head = ev.git("rev-parse", "HEAD")
    except Exception as exc:  # noqa: BLE001 - a failed rollback must stop evolution, not the agent
        ev.git("revert", "--abort", check=False)
        halt = f"rollback of {str(sha)[:12]} failed ({exc}); canary reason: {reason}. Evolution halted: fix or revert by hand, " \
               "then `automonetize evolution resume`."
        elog.set_kv("halted", halt)
        canary.update(status="rollback_failed", reason=halt)
        elog.set_kv("canary", canary)
        state.log_error("evolution", halt, kind="alert")
        return {"rolled_back": False, "reason": halt}
    if attempt:
        elog.set_status(aid, "rolled_back", f"{attempt['detail']} | rolled back at {new_head[:12]}: {reason}")
        elog.blacklist(attempt["fingerprint"], f"rolled back: {reason}", aid)
    elog.enter_cooldown(float(config.evolution_cooldown_hours), f"rolled back {str(sha)[:12]}: {reason}")
    canary.update(status="rolled_back", reason=reason, revert_sha=new_head)
    elog.set_kv("canary", canary)
    state.log_error("evolution", f"canary failed, reverted {str(sha)[:12]} → {new_head[:12]}: {reason}", kind="alert")
    state.log_action(int(state.get("iteration", 0)), None, "evolution:rollback", "ok", f"reverted {str(sha)[:12]}: {reason}")
    return {"rolled_back": True, "reason": reason, "revert_sha": new_head}


def canary_tick(state: Any, config: Any, run: Callable[..., Any] = subprocess.run,
                reload: Callable[[str], None] = request_reload, cycle_done: int | None = None) -> str:
    """Check the canary; roll back and reload on failure. Safe to call from any thread, often."""
    path = config.data_dir / "evolution_log.db"
    if not path.exists():
        return "none"
    with _canary_lock:
        elog = evolution_log(config, state.clock)
        status, reason = check_canary(elog, state, cycle_done)
        if status != "failed":
            return status
        result = rollback(elog, state, config, reason, run=run)
        if result["rolled_back"]:
            reload(f"rollback: {reason}")
            return "rolled_back"
        return "rollback_failed"
