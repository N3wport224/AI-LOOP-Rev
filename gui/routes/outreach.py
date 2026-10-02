"""Outreach review: ``GET /api/outreach`` (drafts) and ``POST /api/outreach`` (approve / reject).

The agent drafts sales emails but never sends one you haven't approved. This puts the approval a
click away: each draft with its recipient, subject, full text and quality score. Approved drafts
are sent by the next cycle within the warm-up limit, and only if the CAN-SPAM requirements are
met (the response says when they aren't, so an approval never silently goes nowhere).
"""

from __future__ import annotations

import asyncio
from typing import Any

from aiohttp import web

from gui.context import ctx

ACTIONS = {"approve": "approved", "reject": "rejected"}
FIELDS = ("id", "channel", "recipient", "subject", "body", "score", "status", "created_at")


def drafts(gctx, status: str = "pending_review", limit: int = 100) -> dict[str, Any]:
    from tools import build_toolkit
    from tools.circuit_breaker import CircuitBreaker

    cfg = gctx.fresh_config()
    rows = [{k: r.get(k) for k in FIELDS} for r in gctx.state.list_outreach(status, limit=limit)]
    rows.sort(key=lambda r: -(r["score"] or 0))
    try:
        problems = build_toolkit(cfg, gctx.state, CircuitBreaker(1000, 1000, 1000)).dispatcher.compliance_problems("outreach")
    except Exception as exc:  # noqa: BLE001 - show the drafts even if the check can't run
        problems = [repr(exc)]
    return {"drafts": rows, "counts": gctx.state.outreach_counts(), "send_problems": problems, "dry_run": cfg.dry_run,
            "daily_limit": cfg.warmup_start_per_day}


async def get_outreach(request: web.Request) -> web.Response:
    gctx = ctx(request)
    status = request.query.get("status", "pending_review")
    if status not in ("pending_review", "approved", "rejected", "sent"):
        return web.json_response({"ok": False, "message": "unknown status"}, status=400)
    data = await asyncio.get_running_loop().run_in_executor(None, drafts, gctx, status)
    return web.json_response(data, headers={"Cache-Control": "no-store"})


async def post_outreach(request: web.Request) -> web.Response:
    gctx = ctx(request)
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"ok": False, "message": "invalid JSON"}, status=400)
    body = body if isinstance(body, dict) else {}  # a JSON list or string is just an unknown action
    action = str(body.get("action", ""))
    ids = body.get("ids")
    if action not in ACTIONS or not isinstance(ids, list) or not ids or len(ids) > 200 \
            or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
        return web.json_response({"ok": False, "message": "send {action: approve|reject, ids: [numbers]}"}, status=400)

    def apply() -> int:
        done = 0
        for oid in ids:
            row = gctx.state._one("SELECT status FROM outreach_queue WHERE id = ?", (oid,))
            if row and row["status"] == "pending_review" and gctx.state.set_outreach_status(oid, ACTIONS[action]):
                done += 1
        gctx.state.log_action(int(gctx.state.get("iteration", 0)), None, f"outreach:{action}", "ok",
                              f"{done} draft(s) {ACTIONS[action]} in the control panel")
        return done

    done = await asyncio.get_running_loop().run_in_executor(None, apply)
    verb = ACTIONS[action]
    return web.json_response({"ok": True, "changed": done, "message": f"{done} draft(s) {verb}."
                              + (" They go out with the next cycles, within the daily limit." if action == "approve" and done else "")})


def routes() -> list[web.RouteDef]:
    return [web.get("/api/outreach", get_outreach), web.post("/api/outreach", post_outreach)]
