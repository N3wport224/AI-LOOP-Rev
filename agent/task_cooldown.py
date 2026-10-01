"""Failing-task cooldown (Phase 130): a task that keeps failing rests instead of failing every cycle.

After ``COOLDOWN_AFTER`` failures in a row, a task is skipped for ``COOLDOWN_HOURS`` and you get one
alert naming it and its last error. Its first success clears the count. Meanwhile the other tasks
keep running, and the repeated failures no longer push the agent towards its emergency stop.

Tasks that handle money or customers (``NEVER_COOL``) are never rested: if delivery is failing,
it must keep trying, and you hear about it through the usual alerts.

State: kv ``task_failures`` = {task: {"n", "until", "last"}}.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

KEY = "task_failures"
COOLDOWN_AFTER = 3
COOLDOWN_HOURS = 24
NEVER_COOL = frozenset({"sync_revenue", "deliver_orders", "deliver_subscriptions", "run_dunning", "sync_refunds",
                        "report_owner", "check_disk", "ops_checks", "backup_data", "answer_support"})


def cooling(state: Any, task: str) -> str | None:
    """The time the task's rest ends, if it's resting."""
    entry = (state.get(KEY) or {}).get(task) or {}
    until = entry.get("until")
    if until and datetime.fromisoformat(until) > state.clock():
        return until
    return None


def record(state: Any, task: str, ok: bool, detail: str = "") -> None:
    failures = dict(state.get(KEY) or {})
    if ok:
        if task in failures:
            failures.pop(task)
            state.set(KEY, failures)
        return
    if task in NEVER_COOL:
        return
    entry = dict(failures.get(task) or {})
    entry["n"] = int(entry.get("n", 0)) + 1
    entry["last"] = detail[:300]
    if entry["n"] >= COOLDOWN_AFTER and not (entry.get("until") and datetime.fromisoformat(entry["until"]) > state.clock()):
        entry["until"] = (state.clock() + timedelta(hours=COOLDOWN_HOURS)).isoformat(timespec="seconds")
        entry["n"] = 0
        state.log_error("engine", f"Task {task} failed {COOLDOWN_AFTER} times in a row, so it rests for {COOLDOWN_HOURS} "
                                  f"hours while everything else keeps running. Last error: {detail[:200]}", kind="alert")
    failures[task] = entry
    state.set(KEY, failures)


def resting(state: Any) -> dict[str, str]:
    return {t: e["until"] for t, e in (state.get(KEY) or {}).items() if e.get("until") and cooling(state, t)}
