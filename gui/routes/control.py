"""Master control panel: ``GET /api/status``, ``POST /api/control``, ``GET /api/logs``.

The page polls ``/api/status`` every few seconds (supervisor, webhook health, engine state,
active niche, uptime, revenue today vs target, MRR, leads) and ``/api/logs`` for the action log
(SQLite ``actions``) plus the tail of the agent's log file. Secrets are redacted from log lines
before they leave the server.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from aiohttp import web

from dashboard.snapshot import collect_snapshot
from gui.context import ctx

ACTIONS = {"start", "pause", "resume", "kill", "restart"}
SECRET_PATTERNS = [
    re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{6,}"),
    re.compile(r"\bwhsec_[A-Za-z0-9]{6,}"),
    re.compile(r"\bSG\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{10,}|github_pat_[A-Za-z0-9_]{10,})"),
    re.compile(r"(?i)\b(password|passwd|token|secret|api[_-]?key)(\s*[=:]\s*)(\S+)"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._-]{8,}"),
]
TAIL_BYTES = 64 * 1024


def redact(line: str) -> str:
    for rx in SECRET_PATTERNS:
        if rx.groups == 3:
            line = rx.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", line)
        else:
            line = rx.sub("[redacted]", line)
    return line


def tail(path: Path, lines: int) -> list[str]:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - TAIL_BYTES))
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    out = data.splitlines()
    if size > TAIL_BYTES and out:
        out = out[1:]  # first line is probably cut
    return [redact(line) for line in out[-lines:]]


def log_candidates(gctx) -> list[Path]:
    home_logs = Path.home() / "Library" / "Logs" / "automonetize"
    paths = list(gctx.log_paths) or [home_logs / "agent.stdout.log", home_logs / "agent.stderr.log",
                                     gctx.config.data_dir / "agent.log", gctx.config.data_dir / "supervisor.log"]
    return [p for p in paths if p.exists()]


def build_status(gctx) -> dict[str, Any]:
    from gui.control import probe_webhook

    cfg, state = gctx.config, gctx.state
    snap = collect_snapshot(state, cfg)
    sup = gctx.controller.status()
    webhook = (gctx.webhook_probe or probe_webhook)(cfg) if sup["running"] else {"healthy": False, "events": {}}
    hyp = snap.get("hypothesis") or {}
    today = snap["revenue_today"]
    recurring = (snap.get("distribution") or {}).get("recurring") or {}
    return {
        "supervisor": sup,
        "webhook": {**webhook, "public_url": cfg.public_webhook_url, "secret_set": bool(cfg.stripe_webhook_secret)},
        "engine": {"state": sup["engine"], "reason": sup["reason"], "iteration": snap["iteration"],
                   "last_cycle_at": snap["last_cycle_at"], "breaker": snap["breaker"]},
        "niche": {"key": hyp.get("key"), "description": hyp.get("description"), "iterations": hyp.get("iterations"),
                  "pivot_after": hyp.get("pivot_after")},
        "uptime_seconds": snap["uptime_seconds"] if sup["running"] else 0,
        "revenue": {"net_cents": today["net_cents"], "target_cents": today["target_cents"],
                    "gross_cents": today.get("gross_cents", 0), "orders": snap["orders"]},
        "mrr_cents": recurring.get("mrr_cents", 0), "subscribers": recurring.get("active", 0),
        "leads": state.free_subscriber_counts(),
        "dry_run": cfg.dry_run,
    }


async def get_status(request: web.Request) -> web.Response:
    gctx = ctx(request)
    data = await asyncio.get_running_loop().run_in_executor(None, build_status, gctx)
    return web.json_response(data, headers={"Cache-Control": "no-store"})


async def post_control(request: web.Request) -> web.Response:
    gctx = ctx(request)
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"ok": False, "message": "invalid JSON"}, status=400)
    action = str((body or {}).get("action", ""))
    if action not in ACTIONS:
        return web.json_response({"ok": False, "message": f"unknown action {action!r}"}, status=400)
    if action == "kill" and body.get("confirm") is not True:
        return web.json_response({"ok": False, "message": "the kill switch needs confirm: true"}, status=400)
    result = await asyncio.get_running_loop().run_in_executor(None, gctx.controller.act, action)
    return web.json_response(result, status=200 if result.get("ok") else 500)


async def get_logs(request: web.Request) -> web.Response:
    gctx = ctx(request)
    try:
        lines = max(10, min(1000, int(request.query.get("lines", "200"))))
    except ValueError:
        lines = 200

    def collect() -> dict[str, Any]:
        files = {str(p): tail(p, lines) for p in log_candidates(gctx)}
        actions = [{**a, "detail": redact(a.get("detail") or "")} for a in gctx.state.recent_actions(60)]
        return {"actions": actions, "files": files}

    data = await asyncio.get_running_loop().run_in_executor(None, collect)
    return web.json_response(data, headers={"Cache-Control": "no-store"})


def routes() -> list[web.RouteDef]:
    return [web.get("/api/status", get_status), web.post("/api/control", post_control), web.get("/api/logs", get_logs)]
