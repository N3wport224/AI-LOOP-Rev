"""Signature-verified Stripe webhook listener with instant fulfilment.

Flow for each POST to ``webhook_path``:

1. **Verify** with ``stripe.Webhook.construct_event`` (HMAC-SHA256 over ``"{t}.{payload}"`` with
   ``STRIPE_WEBHOOK_SECRET``; timestamps older than ``webhook_tolerance_seconds`` are rejected,
   which blocks replays of captured requests). If the ``stripe`` package isn't installed, an
   equivalent built-in verifier runs the same checks.
2. **Deduplicate** by event id (``webhook_events`` table). Stripe delivers at least once, so
   repeats get a 200 and are not processed again.
3. **Reconcile** the Checkout Session: ``payment_link`` (or ``metadata.asset_id``) → asset,
   ``customer_details.email`` → buyer. The order id is the session id, the same id the polling
   sync uses, so webhook and polling can never double-count a sale.
4. **Record** verified net revenue immediately (daily goal updates on the spot).
5. **Fulfil**: the zip + receipt are emailed right after the 200 response goes back, in a
   background executor so Stripe never waits on SMTP. Failures stay ``paid`` and the engine's
   ``deliver_orders`` sweep retries them.

Events handled:

* ``checkout.session.completed``: paid now → record + fulfil; delayed payment methods → pending.
* ``checkout.session.async_payment_succeeded``: the pending session is now paid.
* ``payment_intent.succeeded``: completes a pending session with that payment intent. A
  PaymentIntent on its own has no Payment Link, email or dataset, so it never creates an order.
* ``checkout.session.expired``: an abandoned checkout, counted for drop-off metrics.
* Subscriptions: a completed session in ``mode=subscription`` activates the subscriber and sends
  the welcome dataset; ``invoice.paid`` records each billing period's revenue;
  ``customer.subscription.updated/deleted`` keep ``subscription_status`` current.

Every session's ``client_reference_id`` is decoded into an acquisition channel (see
``tools/attribution.py``) and stored with the session and the order.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from tools.attribution import decode_ref
from tools.storefront import Order

if TYPE_CHECKING:  # pragma: no cover
    from tools import Toolkit

log = logging.getLogger("automonetize.webhook")

MAX_BODY_BYTES = 512 * 1024
try:  # AppKey must be created at import time: it inspects the defining module's frame
    from aiohttp import web as _web

    _PENDING_KEY: Any = _web.AppKey("pending_fulfilment", set)
except ImportError:  # pragma: no cover - aiohttp is only needed to serve
    _PENDING_KEY = "pending_fulfilment"


def pending_key() -> Any:
    return _PENDING_KEY
HANDLED = {
    "checkout.session.completed",
    "checkout.session.async_payment_succeeded",
    "checkout.session.expired",
    "payment_intent.succeeded",
    "invoice.paid",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "automonetize.handshake",
    "invoice.payment_failed",
}
# Sent by ``automonetize setup-autonomous`` through the public tunnel URL, signed with the webhook
# secret: proves the path Stripe → Cloudflare → tunnel → this listener works end to end.
HANDSHAKE_TYPE = "automonetize.handshake"


class SignatureError(Exception):
    """The payload is not a genuine, fresh Stripe event."""


def sign_payload(payload: bytes | str, secret: str, timestamp: int | None = None) -> str:
    """Build a ``Stripe-Signature`` header (used by tests and ``automonetize webhook --selftest``)."""
    body = payload.decode() if isinstance(payload, bytes) else payload
    t = int(time.time()) if timestamp is None else timestamp
    sig = hmac.new(secret.encode(), f"{t}.{body}".encode(), hashlib.sha256).hexdigest()
    return f"t={t},v1={sig}"


def _builtin_verify(payload: bytes, sig_header: str, secret: str, tolerance: int, now: float) -> dict[str, Any]:
    try:
        items = [part.split("=", 1) for part in sig_header.split(",")]
        timestamp = int(next(v for k, v in items if k == "t"))
        signatures = [v for k, v in items if k == "v1"]
    except (StopIteration, ValueError) as exc:
        raise SignatureError("malformed Stripe-Signature header") from exc
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, s) for s in signatures):
        raise SignatureError("no signatures found matching the expected signature for payload")
    if tolerance and timestamp < now - tolerance:
        raise SignatureError("timestamp outside the tolerance zone")
    return json.loads(payload)


def verify_event(payload: bytes, sig_header: str | None, secret: str, tolerance: int = 300,
                 now: Callable[[], float] = time.time, use_sdk: bool = True) -> dict[str, Any]:
    if not sig_header:
        raise SignatureError("missing Stripe-Signature header")
    if not secret:
        raise SignatureError("STRIPE_WEBHOOK_SECRET is not configured")
    if use_sdk:
        try:
            import stripe
        except ImportError:
            stripe = None  # type: ignore[assignment]
        if stripe is not None:
            try:
                event = stripe.Webhook.construct_event(payload, sig_header, secret, tolerance=tolerance)
            except stripe.SignatureVerificationError as exc:
                raise SignatureError(str(exc)) from exc
            except ValueError as exc:
                raise SignatureError(f"invalid payload: {exc}") from exc
            return event.to_dict() if hasattr(event, "to_dict") else json.loads(payload)
    try:
        return _builtin_verify(payload, sig_header, secret, tolerance, now())
    except json.JSONDecodeError as exc:
        raise SignatureError(f"invalid payload: {exc}") from exc


@dataclass
class WebhookOutcome:
    status: int
    body: dict[str, Any]
    fulfil: list[dict[str, Any]] = field(default_factory=list)  # orders to deliver after responding
    after: list[Callable[[], Any]] = field(default_factory=list)  # other jobs to run after responding


def _iso(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return ""


class WebhookProcessor:
    """Framework-free core: bytes + header in, HTTP status + orders to fulfil out."""

    def __init__(self, tools: "Toolkit", secret: str | None = None, tolerance: int | None = None,
                 now: Callable[[], float] = time.time, use_sdk: bool = True):
        self.tools = tools
        self.secret = tools.config.stripe_webhook_secret if secret is None else secret
        self.tolerance = tools.config.webhook_tolerance_seconds if tolerance is None else tolerance
        self.now = now
        self.use_sdk = use_sdk

    # -- entry point --------------------------------------------------------------
    def handle(self, payload: bytes, sig_header: str | None) -> WebhookOutcome:
        state = self.tools.state
        if not self.secret:
            return WebhookOutcome(503, {"error": "webhook secret not configured"})
        if len(payload) > MAX_BODY_BYTES:
            return WebhookOutcome(413, {"error": "payload too large"})
        try:
            event = verify_event(payload, sig_header, self.secret, self.tolerance, self.now, self.use_sdk)
        except SignatureError as exc:
            state.log_error("webhook", f"rejected: {exc}", kind="security")
            return WebhookOutcome(400, {"error": "invalid signature"})

        event_id, event_type = str(event.get("id", "")), str(event.get("type", ""))
        if not event_id:
            return WebhookOutcome(400, {"error": "event without id"})
        if event_type not in HANDLED:
            return WebhookOutcome(200, {"received": True, "ignored": event_type})
        if not state.claim_webhook_event(event_id, event_type):
            return WebhookOutcome(200, {"received": True, "duplicate": True})
        after: list[Callable[[], Any]] = []  # per call: handle() runs concurrently in executor threads
        try:
            obj = (event.get("data") or {}).get("object") or {}
            status, detail, fulfil = self._dispatch(event_type, obj, after)
        except Exception as exc:  # noqa: BLE001 - let Stripe retry: un-claim and answer 500
            state.release_webhook_event(event_id)
            state.log_error("webhook", f"{event_type} {event_id} failed: {exc!r}")
            return WebhookOutcome(500, {"error": "processing failed"})
        if (event_type in ("checkout.session.completed", "checkout.session.async_payment_succeeded") and status == "processed"
                and obj.get("payment_status") in ("paid", "no_payment_required")):
            from tools.copy_bandit import record_purchase_from_ref

            record_purchase_from_ref(state, obj.get("client_reference_id"), state.clock())  # credit the copy that sold
        state.finish_webhook_event(event_id, status, detail)
        state.set("webhook_last_event", {"id": event_id, "type": event_type, "at": state.now(), "status": status})
        return WebhookOutcome(200, {"received": True, "status": status, "detail": detail}, fulfil, after)

    # -- event handlers -------------------------------------------------------------
    def _dispatch(self, event_type: str, obj: dict[str, Any],
                  after: list[Callable[[], Any]]) -> tuple[str, str, list[dict[str, Any]]]:
        if event_type == HANDSHAKE_TYPE:
            nonce = str(obj.get("nonce", ""))[:64]
            self.tools.state.set(f"handshake:{nonce}", {"received_at": self.tools.state.now(), "via": obj.get("via", "")})
            return "processed", f"handshake {nonce}", []
        if event_type == "payment_intent.succeeded":
            return self._payment_intent_succeeded(obj)
        if event_type == "invoice.payment_failed":
            from strategies.retention_engine import on_payment_failed

            detail, reminder = on_payment_failed(self.tools, obj)
            if reminder is not None:
                after.append(reminder)  # the email goes out after Stripe gets its 200
            return "processed", f"invoice {obj.get('id')} failed: {detail}", []
        if event_type == "invoice.paid":
            from strategies.subscription_engine import record_invoice, restore_api_access

            from strategies.retention_engine import on_paid

            new = record_invoice(self.tools, obj)
            restore_api_access(self.tools, obj)
            on_paid(self.tools, obj)
            return ("processed" if new else "ignored"), f"invoice {obj.get('id')} {'recorded' if new else 'already recorded or not a subscription'}", []
        if event_type.startswith("customer.subscription."):
            from strategies.subscription_engine import apply_subscription_update

            if event_type.endswith("deleted"):
                obj = {**obj, "status": "canceled"}
            sid = apply_subscription_update(self.tools, obj)
            if sid and event_type.endswith("deleted"):
                from strategies.retention_engine import on_canceled

                on_canceled(self.tools, obj)
            return ("processed" if sid else "ignored"), f"subscription {obj.get('id')} → {obj.get('status')}", []
        self._remember_session(obj)
        if event_type == "checkout.session.completed" and obj.get("mode") == "subscription":
            from strategies.subscription_engine import activate_from_session, send_welcome

            from api.auth import is_api_subscriber, provision

            sid, created = activate_from_session(self.tools, obj)
            if sid and is_api_subscriber(self.tools.state, self.tools.state.subscriber(sid)):
                after.append(lambda: provision(self.tools, sid))  # idempotent: one key per subscriber
            elif sid and created:
                after.append(lambda: send_welcome(self.tools, sid))
            return "processed", f"subscriber {obj.get('subscription')} {'activated' if created else 'updated'}", []
        if event_type == "checkout.session.expired":
            return "processed", "checkout abandoned", []
        if event_type == "checkout.session.completed" and obj.get("payment_status") not in ("paid", "no_payment_required"):
            return "processed", "awaiting asynchronous payment", []
        return self._record_paid(obj)

    def _remember_session(self, s: dict[str, Any]) -> None:
        channel, campaign = decode_ref(s.get("client_reference_id"))
        self.tools.state.upsert_checkout_session(
            session_id=str(s.get("id")),
            product_ref=s.get("payment_link"),
            payment_intent=s.get("payment_intent") if isinstance(s.get("payment_intent"), str) else (s.get("payment_intent") or {}).get("id"),
            status=str(s.get("status") or "open"),
            payment_status=s.get("payment_status"),
            email=(s.get("customer_details") or {}).get("email") or s.get("customer_email"),
            amount_cents=s.get("amount_total"),
            channel=channel, campaign=campaign, mode=s.get("mode"),
        )

    def _payment_intent_succeeded(self, pi: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
        session = self.tools.state.session_for_payment_intent(str(pi.get("id")))
        if session is None:
            return "ignored", "no checkout session for this payment intent", []
        if self.tools.state.get_order("stripe", session["session_id"]):
            return "ignored", "order already recorded from the checkout session", []
        return self._record_paid({
            "id": session["session_id"],
            "payment_link": session["product_ref"],
            "customer_details": {"email": session["email"]},
            "amount_total": session["amount_cents"] or pi.get("amount_received") or 0,
            "created": pi.get("created"),
            "metadata": pi.get("metadata") or {},
            "client_reference_id": None,
            "_channel": session.get("channel"), "_campaign": session.get("campaign"),
        })

    def _record_paid(self, s: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
        tools = self.tools
        session_id = str(s.get("id"))
        meta = s.get("metadata") or {}
        asset_hint = int(meta["asset_id"]) if str(meta.get("asset_id", "")).isdigit() else None
        channel, campaign = decode_ref(s.get("client_reference_id"))
        if s.get("_channel"):
            channel, campaign = s["_channel"], s.get("_campaign") or ""
        order = Order(
            provider="stripe",
            order_id=session_id,
            email=(s.get("customer_details") or {}).get("email") or s.get("customer_email"),
            gross_cents=int(s.get("amount_total") or 0),
            product_ref=s.get("payment_link"),
            occurred_at=_iso(s.get("created")) or tools.state.now(),
            asset_id=asset_hint, channel=channel, campaign=campaign,
            meta={k: str(meta[k]) for k in ("kind", "company_id") if meta.get(k)} or None,
        )
        stripe_sf = next((sf for sf in tools.storefronts if sf.name == "stripe"), None)
        fee_pct = stripe_sf.fee_pct if stripe_sf else tools.config.stripe_fee_pct
        fee_fixed = stripe_sf.fee_fixed_cents if stripe_sf else tools.config.stripe_fee_fixed_cents
        report = tools.revenue.record_orders([order], fee_pct, fee_fixed)
        row = tools.state.get_order("stripe", session_id)
        if report.new:
            summary = tools.revenue.daily_summary()
            tools.state.set("last_sale", {"order": session_id, "net_cents": report.net_cents_added, "at": tools.state.now(),
                                          "today_net_cents": summary["net_cents"], "target_met": summary["target_met"]})
            log.info("sale %s: +$%.2f net (today $%.2f/%s)", session_id, report.net_cents_added / 100,
                     summary["net_cents"] / 100, f"${summary['target_cents'] / 100:.2f}")
        fulfil = [row] if row and row["status"] == "paid" else []
        detail = f"order {session_id} {'recorded' if report.new else 'already recorded'}" + (
            "" if row and row["asset_id"] else " (no matching dataset: manual delivery)"
        )
        return "processed", detail, fulfil


# ---------------------------------------------------------------------------- aiohttp server
def build_app(processor: WebhookProcessor, path: str = "/webhook", fulfil: Callable[[dict[str, Any]], Any] | None = None,
              executor: ThreadPoolExecutor | None = None, power: Any = None):
    from aiohttp import web

    from agent import power as power_mod
    from strategies.distribution_engine import fulfil_order

    power = power or power_mod.shared(processor.tools.config)

    def held(fn: Callable[..., Any], *args: Any) -> Any:
        # Verification, recording and fulfilment keep the Mac awake until the order is delivered.
        with power.hold("webhook"):
            return fn(*args)

    executor = executor or ThreadPoolExecutor(max_workers=4, thread_name_prefix="webhook")
    fulfil = fulfil or (lambda order: fulfil_order(processor.tools, order))
    pending: set[asyncio.Future] = set()

    async def webhook(request: web.Request) -> web.Response:
        if request.content_length and request.content_length > MAX_BODY_BYTES:
            return web.json_response({"error": "payload too large"}, status=413)
        payload = await request.read()
        loop = asyncio.get_running_loop()
        outcome = await loop.run_in_executor(executor, held, processor.handle, payload, request.headers.get("Stripe-Signature"))
        jobs = [lambda order=order: fulfil(order) for order in outcome.fulfil] + list(outcome.after)
        for job in jobs:
            fut = loop.run_in_executor(executor, held, job)
            pending.add(fut)
            fut.add_done_callback(pending.discard)
        return web.json_response(outcome.body, status=outcome.status)

    leads = None
    if processor.tools.config.lead_magnet_enabled:
        from strategies.lead_magnet import LeadEndpoints

        leads = LeadEndpoints(processor.tools)

    def client_ip(request: web.Request) -> str:
        # Behind the tunnel every request comes from 127.0.0.1; cloudflared passes the visitor's
        # address in CF-Connecting-IP. Only trust it from loopback (i.e. from cloudflared).
        peer = request.remote or ""
        if peer in ("127.0.0.1", "::1"):
            return request.headers.get("CF-Connecting-IP", peer)[:64]
        return peer

    def page_response(page: Any) -> web.Response:
        resp = web.Response(status=page.status, text=page.body, content_type=page.content_type)
        resp.headers.update(page.headers)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        if page.after is not None:
            fut = asyncio.get_running_loop().run_in_executor(executor, held, page.after)
            pending.add(fut)
            fut.add_done_callback(pending.discard)
        return resp

    async def lead_capture(request: web.Request) -> web.Response:
        if request.content_length and request.content_length > 8192:
            return web.Response(status=413, text="payload too large")
        wants_json = "application/json" in request.headers.get("Content-Type", "")
        try:
            data = await request.json() if wants_json else dict(await request.post())
        except (ValueError, UnicodeDecodeError):
            return web.Response(status=400, text="bad request")
        form = {k: str(v)[:300] for k, v in (data or {}).items() if isinstance(k, str)}
        page = await asyncio.get_running_loop().run_in_executor(executor, held, leads.capture, form, client_ip(request), wants_json)
        return page_response(page)

    def lead_action(action: str):
        async def handler(request: web.Request) -> web.Response:
            loop = asyncio.get_running_loop()
            if request.method == "GET":
                page = await loop.run_in_executor(executor, held, leads.action_page, action, request.query.get("t", "")[:100])
                return page_response(page)
            form = dict(await request.post()) if request.content_type != "application/json" else {}
            token = str(form.get("t") or request.query.get("t", ""))[:100]
            page = await loop.run_in_executor(executor, held, getattr(leads, action), token)
            return page_response(page)
        return handler

    async def health(request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "events": processor.tools.state.webhook_event_counts()})

    async def drain(app: web.Application) -> None:
        if pending:
            await asyncio.wait(list(pending), timeout=30)

    async def release_executor(app: web.Application) -> None:
        # Fulfilment has drained; without this every webhook restart leaked its worker threads.
        await asyncio.get_running_loop().run_in_executor(None, executor.shutdown, True)

    app = web.Application(client_max_size=MAX_BODY_BYTES)
    app.router.add_post(path, webhook)
    app.router.add_get("/healthz", health)
    if leads is not None:
        app.router.add_post("/lead-magnet/capture", lead_capture)
        for action in ("confirm", "unsubscribe"):
            app.router.add_route("GET", f"/lead-magnet/{action}", lead_action(action))
            app.router.add_route("POST", f"/lead-magnet/{action}", lead_action(action))
    if processor.tools.config.copy_bandit_enabled:
        from strategies.lead_magnet import CaptureLimiter
        from tools.copy_bandit import handle_beacon

        beacon_limiter = CaptureLimiter(600)

        async def beacon(request: web.Request) -> web.Response:
            if request.content_length and request.content_length > 1024:
                return web.Response(status=413)
            if not beacon_limiter.allow(client_ip(request)):
                return web.Response(status=429)
            body = await request.read()
            status, _ = await asyncio.get_running_loop().run_in_executor(
                executor, handle_beacon, processor.tools.state, body, processor.tools.state.clock())
            return web.Response(status=status, headers={"Cache-Control": "no-store"})

        app.router.add_post("/t/e", beacon)
    # Public commerce routes under /v1 (registered before the API's /v1 catch-all).
    from strategies import dossier_engine
    from tools.storefront import recovery_endpoint

    async def run_in_pool(fn: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(executor, held, fn, *args)

    recovery_endpoint.mount(app, processor.tools, run_in_pool, client_ip, executor, held, pending)

    async def download(request: web.Request) -> web.StreamResponse:
        from tools import download_links

        path, why = await run_in_pool(download_links.redeem, processor.tools.state, processor.tools.files,
                                      request.match_info.get("token", ""))
        if path is None:
            return web.Response(status=404, text=why, headers={"Cache-Control": "no-store"})
        return web.FileResponse(path, headers={"Content-Disposition": f'attachment; filename="{path.name}"',
                                               "Cache-Control": "no-store", "X-Robots-Tag": "noindex"})

    app.router.add_get("/d/{token}", download)

    async def rating(request: web.Request) -> web.Response:
        from strategies import ratings

        token = request.match_info.get("token", "")
        try:
            score = int(request.match_info.get("score", "0"))
        except ValueError:
            score = 0
        state, cfg = processor.tools.state, processor.tools.config
        if request.method == "GET":  # a confirm button: link scanners don't vote
            status, body = await run_in_pool(lambda: ratings.confirm_page(cfg, token, score, ratings.lookup(state, token)))
        else:
            outcome = await run_in_pool(ratings.record, state, token, score)
            status, body = ratings.thanks_page(cfg, score, outcome)
        return web.Response(status=status, text=body, content_type="text/html",
                            headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})

    app.router.add_route("GET", "/r/{token}/{score}", rating)
    app.router.add_route("POST", "/r/{token}/{score}", rating)
    dossier_engine.mount(app, processor.tools, run_in_pool, client_ip)
    if processor.tools.config.api_enabled:
        from api.server import mount

        mount(app, processor.tools, executor, held, client_ip)
    app.on_shutdown.append(drain)
    app.on_cleanup.append(release_executor)
    app[pending_key()] = pending
    return app


class WebhookServer:
    """Runs the aiohttp app on its own event loop; ``run(stop_event)`` blocks until the event is set."""

    def __init__(self, tools: "Toolkit", host: str | None = None, port: int | None = None, path: str | None = None,
                 power: Any = None):
        cfg = tools.config
        self.power = power
        self.tools = tools
        self.host = host or cfg.webhook_host
        self.port = cfg.webhook_port if port is None else port
        self.path = path or cfg.webhook_path
        self.processor = WebhookProcessor(tools)
        self.started = threading.Event()
        self.bound_port: int | None = None
        self.sock: Any = None  # a listening socket owned by the supervisor (kept open across reloads)

    def run(self, stop_event: threading.Event) -> None:
        asyncio.run(self._serve(stop_event))

    async def _serve(self, stop_event: threading.Event) -> None:
        from aiohttp import web

        runner = web.AppRunner(build_app(self.processor, self.path, power=self.power), access_log=None)
        await runner.setup()
        # A dup of the supervisor's socket: closing it on shutdown leaves the original listening,
        # so a reload never closes the port.
        site = web.SockSite(runner, self.sock.dup()) if self.sock is not None else web.TCPSite(runner, self.host, self.port)
        await site.start()
        sockets = getattr(site._server, "sockets", None) or []  # noqa: SLF001 - to report an ephemeral port
        self.bound_port = sockets[0].getsockname()[1] if sockets else self.port
        log.info("webhook listener on http://%s:%s%s", self.host, self.bound_port, self.path)
        self.started.set()
        try:
            while not stop_event.is_set():
                await asyncio.sleep(0.2)
        finally:
            await runner.cleanup()  # runs on_shutdown: waits for in-flight fulfilment
