"""Heartbeat: an outside service emails you when the agent goes quiet.

The agent can tell you about problems only while it runs. If the Mac is off, asleep, offline or
the agent has crashed, nobody would know. ``send_heartbeat`` pings ``heartbeat_url``
(``HEALTHCHECK_URL`` in ``.env``) every cycle; a free check at https://healthchecks.io (or any
"dead man's switch" service) emails you when the pings stop.

Set it up once: create a check at healthchecks.io (period: 1 hour, grace: 1 hour), copy its ping
URL and run ``automonetize heartbeat <url>``. Part of the agent (``agent/``), not of
``strategies/``, so self-evolution can't change it.
"""

from __future__ import annotations

from typing import Any

from strategies.base import Strategy, TaskContext, TaskResult


def ping(http: Any, url: str) -> None:
    http.get(url, check_robots=False, attempts=2)


class Heartbeat(Strategy):
    name = "heartbeat"
    tasks = ("send_heartbeat",)

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        cfg, state = ctx.tools.config, ctx.tools.state
        url = str(cfg.heartbeat_url or "").strip()
        if not url:
            return TaskResult(True, "no heartbeat URL (see `automonetize heartbeat`)", {})
        if not url.startswith("https://"):
            return TaskResult(True, "heartbeat URL must start with https://", {})
        try:
            ping(ctx.tools.http, url)
        except Exception as exc:  # noqa: BLE001 - a missed ping is what the service alerts on; never fail the cycle
            state.log_error("heartbeat", f"heartbeat ping failed: {exc!r}")
            return TaskResult(True, f"heartbeat failed: {exc!r}"[:200], {"ok": False})
        state.set("last_heartbeat_at", state.now())
        return TaskResult(True, "heartbeat sent", {"ok": True})
