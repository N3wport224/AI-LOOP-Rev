"""What visitors look for (Phases 350-354): site searches that find nothing say what to build next.

* **Phase 350, the endpoint:** ``POST /v1/search-log`` takes one search (``{"q": ..., "zero": true}``):
  at most ``MAX_LEN`` characters, letters, digits and a few symbols only, rate-limited per network and
  behind the forms' abuse guard. No address, cookie or identifier is stored, only the words.
* **Phase 351, from the search page:** ``search/`` sends the search after the visitor pauses typing
  (or presses Enter), once per distinct search, and never when the browser asks not to be tracked.
  It works only when the agent has a public address.
* **Phase 352, demand:** a search that found nothing, seen ``MIN_SEARCHES`` (2) times or more, counts
  like a customer request for its technology, so the factory makes that product sooner.
* **Phase 353, in Monday's report:** "Searched for but not on sale: elixir (5), kotlin berlin (3)".
* **Phase 354, kept small:** the ``KEEP`` most frequent searches for at most ``KEEP_DAYS`` days; the
  privacy page says so.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any

KEY = "search_queries"
MAX_LEN = 60
MIN_SEARCHES = 2
KEEP = 500
KEEP_DAYS = 90
PER_IP_HOUR = 30
_ALLOWED = re.compile(r"[^a-z0-9 +#.\-]")


def normalize(q: Any) -> str:
    return " ".join(_ALLOWED.sub(" ", str(q or "").lower()).split())[:MAX_LEN]


# ------------------------------------------------------------------ Phases 352, 354
def record(state: Any, q: str, zero: bool) -> dict[str, Any] | None:
    from strategies.customer_requests import TOPICS
    from strategies.revenue_models import parse_request

    q = normalize(q)
    if len(q) < 3:
        return None
    items = dict(state.get(KEY) or {})
    entry = dict(items.get(q) or {"n": 0, "zero": 0})
    entry["n"] += 1
    entry["zero"] += 1 if zero else 0
    entry["last"] = state.now()
    items[q] = entry
    cutoff = (state.clock() - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")
    items = {k: v for k, v in items.items() if v.get("last", "") >= cutoff}
    items = dict(sorted(items.items(), key=lambda kv: -kv[1]["n"])[:KEEP])
    state.set(KEY, items)
    if zero and entry["zero"] == MIN_SEARCHES:  # counts once, when it becomes a pattern
        tech = parse_request(q).get("tech")
        if tech:
            topics = dict(state.get(TOPICS) or {})
            topics[tech] = int(topics.get(tech) or 0) + 1
            state.set(TOPICS, topics)
    return entry


# ------------------------------------------------------------------ Phase 353
def not_found(state: Any, limit: int = 8) -> list[tuple[str, int]]:
    items = state.get(KEY) or {}
    return sorted(((q, v["zero"]) for q, v in items.items() if v.get("zero", 0) >= MIN_SEARCHES), key=lambda x: -x[1])[:limit]


def weekly_line(state: Any) -> str:
    rows = not_found(state)
    if not rows:
        return ""
    return "Searched for on the site but not on sale: " + ", ".join(f"{q} ({n})" for q, n in rows) + "."


# ------------------------------------------------------------------ Phase 350
def mount(app: Any, tools: Any, run: Any, client_ip: Any, limiter: Any) -> None:
    from aiohttp import web

    from tools.abuse_guard import for_app

    guard = for_app(app, tools.state)

    async def handler(request: web.Request) -> web.Response:
        if request.content_length and request.content_length > 512:
            return web.Response(status=413, text="too large")
        if not limiter.allow(client_ip(request)):
            return web.Response(status=429, text="too many")
        try:
            data = json.loads((await request.read()).decode("utf-8", "replace") or "{}")
        except ValueError:
            data = None
        if not isinstance(data, dict):
            return web.Response(status=400, text='send {"q": ...}')
        if guard.check(client_ip(request), data, ()):
            return web.Response(status=204)  # quietly ignored
        await run(record, tools.state, str(data.get("q") or ""), bool(data.get("zero")))
        return web.Response(status=204, headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"})

    app.router.add_post("/v1/search-log", handler)
