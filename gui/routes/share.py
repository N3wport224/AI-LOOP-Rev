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


def routes() -> list[web.RouteDef]:
    return [web.get("/api/share", get_share)]
