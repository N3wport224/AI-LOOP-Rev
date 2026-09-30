"""Read-only telemetry snapshot of the agent, shared by the dashboard and `status --json`."""

from __future__ import annotations

from typing import Any

from agent.config import Config
from agent.engine import uptime_seconds
from agent.state import StateStore
from tools.revenue_tracker import RevenueTracker


def collect_snapshot(state: StateStore, config: Config) -> dict[str, Any]:
    hyp = state.active_hypothesis()
    revenue = RevenueTracker(state, daily_target_cents=config.daily_target_cents)
    breaker = state.get("breaker", {}) or {}
    emergency = state.get("emergency_stop")
    stop_file = config.stop_file.exists()
    hypotheses = state.list_hypotheses()
    assets = state.list_assets()
    return {
        "objective": f"Reach ${config.daily_target_cents / 100:.2f}/day verified net revenue",
        "hypothesis": None
        if hyp is None
        else {
            "id": hyp["id"],
            "key": hyp["key"],
            "description": hyp["description"],
            "iterations": hyp["iterations"],
            "pivot_after": config.pivot_after_iterations,
            "revenue_cents": state.revenue_for_hypothesis(hyp["id"]),
            "origin": hyp["params"].get("origin", ""),
        },
        "hypotheses": {
            "total": len(hypotheses),
            "deprecated": sum(1 for h in hypotheses if h["status"] == "deprecated"),
        },
        "iteration": int(state.get("iteration", 0)),
        "uptime_seconds": uptime_seconds(state),
        "last_cycle_at": state.get("last_cycle_at"),
        "pid": state.get("pid"),
        "leads_total": state.count_leads(),
        "leads_active_niche": state.count_leads(hyp["params"]["niche"]) if hyp else 0,
        "assets": [
            {k: a[k] for k in ("id", "title", "version", "lead_count", "status", "product_ref", "path")}
            for a in assets[:5]
        ],
        "assets_total": len(assets),
        "outreach": state.outreach_counts(),
        "revenue_today": revenue.daily_summary(),
        "revenue_history": revenue.history(7),
        "actions_total": state.count_actions(),
        "actions_failed": state.count_actions("failed"),
        "recent_actions": state.recent_actions(8),
        "recent_errors": [
            {k: e[k] for k in ("created_at", "source", "kind", "message")} for e in state.recent_errors(6)
        ],
        "operational_failures": state.count_errors("operational_failure"),
        "breaker": {
            "status": "STOPPED" if (emergency or stop_file) and not breaker.get("tripped") else breaker.get("status", "OK"),
            "consecutive_errors": breaker.get("consecutive_errors", 0),
            "max_consecutive_errors": breaker.get("max_consecutive_errors", config.max_consecutive_errors),
            "trip_reason": breaker.get("trip_reason", ""),
            "emergency_stop": (emergency or {}).get("reason") or ("stop file present" if stop_file else ""),
        },
    }
