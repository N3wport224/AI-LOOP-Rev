"""The ``evolve_code`` engine task: diagnose every cycle, evolve at most once per interval.

It lives in ``agent/`` (immutable to evolution), not ``strategies/``, so the agent can't edit the
code that decides whether and how it edits itself. Order of gates:

1. ``ENABLE_AUTONOMOUS_CODE_EVOLUTION`` must be ``true`` (default ``false``). Diagnosis still runs
   and is shown on the dashboards, so you can see what it would do before switching it on.
2. not halted by a failed rollback; no canary in progress; no cooldown; the last attempt was more
   than ``evolution_interval_hours`` ago; ``git`` available and the repo is a git checkout.
3. the best untried patch goes through ``Evolver.attempt``. A merge starts the canary and asks the
   supervisor for a graceful reload.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timedelta
from typing import Any, Callable

from agent.evolution.diagnostics import diagnose
from agent.evolution.evolver import Check, Evolver
from agent.evolution.hot_reload import evolution_log, repo_root, request_reload, start_canary
from strategies.base import Strategy, TaskContext, TaskResult


def blocked(config: Any, elog: Any, state: Any) -> str | None:
    """Why evolution can't run now, or None."""
    if not config.enable_autonomous_code_evolution:
        return "autonomous code evolution is off (ENABLE_AUTONOMOUS_CODE_EVOLUTION=false)"
    if elog.halted():
        return f"halted: {elog.halted()}"
    canary = elog.get_kv("canary") or {}
    if canary.get("status") in ("monitoring", "rolling_back"):
        return f"canary watching {str(canary.get('commit_sha'))[:12]} until {canary.get('until')}"
    until = elog.cooldown_until()
    if until:
        return f"cooling down until {until.isoformat(timespec='minutes')} ({elog.get_kv('cooldown_reason')})"
    last = elog.get_kv("last_attempt_at")
    if last and state.clock() < datetime.fromisoformat(last) + timedelta(hours=float(config.evolution_interval_hours)):
        return f"next attempt after {(datetime.fromisoformat(last) + timedelta(hours=float(config.evolution_interval_hours))).isoformat(timespec='minutes')}"
    repo = repo_root(config)
    if shutil.which("git") is None:
        return "git is not installed"
    if not (repo / ".git").exists():
        return f"{repo} is not a git checkout"
    return None


class EvolutionStrategy(Strategy):
    name = "code_evolution"
    tasks = ("evolve_code",)

    def __init__(self, run: Callable[..., Any] = subprocess.run, checks: list[Check] | None = None,
                 reload: Callable[[str], None] = request_reload):
        self._run = run
        self._checks = checks
        self._reload = reload

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        tools, state, cfg = ctx.tools, ctx.tools.state, ctx.tools.config
        elog = evolution_log(cfg, state.clock)
        repo = repo_root(cfg)
        findings = diagnose(state, cfg, repo, log=elog)
        ready = [f.hypothesis for f in findings if f.hypothesis is not None and not elog.tried(f.hypothesis.fingerprint)]
        elog.set_kv("last_diagnosis", {"at": state.now(), "ready": len(ready), "findings": [
            {k: v for k, v in f.to_dict().items() if k != "evidence"} | {"hypothesis": {k: v for k, v in f.hypothesis.to_dict().items()
                                                                                       if k != "evidence"} if f.hypothesis else None}
            for f in findings[:20]]})
        metrics: dict[str, Any] = {"findings": len(findings), "ready": len(ready)}
        head = f"{len(findings)} finding(s), {len(ready)} patch(es) ready"
        why = blocked(cfg, elog, state)
        if why:
            return TaskResult(True, f"{head}; {why}", metrics)
        if not ready:
            return TaskResult(True, f"{head}; nothing to evolve", metrics)
        hyp = ready[0]
        elog.set_kv("last_attempt_at", state.now())
        from agent.backup import safety_backup

        if not os.environ.get("AM_NO_SELF_UPDATE"):  # sandboxes never write real backups
            safety_backup(cfg, repo, state.clock(), "pre-evolution")
        outcome = Evolver(repo, elog, cfg, run=self._run, checks=self._checks, clock=state.clock).attempt(hyp)
        metrics.update(attempt_id=outcome.attempt_id, outcome=outcome.status, commit=outcome.commit_sha)
        state.log_action(int(state.get("iteration", 0)), ctx.hypothesis["id"], f"evolution:{outcome.status}",
                         "ok" if outcome.status in ("merged", "aborted") else "failed",
                         f"#{outcome.attempt_id} {hyp.title}: {outcome.detail}"[:500])
        if outcome.status == "merged":
            start_canary(elog, state, cfg, outcome.attempt_id, outcome.commit_sha or "", [c.path for c in hyp.changes], hyp.title)
            self._reload(f"evolution #{outcome.attempt_id} merged: {hyp.title}")
        elif outcome.status in ("failed", "rejected"):
            tools.state.log_error("evolution", f"attempt #{outcome.attempt_id} {outcome.status}: {outcome.detail}"[:1000], kind="evolution")
        return TaskResult(True, f"{head}; attempt #{outcome.attempt_id} {outcome.status}: {hyp.title}", metrics)
