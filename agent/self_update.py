"""Self-update: the agent installs new versions of its own code, safely, without you.

Every ``auto_update_hours`` (6) the ``self_update`` task checks the branch this checkout tracks
(e.g. ``origin/claude/...``) for new commits. When there are some:

1. **Sandbox.** The update is checked out in a temporary git worktree (merged with any local
   commits, e.g. self-evolution's). A merge conflict stops here: you get an alert.
2. **Dependencies.** If ``requirements.txt`` or ``pyproject.toml`` changed, the new requirements
   are installed into this Python first (adding packages doesn't affect the running code).
3. **Checks.** The full test suite (and the end-to-end simulator) must pass in the worktree, with
   no secrets in its environment, exactly as for self-evolution.
4. **Switch.** The branch fast-forwards to the verified commit and the agent reloads itself
   (the webhook socket is kept, so no payment is missed).
5. **Canary.** For ``evolution_canary_minutes`` (60) after the switch, a crash, a new kind of
   operational failure or an exception in a changed file rolls the checkout back to the previous
   commit and reloads. That upstream version is skipped until a newer one appears.

Never runs over uncommitted changes, a detached HEAD or a branch without an upstream, and never
while a self-evolution canary is running. Off with ``auto_update = false``. Part of ``agent/``
(not ``strategies/``), so self-evolution can't change it. State: kv ``self_update``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from strategies.base import Strategy, TaskContext, TaskResult

KEY = "self_update"
NO_UPDATE_ENV = "AM_NO_SELF_UPDATE"  # set in sandboxes (tests, simulator, evolution checks): never touch the real checkout
DEP_FILES = ("requirements.txt", "pyproject.toml")


def info(state: Any) -> dict[str, Any]:
    return dict(state.get(KEY) or {})


class Updater:
    def __init__(self, repo: Path, config: Any, state: Any, run: Callable[..., Any] = subprocess.run, checks: list | None = None):
        from agent.evolution.evolver import default_checks

        self.repo, self.config, self.state, self.run = Path(repo), config, state, run
        self.checks = default_checks(config) if checks is None else checks

    def git(self, *args: str, cwd: Path | None = None, check: bool = True, timeout: float = 300) -> str:
        from agent.evolution.evolver import AGENT_EMAIL, AGENT_NAME

        proc = self.run(["git", "-c", f"user.name={AGENT_NAME}", "-c", f"user.email={AGENT_EMAIL}", "-c", "commit.gpgsign=false",
                         *args], cwd=str(cwd or self.repo), capture_output=True, text=True, timeout=timeout)
        if check and proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()[:400]}")
        return (proc.stdout or "").strip()

    def blocked(self) -> str | None:
        if shutil.which("git") is None and self.run is subprocess.run:
            return "git is not installed"
        if not (self.repo / ".git").exists():
            return f"{self.repo} is not a git checkout"
        if not self.git("symbolic-ref", "--quiet", "--short", "HEAD", check=False):
            return "detached HEAD"
        if self.git("status", "--porcelain", "--untracked-files=no"):
            return "uncommitted changes in the checkout (the agent never updates over your edits)"
        if not self.git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False):
            return "this branch has no upstream to update from"
        return None

    def pending(self) -> tuple[str, str, int]:
        """(HEAD, upstream sha, commits behind) after a fetch."""
        self.git("fetch", "--quiet", timeout=180)
        head = self.git("rev-parse", "HEAD")
        upstream = self.git("rev-parse", "@{u}")
        behind = int(self.git("rev-list", "--count", "HEAD..@{u}") or 0)
        return head, upstream, behind

    def verify(self, head: str, upstream: str) -> tuple[bool, str, str, list[str]]:
        """Build the update in a worktree and run the checks. (ok, target sha, detail, changed files)."""
        from agent.evolution.evolver import sandbox_env

        worktree = Path(tempfile.mkdtemp(prefix="automonetize-update-"))
        branch = f"auto/update-{upstream[:12]}"
        try:
            self.git("worktree", "add", "-B", branch, str(worktree), head)
            fast_forward = self.run(["git", "merge-base", "--is-ancestor", head, upstream], cwd=str(self.repo),
                                    capture_output=True, text=True, timeout=60).returncode == 0
            if fast_forward:
                self.git("merge", "--ff-only", "-q", upstream, cwd=worktree)
            else:  # local commits (e.g. self-evolution): merge them with the update
                try:
                    self.git("merge", "--no-edit", "-q", upstream, cwd=worktree)
                except RuntimeError as exc:
                    self.git("merge", "--abort", cwd=worktree, check=False)
                    return False, "", f"the update conflicts with local commits: {exc}", []
            target = self.git("rev-parse", "HEAD", cwd=worktree)
            changed = [f for f in self.git("diff", "--name-only", head, target).splitlines() if f]
            if any(f in DEP_FILES for f in changed) and (worktree / "requirements.txt").exists():
                pip = self.run([sys.executable, "-m", "pip", "install", "--quiet", "-r", str(worktree / "requirements.txt")],
                               cwd=str(worktree), capture_output=True, text=True, timeout=900)
                if pip.returncode != 0:
                    return False, target, f"installing the new requirements failed: {(pip.stdout + pip.stderr)[-300:]}", changed
            env = sandbox_env(worktree)
            for check in self.checks:
                try:
                    proc = self.run(check.argv, cwd=str(worktree), env=env, capture_output=True, text=True, timeout=check.timeout)
                except subprocess.TimeoutExpired:
                    return False, target, f"{check.name} timed out", changed
                if proc.returncode != 0:
                    return False, target, f"{check.name} failed: {((proc.stdout or '') + (proc.stderr or ''))[-400:]}", changed
            return True, target, "checks passed", changed
        finally:
            self.git("worktree", "remove", "--force", str(worktree), check=False)
            shutil.rmtree(worktree, ignore_errors=True)
            self.git("worktree", "prune", check=False)
            self.git("branch", "-D", branch, check=False)

    def switch(self, head: str, target: str) -> None:
        if self.git("rev-parse", "HEAD") != head or self.git("status", "--porcelain", "--untracked-files=no"):
            raise RuntimeError("the checkout changed while the update was being checked")
        self.git("merge", "--ff-only", "-q", target)

    def roll_back(self, previous: str) -> None:
        self.git("reset", "--keep", previous)


def canary_tick(state: Any, config: Any, run: Callable[..., Any] = subprocess.run,
                reload: Callable[[str], None] | None = None, cycle_done: int | None = None) -> str:
    """After an update: roll back and reload on a regression; pass after the window and one full cycle."""
    from agent.evolution.hot_reload import canary_failure, repo_root, request_reload

    data = info(state)
    canary = data.get("canary")
    if not canary or canary.get("status") != "monitoring":
        return "none"
    failure = canary_failure(state, canary)
    if failure:
        Updater(repo_root(config), config, state, run=run, checks=[]).roll_back(canary["previous"])
        canary.update(status="rolled_back", reason=failure)
        data.update(canary=canary, skip=canary["upstream"])
        state.set(KEY, data)
        state.log_error("self_update", f"The update to {canary['target'][:12]} misbehaved ({failure}); rolled back to "
                                       f"{canary['previous'][:12]}. It will be skipped until a newer version is published.", kind="alert")
        (reload or request_reload)(f"self-update rollback: {failure}")
        return "rolled_back"
    if cycle_done is not None and cycle_done > int(canary["iteration"]):
        canary["cycles"] = int(canary.get("cycles", 0)) + 1
    if state.clock() >= datetime.fromisoformat(canary["until"]) and int(canary.get("cycles", 0)) >= 1:
        canary.update(status="passed")
        state.set("whats_new", {"target": canary["target"], "changes": data.get("changes") or [], "at": state.now(), "sent": False})
        state.log_action(int(state.get("iteration", 0)), None, "self_update", "ok", f"{canary['target'][:12]} healthy after the canary window")
    data["canary"] = canary
    state.set(KEY, data)
    return canary["status"]


class SelfUpdate(Strategy):
    name = "self_update"
    tasks = ("self_update",)

    def __init__(self, run: Callable[..., Any] = subprocess.run, checks: list | None = None,
                 reload: Callable[[str], None] | None = None):
        self._run, self._checks, self._reload = run, checks, reload

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        from agent.evolution.hot_reload import evolution_log, repo_root, request_reload

        cfg, state = ctx.tools.config, ctx.tools.state
        if not cfg.auto_update or os.environ.get(NO_UPDATE_ENV):
            return TaskResult(True, "self-update off", {})
        data = info(state)
        if (data.get("canary") or {}).get("status") == "monitoring":
            return TaskResult(True, "watching the last update", {})
        last = data.get("checked_at")
        if last and state.clock() - datetime.fromisoformat(last) < timedelta(hours=float(cfg.auto_update_hours)):
            return TaskResult(True, f"checked for updates {last[:16]}", {})
        if (cfg.data_dir / "evolution_log.db").exists() and \
                ((evolution_log(cfg, state.clock).get_kv("canary") or {}).get("status") in ("monitoring", "rolling_back")):
            return TaskResult(True, "self-evolution canary running; update later", {})
        up = Updater(repo_root(cfg), cfg, state, run=self._run, checks=self._checks)
        data["checked_at"] = state.now()
        why = up.blocked()
        if why:
            data["status"] = f"not updating: {why}"
            state.set(KEY, data)
            return TaskResult(True, data["status"], {})
        try:
            head, upstream, behind = up.pending()
        except Exception as exc:  # noqa: BLE001 - offline etc.: try again next interval
            data["status"] = f"couldn't check for updates: {exc}"[:300]
            state.set(KEY, data)
            return TaskResult(True, data["status"], {})
        if not behind:
            data["status"] = "up to date"
            state.set(KEY, data)
            return TaskResult(True, "up to date", {"behind": 0})
        if data.get("skip") == upstream:
            data["status"] = f"skipping {upstream[:12]} (rolled back earlier); waiting for a newer version"
            state.set(KEY, data)
            return TaskResult(True, data["status"], {"behind": behind})
        ok, target, detail, changed = up.verify(head, upstream)
        if not ok:
            data.update(status=f"update {upstream[:12]} not installed: {detail}"[:400], skip=upstream)
            state.set(KEY, data)
            state.log_error("self_update", f"An update ({behind} commit(s)) was not installed: {detail}"[:900], kind="alert")
            return TaskResult(True, data["status"], {"behind": behind, "installed": False})
        from agent.backup import safety_backup

        safety_backup(cfg, repo_root(cfg), state.clock(), "pre-update")
        try:
            up.switch(head, target)
        except Exception as exc:  # noqa: BLE001
            data["status"] = f"update postponed: {exc}"[:300]
            state.set(KEY, data)
            return TaskResult(True, data["status"], {"behind": behind, "installed": False})
        last_error = state._one("SELECT COALESCE(MAX(id), 0) AS n FROM errors")["n"]
        day_ago = (state.clock() - timedelta(hours=24)).isoformat(timespec="seconds")
        failing = sorted({r["source"] for r in state._all("SELECT DISTINCT source FROM errors WHERE kind = 'operational_failure' "
                                                            "AND created_at >= ?", (day_ago,))})
        changes = [s for s in up.git("log", "--no-merges", "--format=%s", f"{head}..{target}", check=False).splitlines() if s][:30]
        data.update(status=f"updated to {target[:12]} ({behind} commit(s))", installed_at=state.now(), changes=changes, canary={
            "status": "monitoring", "previous": head, "target": target, "upstream": upstream, "files": changed,
            "until": (state.clock() + timedelta(minutes=int(cfg.evolution_canary_minutes))).isoformat(timespec="seconds"),
            "iteration": int(state.get("iteration", 0)), "error_id": int(last_error), "failing_before": failing})
        state.set(KEY, data)
        state.log_action(int(state.get("iteration", 0)), None, "self_update", "ok", data["status"])
        (self._reload or request_reload)(f"self-update to {target[:12]}")
        return TaskResult(True, data["status"], {"behind": behind, "installed": True})
