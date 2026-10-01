"""Webhook health: make sure Stripe can still reach the agent, and repair it when it can't.

Instant delivery depends on Stripe calling ``public_webhook_url`` through the tunnel. (Polling in
``sync_revenue`` is the safety net, so a broken webhook delays deliveries rather than losing them.)
Every ``webhook_check_hours`` (6), in live mode, ``check_webhook``:

1. **Tunnel:** fetches ``<public URL>/healthz``. If that fails, the tunnel (cloudflared) or the
   listener is down: one alert a day with what to run.
2. **Endpoint:** lists the Stripe webhook endpoints. If ours is missing, disabled or lacks an event
   the agent handles, it's repaired with the same code as ``automonetize setup-autonomous`` (a new
   signing secret is saved to ``.env`` and the agent reloads to use it).
3. **Failed deliveries:** counts events from the last day that Stripe couldn't deliver; an alert
   if there are any.

State: kv ``webhook_health``; shown in ``automonetize doctor``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from strategies.base import Strategy, TaskContext, TaskResult

KEY = "webhook_health"
STRIPE_API = "https://api.stripe.com/v1"


class WebhookHealth(Strategy):
    name = "webhook_health"
    tasks = ("check_webhook",)

    def __init__(self, env_file: Path | None = None, reload: Any = None):
        self.env_file, self._reload = env_file, reload

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        from agent.evolution.hot_reload import repo_root, request_reload
        from agent.setup_autonomous import register_webhook_endpoint, webhook_events

        tools, cfg, state = ctx.tools, ctx.tools.config, ctx.tools.state
        if cfg.dry_run or not (cfg.stripe_secret_key and cfg.public_webhook_url):
            return TaskResult(True, "webhook check: not live or no public URL", {})
        prev = state.get(KEY) or {}
        if prev.get("at") and state.clock() - datetime.fromisoformat(prev["at"]) < timedelta(hours=float(cfg.webhook_check_hours)):
            return TaskResult(True, f"webhook checked {prev['at'][:16]}", {})
        problems: list[str] = []
        repaired = ""
        try:
            tools.http.get(f"{cfg.lead_capture_base}/healthz", check_robots=False, attempts=2)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"the public URL doesn't answer ({getattr(exc, 'status', type(exc).__name__)}): is the tunnel "
                            "running? Run `automonetize doctor --fix`, or `cloudflared tunnel run` if you set one up by hand")
        headers = {"Authorization": f"Bearer {cfg.stripe_secret_key}"}
        try:
            listing = tools.http.get_json(f"{STRIPE_API}/webhook_endpoints", params={"limit": 100}, headers=headers,
                                          check_robots=False)
            ep = next((e for e in listing.get("data", []) if e.get("url") == cfg.public_webhook_url), None)
            enabled = set((ep or {}).get("enabled_events") or [])
            broken = ep is None or ep.get("status") == "disabled" or ("*" not in enabled and not set(webhook_events()) <= enabled)
            if broken:
                before = cfg.stripe_webhook_secret
                check = register_webhook_endpoint(cfg, tools.http, self.env_file or repo_root(cfg) / ".env")
                if check.status == "ok":
                    repaired = check.detail
                    state.log_action(int(state.get("iteration", 0)), None, "webhook", "ok", f"repaired: {check.detail}")
                    if cfg.stripe_webhook_secret != before:
                        (self._reload or request_reload)("new Stripe webhook secret")
                else:
                    problems.append(f"Stripe webhook endpoint needs attention: {check.detail}")
            since = int((state.clock() - timedelta(days=1)).timestamp())
            failed = tools.http.get_json(f"{STRIPE_API}/events", params={"delivery_success": "false", "created[gte]": since,
                                                                       "limit": 100}, headers=headers, check_robots=False)
            n = len(failed.get("data") or [])
            if n:
                problems.append(f"{n} Stripe event(s) in the last day couldn't be delivered to the agent (orders still arrive "
                                "by polling, just later)")
        except Exception as exc:  # noqa: BLE001 - reported, retried next interval
            problems.append(f"couldn't check the Stripe webhook: {exc!r}"[:200])
        record = {"at": state.now(), "problems": problems, "repaired": repaired}
        last_alert = prev.get("alerted_at") if problems else None  # a healthy check resets it
        if problems and (not last_alert or state.clock() - datetime.fromisoformat(last_alert) >= timedelta(hours=24)):
            state.log_error("webhook_health", "; ".join(problems), kind="alert")
            last_alert = state.now()
        if last_alert:
            record["alerted_at"] = last_alert
        state.set(KEY, record)
        return TaskResult(True, "webhook: " + ("; ".join(problems) or (f"repaired ({repaired})" if repaired else "healthy")),
                          {"problems": len(problems), "repaired": bool(repaired)})
