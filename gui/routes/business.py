"""Products, Marketing and Money tabs (Phases 200-203).

* ``GET /api/products``: the factory catalog (on sale, waiting, retired), what it makes next, the
  revenue leaderboard and products with checkouts but no sales. ``POST /api/products/make`` makes
  one product now. ``GET /api/products.csv`` downloads the catalog (Phase 237).
* ``GET /api/marketing``: drafts waiting for you (with their text and tracked link), the week's
  plan, strategy scores and the optimiser's notes. ``POST /api/marketing/mark`` records a draft as
  posted or skipped.
* ``GET /api/money``: the month's forecast, the offers and their links, sponsorships waiting for
  approval, affiliates and what you owe them, recent price changes. ``POST /api/sponsor/approve``
  puts a paid sponsorship live; ``POST /api/affiliate/add`` approves an affiliate and emails them
  their links.

Everything runs locally on this Mac (the panel only listens on 127.0.0.1); every POST needs the
session's CSRF token.
"""

from __future__ import annotations

import asyncio
from typing import Any

from aiohttp import web

from gui.context import ctx


def _tools(gctx: Any) -> Any:
    from tools import build_toolkit
    from tools.circuit_breaker import CircuitBreaker

    return build_toolkit(gctx.fresh_config(), gctx.state, CircuitBreaker(1000, 1000, 1000))


async def _run(fn: Any, *args: Any) -> Any:
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, headers={"Cache-Control": "no-store"})


async def _body(request: web.Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


# ------------------------------------------------------------------ Phase 200: products
def products(gctx: Any) -> dict[str, Any]:
    from strategies import product_factory as pf
    from strategies.marketing_optimizer import leaky
    from strategies.sales_channels import leaderboard

    state, cfg = gctx.state, gctx.fresh_config()
    cand = pf.next_candidate(state, cfg, peek=True) if cfg.product_factory else None
    from strategies.factory_cadence import describe as describe_pace

    return {"catalog": pf.catalog(state), "interval_minutes": int(cfg.factory_interval_seconds) // 60,
            "pace": describe_pace(state, cfg),
            "factory_on": bool(cfg.product_factory),
            "next": {"title": cand["title"], "rows": len(cand["rows"]) if cand.get("rows") else 0} if cand else None,
            "leaderboard": leaderboard(state)[:30], "leaky": leaky(state), "plan": _plan(state, cfg),
            "insights": _insights(state)}


def _insights(state: Any) -> dict[str, Any]:
    from strategies.factory_insights import describe, insights

    data = insights(state)
    return {**data, "lines": describe(data)}


def _plan(state: Any, cfg: Any) -> dict[str, Any]:
    from strategies.growth_plan import describe, plan

    p = plan(state, cfg)
    return {**p, "lines": describe(p)}


async def get_products(request: web.Request) -> web.Response:
    return _json(await _run(products, ctx(request)))


def catalog_list(gctx: Any, q: str = "", status: str = "") -> dict[str, Any]:
    """Phase 362: the catalog, searchable (title, slug, technology, type), at most 200 rows."""
    from strategies.catalog_insight import rows
    from strategies.product_controls import hidden, pinned

    state = gctx.state
    q = " ".join(str(q or "").lower().split())[:60]
    out = []
    unlisted = hidden(state)
    for r in reversed(rows(state)):
        if status and r["status"] != status:
            continue
        if q and q not in f"{r['title']} {r['slug']} {r['technology']} {r['type']}".lower():
            continue
        out.append({**{k: r[k] for k in ("slug", "title", "type", "technology", "status", "rows", "price_cents", "orders_90d",
                                         "revenue_cents_90d")},
                    "pinned": pinned(state, r["slug"]), "hidden": r["slug"] in unlisted})
        if len(out) >= 200:
            break
    return {"items": out}


async def get_catalog(request: web.Request) -> web.Response:
    return _json(await _run(catalog_list, ctx(request), request.query.get("q", ""), request.query.get("status", "")))


def trends_data(gctx: Any) -> dict[str, Any]:
    """Phase 360: the same numbers as the site's trends page."""
    from strategies.market_trends import trends
    from strategies.product_factory import label

    t = trends(gctx.state)
    def rows(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"tech": label(r["tech"]), "growth_pct": round(r["growth"] * 100), "before": r["before"], "recent": r["recent"]}
                for r in items[:15]]
    return {"rising": rows(t.get("rising") or []), "falling": rows(t.get("falling") or []),
            "families": {k: sum(v[-4:]) for k, v in (t.get("families") or {}).items()},
            "countries": [list(c) for c in t.get("countries") or []], "pairs": [list(p) for p in t.get("pairs") or []],
            "at": t.get("at", "")}


async def get_trends(request: web.Request) -> web.Response:
    return _json(await _run(trends_data, ctx(request)))


async def get_products_csv(request: web.Request) -> web.Response:
    """Phase 237: the whole catalog as a spreadsheet."""
    from strategies.catalog_insight import catalog_csv

    body = await _run(catalog_csv, ctx(request).state)
    return web.Response(text=body, content_type="text/csv", charset="utf-8",
                        headers={"Cache-Control": "no-store", "Content-Disposition": 'attachment; filename="catalog.csv"'})


async def post_action(request: web.Request) -> web.Response:
    """Phases 280-284: pin, retire, price, hide, rebuild one product."""
    from strategies.product_controls import apply

    data = await _body(request)
    gctx = ctx(request)
    try:
        message = await _run(apply, _tools(gctx), str(data.get("slug") or ""), str(data.get("action") or ""),
                             str(data.get("value") or ""))
    except ValueError as exc:
        return _json({"ok": False, "message": str(exc)}, 400)
    return _json({"ok": True, "message": message})


async def post_make(request: web.Request) -> web.Response:
    from strategies import product_factory as pf

    out = await _run(lambda: pf.tick(_tools(ctx(request)), force=True))
    made = out.get("made")
    return _json({"ok": bool(made), "message": f"Made {made['title']} ({out['status']})" if made else f"Nothing made: {out.get('why')}"})


# ------------------------------------------------------------------ Phase 201: marketing
def marketing(gctx: Any) -> dict[str, Any]:
    from strategies import marketing_engine as me
    from strategies import marketing_optimizer as mo

    state = gctx.state
    drafts = [{"id": p["id"], "strategy": me.PLAYBOOK.get(p["strategy"], {}).get("name", p["strategy"]), "title": p["title"],
               "body": p["body"], "link": p["link"] or "", "created_at": p["created_at"]} for p in me.queue(state, 50)]
    scores = sorted(({"key": k, **v} for k, v in me.scores(state).items()), key=lambda r: -r["revenue_cents"])
    return {"drafts": drafts, "plan": [{"date": d, "what": w} for d, w in me.calendar(state)], "scores": scores,
            "paused": list(me.paused(state)), "notes": mo.describe(state)}


async def get_marketing(request: web.Request) -> web.Response:
    return _json(await _run(marketing, ctx(request)))


async def post_mark(request: web.Request) -> web.Response:
    from strategies import marketing_engine as me

    data = await _body(request)
    status = {"posted": "posted", "skipped": "skipped"}.get(str(data.get("status")))
    try:
        play_id = int(data.get("id"))
    except (TypeError, ValueError):
        play_id = 0
    if not status or play_id <= 0:
        return _json({"ok": False, "message": "needs a draft id and status posted or skipped"}, 400)
    ok = await _run(me.mark, ctx(request).state, play_id, status)
    return _json({"ok": ok, "message": f"Draft {play_id} marked {status}." if ok else "That draft isn't waiting any more."})


# ------------------------------------------------------------------ Phase 202: money
def money(gctx: Any) -> dict[str, Any]:
    from strategies import revenue_models as rm
    from strategies import sales_channels as sc
    from strategies.money_insight import forecast, price_history

    state, cfg = gctx.state, gctx.fresh_config()
    offers = []
    for kind, aid in rm.offers(state).items():
        a = state.get_asset(int(aid)) or {}
        offers.append({"kind": kind, "title": rm.KINDS[kind][1], "url": a.get("checkout_url") or "", "status": a.get("status")})
    sponsors = [s for s in state.get(rm.SPONSORS) or [] if s.get("status") in ("pending", "live")]
    return {"forecast": forecast(state, cfg), "offers": offers, "sponsors": sponsors,
            "affiliates": sc.commissions(state, cfg), "prices": price_history(state, 10)}


async def get_money(request: web.Request) -> web.Response:
    return _json(await _run(money, ctx(request)))


async def post_sponsor(request: web.Request) -> web.Response:
    from strategies.revenue_models import approve_sponsor

    data = await _body(request)
    try:
        entry = await _run(approve_sponsor, ctx(request).state, int(data.get("id")), str(data.get("line") or ""),
                           str(data.get("url") or ""))
    except (TypeError, ValueError) as exc:
        return _json({"ok": False, "message": str(exc)}, 400)
    return _json({"ok": True, "message": f"Live until {entry['ends'][:16].replace('T', ' ')} UTC (after the next site build)."})


async def post_affiliate(request: web.Request) -> web.Response:
    from strategies import sales_channels as sc

    data = await _body(request)
    gctx = ctx(request)

    def add() -> dict[str, Any]:
        aff = sc.add_affiliate(gctx.state, str(data.get("email") or ""))
        tools = _tools(gctx)
        tools.dispatcher.send_transactional(sc.welcome_email(gctx.state, tools.config, aff), audit_key=f"affiliate:{aff['code']}")
        return aff

    try:
        aff = await _run(add)
    except ValueError as exc:
        return _json({"ok": False, "message": str(exc)}, 400)
    return _json({"ok": True, "message": f"{aff['email']} is affiliate {aff['code']}; their links are on the way."})


def routes() -> list[web.RouteDef]:
    return [web.get("/api/products", get_products), web.get("/api/products.csv", get_products_csv),
            web.get("/api/catalog", get_catalog), web.get("/api/trends", get_trends),
            web.post("/api/products/make", post_make), web.post("/api/products/action", post_action),
            web.get("/api/marketing", get_marketing), web.post("/api/marketing/mark", post_mark),
            web.get("/api/money", get_money), web.post("/api/sponsor/approve", post_sponsor),
            web.post("/api/affiliate/add", post_affiliate)]
