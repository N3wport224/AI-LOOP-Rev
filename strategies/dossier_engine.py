"""$49 Executive Migration Dossier: product, checkout, instant delivery (Phase 9).

* ``publish_dossier_tier``: one Stripe Product + one-off Price ($49), registered once as an asset
  of kind ``dossier``. There's no Payment Link per company. Instead,
  ``GET /v1/dossiers/{company_id}/buy`` creates a Checkout Session for that company, with
  ``metadata.kind = dossier`` and ``metadata.company_id``, and redirects the buyer to Stripe.
  That link is what the API (``dossier_url``) and the Weekly Tech Pulse point to.
* On ``checkout.session.completed`` the webhook records the order (the metadata attributes it to
  the dossier asset). Fulfilment builds the dossier for that company from the latest data and
  emails the PDF and Markdown. Delivered files are kept under ``data/dossiers/``, so order
  recovery can re-send exactly what was bought.
* Only companies with ``max(urgency, intent) > dossier_min_score`` are sold: the buy link answers
  404 for anyone else, so a thin dossier is never charged for.
"""

from __future__ import annotations

import json
from datetime import timezone
from pathlib import Path
from typing import Any

from strategies.base import Strategy, TaskContext, TaskResult
from strategies.subscription_engine import stripe_storefront
from tools.attribution import encode_ref
from tools.dispatcher import Attachment, Email
from tools.dossier_builder import build, eligible, to_markdown, to_pdf

DOSSIER_KIND = "dossier"


def dossier_asset(state: Any) -> dict[str, Any] | None:
    return next((a for a in state.list_assets() if a["kind"] == DOSSIER_KIND and a.get("status") == "published"), None)


def raw_records(tools: Any, company_id: str) -> dict[str, dict[str, Any]]:
    """The company's raw radar record in each niche (tech_stack categories and evidence live there)."""
    from api.data import company_id as cid

    root = tools.files.resolve("exports/intel")
    out = {}
    for path in sorted(root.glob("*/tech_radar.json")) if root.exists() else []:
        try:
            rows = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        for r in rows:
            if cid(r.get("company", "")) == company_id:
                out[path.parent.name] = r
    return out


def lookup(tools: Any, company_id: str) -> dict[str, Any] | None:
    from api.data import SignalIndex

    return SignalIndex(tools.files, tools.state).company(company_id, tools.state.clock())


def dossier_link(config: Any, company_id: str, source: str = "api") -> str:
    base = config.lead_capture_base
    return f"{base}/v1/dossiers/{company_id}/buy?src={source}" if base else ""


def offer_for(tools: Any, company: dict[str, Any], source: str = "api") -> dict[str, Any]:
    """The upsell snippet for API responses and emails."""
    live = dossier_asset(tools.state) is not None and bool(tools.config.lead_capture_base)
    ok = live and eligible(company, tools.config.dossier_min_score)
    return {"dossier_available": ok, "dossier_url": dossier_link(tools.config, company["company_id"], source) if ok else None,
            "dossier_price_cents": tools.config.dossier_price_cents if ok else None}


def create_checkout(tools: Any, company_id: str, source: str = "api", email: str = "") -> str | None:
    """A Checkout Session for one company's dossier. Returns the Stripe URL, or None if not for sale."""
    asset = dossier_asset(tools.state)
    company = lookup(tools, company_id)
    sf = stripe_storefront(tools)
    if asset is None or company is None or sf is None or not eligible(company, tools.config.dossier_min_score):
        return None
    price_id = json.loads(asset.get("kind_meta") or "{}").get("price_id")
    base = tools.config.lead_capture_base
    meta = {"kind": DOSSIER_KIND, "company_id": company_id, "asset_id": str(asset["id"])}
    session = sf.client.create_checkout_session(
        price_id, success_url=f"{base}/v1/dossiers/{company_id}/thanks", cancel_url=tools.config.pages_base_url or f"{base}/docs/api",
        metadata=meta, key=f"dossier-{company_id}-{tools.state.clock().strftime('%Y%m%d%H%M%S%f')}",
        client_reference_id=encode_ref(source, "dossier"), customer_email=email or "",
    )
    return session.get("url")


def produce(tools: Any, company_id: str) -> tuple[dict[str, Any], bytes, str] | None:
    company = lookup(tools, company_id)
    if company is None:
        return None
    now = tools.state.clock().astimezone(timezone.utc)
    content = build(company, raw_records(tools, company_id), now, tools.config.site_title)
    return content, to_pdf(content), to_markdown(content)


def fulfil_dossier(tools: Any, order: dict[str, Any], asset: dict[str, Any]) -> str:
    """Build and email the dossier for a paid order. Called by ``fulfil_order``."""
    meta = json.loads(order.get("meta") or "{}")
    company_id = meta.get("company_id")
    made = produce(tools, company_id) if company_id else None
    if made is None:
        tools.state.log_error("dossier", f"order {order['order_id']}: company {company_id!r} not in the dataset")
        return "manual"
    content, pdf, md = made
    stamp = content["generated_at"][:10]
    rel = f"dossiers/{company_id}-{stamp}-{order['id']}"
    tools.files.write_bytes(rel + ".pdf", pdf)
    tools.files.write_text(rel + ".md", md)
    email = Email(
        to=order["email"], kind="delivery", subject=f"Your dossier: {content['company']} ({stamp})",
        body=(f"Hi,\n\nYour Executive Migration Dossier on {content['company']} is attached (PDF, plus Markdown for your notes).\n\n"
              f"Why now: {content['why_now']}\n\nIt reflects the company's public postings as of {stamp}. Reply to this email if "
              f"anything looks off.\n\n{tools.config.sender_name or 'AutoMonetize'}"),
        attachments=[Attachment(f"{company_id}-dossier-{stamp}.pdf", pdf, "application/pdf"),
                     Attachment(f"{company_id}-dossier-{stamp}.md", md.encode(), "text/markdown")],
    )
    outcome = tools.dispatcher.send_transactional(email, audit_key=f"order:{order['id']}")
    tools.state._exec("UPDATE orders SET meta = ? WHERE id = ?", (json.dumps({**meta, "file": rel}), order["id"]))
    return outcome


class DossierEngine(Strategy):
    name = "dossier_engine"
    tasks = ("publish_dossier_tier",)

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        tools, cfg = ctx.tools, ctx.tools.config
        if cfg.dossier_price_cents <= 0:
            return TaskResult(True, "dossier tier disabled", {"published": False})
        existing = dossier_asset(tools.state)
        if existing:
            return TaskResult(True, "dossier tier live", {"published": False, "live": True})
        sf = stripe_storefront(tools)
        if sf is None:
            return TaskResult(True, "dossiers need a Stripe secret key", {"published": False})
        if not cfg.lead_capture_base:
            return TaskResult(True, "not selling dossiers until the buy link is public (tunnel)", {"published": False, "blocked": "tunnel"})
        if not (tools.dispatcher.can_deliver() or cfg.allow_manual_fulfillment):
            return TaskResult(True, "not selling dossiers until email delivery works", {"published": False, "blocked": "email"})
        title = "Executive Migration Dossier"
        product = sf.client.create_product(
            title, "One company, briefed: technology footprint, legacy systems, migration path, hiring activity by "
                   "department and recommended pitch angles, from its live job postings. PDF, delivered instantly.",
            {"kind": DOSSIER_KIND}, f"automonetize-dossier-{cfg.dossier_price_cents}-product")
        price = sf.client.create_price(product["id"], cfg.dossier_price_cents, cfg.currency,
                                       f"automonetize-dossier-{cfg.dossier_price_cents}-price")
        aid = tools.state.add_asset(ctx.hypothesis["id"], DOSSIER_KIND, title, "dossiers/", 1, 0, cfg.dossier_price_cents,
                                    product_ref=f"price:{price['id']}")
        tools.state.update_asset(aid, provider="stripe", status="published", checkout_url=f"{cfg.lead_capture_base}/v1/dossiers",
                                 kind_meta=json.dumps({"price_id": price["id"], "product_id": product["id"]}))
        return TaskResult(True, f"dossier tier live at ${cfg.dossier_price_cents / 100:.2f} per company", {"published": True})


# ----------------------------------------------------------------------------- HTTP
def mount(app: Any, tools: Any, run: Any, client_ip: Any) -> None:
    """``GET /v1/dossiers/{company_id}/buy`` (→ 303 to Stripe) and ``/thanks``. Public, rate-limited."""
    from aiohttp import web

    from strategies.lead_magnet import CaptureLimiter

    limiter = CaptureLimiter(20)

    def page(title: str, text: str, status: int = 200) -> web.Response:
        import html

        return web.Response(status=status, content_type="text/html", headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"},
                            text=(f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                                  f"<title>{html.escape(title)}</title><body style='font-family:system-ui;max-width:560px;margin:3rem auto;"
                                  f"padding:0 1rem'><h1>{html.escape(title)}</h1><p>{html.escape(text)}</p></body>"))

    async def buy(request: web.Request) -> web.StreamResponse:
        cid = request.match_info["company_id"]
        if not limiter.allow(client_ip(request)):
            return page("Too many requests", "Please try again later.", 429)
        if not all(c.isalnum() or c == "-" for c in cid) or len(cid) > 120:
            return page("Not found", "No such company.", 404)
        src = "".join(c for c in request.query.get("src", "api") if c.isalnum() or c == "_")[:20] or "api"
        email = request.query.get("email", "")[:254]
        try:
            url = await run(create_checkout, tools, cid, src, email if "@" in email else "")
        except Exception as exc:  # noqa: BLE001 - Stripe down: say so, don't 500
            tools.state.log_error("dossier", f"checkout for {cid} failed: {exc!r}")
            return page("Checkout unavailable", "We couldn't open checkout just now. Please try again in a minute.", 503)
        if not url:
            return page("Not available", "A dossier isn't available for this company.", 404)
        raise web.HTTPSeeOther(url)

    async def thanks(request: web.Request) -> web.Response:
        return page("Thank you", "Your dossier is being generated and will arrive by email within a minute or two.")

    app.router.add_get("/v1/dossiers/{company_id}/buy", buy)
    app.router.add_get("/v1/dossiers/{company_id}/thanks", thanks)


def delivered_file(tools: Any, order: dict[str, Any]) -> Path | None:
    rel = json.loads(order.get("meta") or "{}").get("file")
    return tools.files.resolve(rel + ".pdf") if rel and tools.files.exists(rel + ".pdf") else None
