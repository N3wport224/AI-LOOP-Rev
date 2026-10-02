"""Share kit: ``GET /api/share``, this week's ready-to-paste posts with tracked links.

Read-only. The agent never posts as you; this tab puts each post one "Copy" click away.
"""

from __future__ import annotations

import asyncio
from typing import Any

from aiohttp import web

from gui.context import ctx


def kit(gctx) -> dict[str, Any]:
    from strategies.share_kit import KEY, build_kit
    from tools.file_io import SandboxedFileIO

    cfg = gctx.fresh_config()
    data = build_kit(gctx.state, SandboxedFileIO(cfg.data_dir))  # cheap: reads the database only
    gctx.state.set(KEY, data)
    return data


async def get_share(request: web.Request) -> web.Response:
    data = await asyncio.get_running_loop().run_in_executor(None, kit, ctx(request))
    return web.json_response(data, headers={"Cache-Control": "no-store"})


async def get_testimonials(request: web.Request) -> web.Response:
    from strategies.testimonials import listing

    gctx = ctx(request)
    rows = [{k: i[k] for k in ("id", "niche", "text", "status", "at")} for i in listing(gctx.state)]
    return web.json_response({"testimonials": rows[::-1]}, headers={"Cache-Control": "no-store"})


async def post_testimonials(request: web.Request) -> web.Response:
    from strategies.testimonials import set_status

    gctx = ctx(request)
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"ok": False, "message": "invalid JSON"}, status=400)
    body = body if isinstance(body, dict) else {}  # a JSON list or string is just an unknown action
    action, ids = str(body.get("action", "")), body.get("ids")
    status = {"approve": "approved", "reject": "rejected"}.get(action)
    if not status or not isinstance(ids, list) or not ids or len(ids) > 200 \
            or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
        return web.json_response({"ok": False, "message": "send {action: approve|reject, ids: [numbers]}"}, status=400)
    changed = await asyncio.get_running_loop().run_in_executor(None, set_status, gctx.state, ids, status)
    note = " They appear on the product page with the next site rebuild." if status == "approved" and changed else ""
    return web.json_response({"ok": True, "changed": changed, "message": f"{changed} quote(s) {status}.{note}"})


def routes() -> list[web.RouteDef]:
    return [web.get("/api/share", get_share), web.get("/api/testimonials", get_testimonials),
            web.post("/api/testimonials", post_testimonials)]
