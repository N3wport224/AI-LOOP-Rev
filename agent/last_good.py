"""Crash-loop rollback (Phase 74): if new code keeps crashing, go back to the last code that worked.

Self-update's canary watches the first hour after an update. This covers the rest of the time
(an update installed by hand, a bug that shows up on day two):

* **Last known good:** once the current commit has run for ``LAST_GOOD_HOURS`` (24) with no crashed
  cycle, it's recorded as last known good (kv ``last_good``).
* **Crash loop:** ``CRASH_LIMIT`` (3) crashed cycles in a row on a commit that isn't the last known
  good one → the checkout is reset to the last known good commit (``git reset --keep``, so
  uncommitted edits are never lost), the agent reloads, self-update skips the bad version, and you
  get an alert with both commits.

Never runs over uncommitted changes, and never in sandboxes. Part of ``agent/``.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta
from typing import Any, Callable

KEY = "last_good"
STREAK = "crash_streak"
SEEN = "head_seen"
LAST_GOOD_HOURS = 24
CRASH_LIMIT = 3


def head(repo: Any, run: Callable[..., Any] = subprocess.run) -> str:
    proc = run(["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, timeout=30)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def tick(state: Any, config: Any, status: str, run: Callable[..., Any] = subprocess.run,
         reload: Callable[[str], None] | None = None) -> str:
    """Call after every cycle. Returns what it did: "", "recorded", "rolled_back" or "blocked: ..."."""
    from agent.evolution.hot_reload import repo_root, request_reload
    from agent.self_update import NO_UPDATE_ENV, Updater

    if os.environ.get(NO_UPDATE_ENV) and reload is None:
        return ""
    repo = repo_root(config)
    if not (repo / ".git").exists():
        return ""
    sha = head(repo, run)
    if not sha:
        return ""
    now = state.clock()
    seen = dict(state.get(SEEN) or {})
    if sha not in seen:
        seen = {sha: now.isoformat(timespec="seconds")}  # only the current commit matters
        state.set(SEEN, seen)
        state.set(STREAK, 0)
    if status == "crashed":
        streak = int(state.get(STREAK) or 0) + 1
        state.set(STREAK, streak)
        seen[sha] = now.isoformat(timespec="seconds")  # the 24 healthy hours start again
        state.set(SEEN, seen)
        good = (state.get(KEY) or {}).get("sha")
        if streak >= CRASH_LIMIT and good and good != sha:
            up = Updater(repo, config, state, run=run, checks=[])
            if up.git("status", "--porcelain", "--untracked-files=no", check=False):
                return "blocked: uncommitted changes"
            try:
                up.roll_back(good)
            except Exception as exc:  # noqa: BLE001
                state.log_error("last_good", f"crash loop on {sha[:12]}, and going back to {good[:12]} failed: {exc!r}", kind="alert")
                return "blocked: rollback failed"
            data = dict(state.get("self_update") or {})
            upstream = up.git("rev-parse", "@{u}", check=False)
            if upstream:
                data["skip"] = upstream  # don't reinstall the version that crashed
            state.set("self_update", data)
            state.set(STREAK, 0)
            state.log_error("last_good", f"The agent crashed {streak} cycles in a row on {sha[:12]}, so it went back to the last "
                                         f"version that worked ({good[:12]}). That update is skipped until a newer one arrives.",
                            kind="alert")
            (reload or request_reload)(f"crash loop: back to {good[:12]}")
            return "rolled_back"
        return ""
    state.set(STREAK, 0)
    if (state.get(KEY) or {}).get("sha") != sha and now - datetime.fromisoformat(seen[sha]) >= timedelta(hours=LAST_GOOD_HOURS):
        state.set(KEY, {"sha": sha, "at": now.isoformat(timespec="seconds")})
        return "recorded"
    return ""
