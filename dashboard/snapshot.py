"""Read-only telemetry snapshot of the agent, shared by the dashboard and `status --json`."""

from __future__ import annotations

from typing import Any

from agent.config import Config
from agent.engine import uptime_seconds
from agent.hypotheses import score_hypothesis
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
        "funnel": score_hypothesis(state, hyp) if hyp else None,
        "view_tracking": bool(state.get("view_tracking")),
        "distribution": _distribution(state, config),
        "orders": state.order_counts(),
        "assets": [
            {k: a.get(k) for k in ("id", "title", "version", "lead_count", "status", "product_ref", "path",
                                   "provider", "checkout_url", "showcase_url", "price_cents")}
            for a in assets[:5]
        ],
        "assets_total": len(assets),
        "outreach": state.outreach_counts(),
        "revenue_today": revenue.daily_summary(),
        "revenue_history": revenue.history(7),
        "actions_total": state.count_actions(),
        "actions_failed": state.count_actions("failed"),
        "recent_actions": state.recent_actions(20),
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


def _distribution(state: StateStore, config: Config) -> dict[str, Any]:
    """Storefront and dispatch status without building network clients."""
    if config.storefront_provider != "auto":
        provider = config.storefront_provider
    elif config.stripe_secret_key or config.stripe_payment_links:
        provider = "stripe"
    elif config.lemonsqueezy_api_key and config.lemonsqueezy_store_id:
        provider = "lemonsqueezy"
    else:
        provider = "gumroad (staging)"
    from datetime import datetime, timezone

    now = state.clock()
    day = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    first = state.first_send_at()
    weeks = max(0, (now - datetime.fromisoformat(first)).days // 7) if first else 0
    limit = min(config.dispatch_max_per_day, config.warmup_start_per_day + config.warmup_step_per_week * weeks)
    return {
        "storefront": provider,
        "dry_run": config.dry_run,
        "email_backend": config.outreach_email_backend,
        "daily_limit": limit,
        "sent_today": state.sent_since(day),
        "dry_run_today": int(state.get(f"dry_run_count:{day.date()}", 0)),
        "suppressed": len(state.list_suppressed()),
        "webhook": {
            "configured": bool(config.stripe_webhook_secret),
            "last_event": state.get("webhook_last_event"),
            "events": state.webhook_event_counts(),
        },
        "last_sale": state.get("last_sale"),
        "pricing": [
            {k: e[k] for k in ("asset_id", "price_cents", "status", "views", "initiations", "reason")}
            for h in ([state.active_hypothesis()] if state.active_hypothesis() else [])
            for e in state.experiments_for_hypothesis(h["id"]) if e["status"] in ("running", "converged")
        ],
        "syndicated": {k.split(":", 1)[1]: state.get(k) for k in ("syndicated:devto", "syndicated:hashnode", "syndicated:github_discussions") if state.get(k)},
        "feed_items": len(state.get("feed_items", []) or []),
        "offline_since": state.get("offline_since"),
        "last_shutdown": state.get("last_shutdown"),
        "recurring": _recurring(state),
    }


def _recurring(state: StateStore) -> dict[str, Any]:
    from dashboard.analytics import monthly_amount

    subs = state.list_subscribers()
    live = [s for s in subs if s["subscription_status"] in ("active", "trialing")]
    return {
        "active": len(live),
        "past_due": sum(1 for s in subs if s["subscription_status"] == "past_due"),
        "canceled": sum(1 for s in subs if s["subscription_status"] == "canceled"),
        "mrr_cents": round(sum(monthly_amount(s["price_cents"], s["interval"]) for s in live)),
    }
