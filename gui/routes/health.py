"""Health tab (Phase 64): ``automonetize doctor`` in the control panel, with Fix buttons.

``GET /api/health`` runs the doctor's checks (the quick ones, no ``git fetch``) and returns each
finding. ``POST /api/health/fix`` applies one *safe* automatic fix by name (start the agent, turn
on autostart, install an update): the same fixes ``automonetize doctor --fix`` applies.
``POST /api/quiet`` switches quiet mode (hold marketing email) on or off.
"""

from __future__ import annotations

import asyncio
from typing import Any

from aiohttp import web

from gui.context import ctx


def _doctor(gctx, run: Any = None):
    from cli.doctor import Doctor

    kwargs = {"run": run} if run else {}
    return Doctor(gctx.fresh_config(), gctx.state, gctx.controller, **kwargs)


def findings(gctx) -> dict[str, Any]:
    from tools.contact_policy import quiet

    try:
        out = [{"name": f.name, "status": f.status, "detail": f.detail, "fix": f.fix, "can_fix": f.auto is not None}
               for f in _doctor(gctx).checks(deep=False)]
    except Exception as exc:  # noqa: BLE001 - show the failure instead of an empty tab
        out = [{"name": "Health check", "status": "fail", "detail": f"the check itself failed: {exc!r}"[:300],
                "fix": "run `automonetize doctor` in Terminal for details", "can_fix": False}]
    order = {"fail": 0, "warn": 1, "ok": 2}
    out.sort(key=lambda f: order.get(f["status"], 3))
    q = quiet(gctx.state)
    return {"findings": out, "quiet": bool(q), "quiet_since": (q or {}).get("since")}


async def get_health(request: web.Request) -> web.Response:
    data = await asyncio.get_running_loop().run_in_executor(None, findings, ctx(request))
    return web.json_response(data, headers={"Cache-Control": "no-store"})


async def post_fix(request: web.Request) -> web.Response:
    gctx = ctx(request)
    try:
        name = str((await request.json()).get("name", ""))
    except (ValueError, AttributeError):
        return web.json_response({"ok": False, "message": "invalid JSON"}, status=400)

    def apply() -> tuple[bool, str]:
        match = next((f for f in _doctor(gctx).checks(deep=False) if f.name == name and f.auto is not None), None)
        if match is None:
            return False, "nothing to fix automatically here (or it's already fixed)"
        return True, match.auto() or "done"

    ok, message = await asyncio.get_running_loop().run_in_executor(None, apply)
    return web.json_response({"ok": ok, "message": message}, status=200 if ok else 409)


async def post_quiet(request: web.Request) -> web.Response:
    from tools.contact_policy import set_quiet

    gctx = ctx(request)
    try:
        on = (await request.json()).get("on")
    except (ValueError, AttributeError):
        on = None
    if not isinstance(on, bool):
        return web.json_response({"ok": False, "message": "send {on: true|false}"}, status=400)
    await asyncio.get_running_loop().run_in_executor(None, lambda: set_quiet(gctx.state, on, "turned on in the control panel"))
    return web.json_response({"ok": True, "message": "Quiet mode on: marketing email is held." if on
                              else "Quiet mode off: marketing email goes out again."})


def routes() -> list[web.RouteDef]:
    return [web.get("/api/health", get_health), web.post("/api/health/fix", post_fix), web.post("/api/quiet", post_quiet)]
