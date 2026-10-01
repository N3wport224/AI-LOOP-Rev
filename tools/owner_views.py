"""What a task does and what changed (Phases 135 and 138).

* ``explain(task)`` (``automonetize explain <task>``): the task's purpose (from its code's own
  documentation), its place in the cycle and its last runs, so a line in the log is never a mystery.
* ``what_changed(days)`` (``automonetize what-changed [--days N]``): in one list, everything that
  changed: settings, prices, new dataset versions, installed updates, resting tasks and muted alerts.
"""

from __future__ import annotations

import inspect
from datetime import timedelta
from typing import Any


def _purpose(obj: Any) -> str:
    doc = inspect.getdoc(inspect.getmodule(obj)) or inspect.getdoc(obj) or ""
    return doc.split("\n\n")[0].replace("\n", " ").strip()


CORE = {"evolve_code": "agent.evolution.task:EvolutionStrategy", "backup_data": "agent.backup:Backups",
        "send_heartbeat": "agent.heartbeat:Heartbeat", "self_update": "agent.self_update:SelfUpdate",
        "audit_security": "agent.security_audit:SecurityAudit", "housekeeping": "agent.housekeeping:Housekeeping",
        "check_disk": "agent.disk_guard:DiskGuard", "ops_checks": "agent.ops_checks:OpsChecks",
        "sync_revenue": "tools.revenue_tracker:RevenueTracker"}


def handler_for(task: str) -> Any:
    """The class that runs ``task`` (strategies, or the agent's own core tasks)."""
    import importlib

    from strategies import default_strategies

    for s in default_strategies():
        if task in s.tasks:
            return type(s)
    if task in CORE:
        mod, _, name = CORE[task].partition(":")
        return getattr(importlib.import_module(mod), name, None)
    return None


def explain(state: Any, task: str) -> dict[str, Any] | None:
    from agent.engine import PLAN
    from agent.task_cooldown import cooling

    plan = dict(PLAN)
    if task not in plan:
        return None
    cls = handler_for(task)
    runs = state._all("SELECT status, detail, duration, created_at FROM actions WHERE name = ? ORDER BY id DESC LIMIT 5",
                      (task,))
    return {"task": task, "priority": plan[task], "position": sorted(plan.values()).index(plan[task]) + 1, "of": len(plan),
            "purpose": _purpose(cls) if cls is not None else "", "source": cls.__module__ if cls is not None else "",
            "runs": runs, "resting_until": cooling(state, task)}


def what_changed(state: Any, days: int = 7) -> list[tuple[str, str]]:
    """[(when, what)] newest first."""
    since = (state.clock() - timedelta(days=days)).isoformat(timespec="seconds")
    out: list[tuple[str, str]] = []
    for h in state.get("settings_history") or []:
        if str(h.get("at", "")) >= since:
            out.append((h["at"], "settings: " + "; ".join(h.get("changes", []))[:300]))
    for p in state.get("price_history") or []:
        if p["at"] >= since:
            out.append((p["at"], f"price: {p['title'] or p['asset_id']} ${p['old_cents'] / 100:.2f} → ${p['new_cents'] / 100:.2f}"))
    for a in state._all("SELECT title, version, lead_count, created_at FROM assets WHERE created_at >= ? AND kind = 'lead_directory'",
                        (since,)):
        out.append((a["created_at"], f"new version: {a['title']} v{a['version']} ({a['lead_count']} rows)"))
    news = state.get("whats_new") or {}
    if news.get("at", "") >= since and news.get("target"):
        out.append((news["at"], f"update installed: {news['target'][:12]}"))
    from agent.task_cooldown import resting
    from tools.alert_mute import active

    now = state.now()
    out += [(now, f"resting until {until[:16]}: {task}") for task, until in resting(state).items()]
    out += [(now, f"alerts muted until {until[:16]}: {source}") for source, until in active(state).items()]
    return sorted(out, reverse=True)
