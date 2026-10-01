"""Sent tab (Phase 92): every email the agent sent (or logged in dry run), newest first.

``GET /api/sent`` reads the email audit log (``dispatched_audit.log``): when, what kind, to whom,
subject, live or dry run, result, and the text. Read-only. The panel only listens on this Mac.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import Any

from aiohttp import web

from gui.context import ctx

LIMIT = 200


def recent(gctx, limit: int = LIMIT) -> dict[str, Any]:
    from tools.dispatcher import AUDIT_FILE

    path = gctx.fresh_config().data_dir / AUDIT_FILE
    rows: deque = deque(maxlen=limit)
    if path.exists():
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                rows.append({k: rec.get(k) for k in ("ts", "kind", "to", "subject", "mode", "result")}
                            | {"body": str(rec.get("body") or "")[:3000]})
    return {"emails": list(reversed(rows))}


async def get_sent(request: web.Request) -> web.Response:
    data = await asyncio.get_running_loop().run_in_executor(None, recent, ctx(request))
    return web.json_response(data, headers={"Cache-Control": "no-store"})


def routes() -> list[web.RouteDef]:
    return [web.get("/api/sent", get_sent)]
