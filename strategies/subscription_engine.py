"""Recurring subscription tier: weekly delta updates for $10/month (Stripe).

Lifecycle:

1. ``publish_subscription``: once a niche's one-off dataset is live on Stripe, create a
   recurring Price (``recurring[interval]=month``) and Payment Link for "weekly updates" and
   store it as a ``subscription`` asset. Landers show it next to the one-off price.
2. **Activation**: a ``checkout.session.completed`` in ``mode=subscription`` (webhook, or the
   polling sweep) creates the subscriber with ``subscription_status="active"`` and emails the
   current full dataset as a welcome.
3. **Revenue**: every paid invoice (``invoice.paid``, or polled ``/v1/invoices``) is one verified
   revenue event, idempotent by invoice id. The session itself records no revenue: its
   amount *is* the first invoice.
4. **Status**: ``customer.subscription.updated/deleted`` (or polled ``/v1/subscriptions/:id``)
   keeps ``subscription_status`` current (active, past_due, canceled...). Only active or
   trialing subscribers get deliveries.
5. ``deliver_subscriptions``: every week from Monday ``subscription_delivery_hour`` (in
   ``subscription_timezone``) on, each active subscriber gets that week's package once: the
   delta (companies new, or with new roles, in the last 7 days), plus the full current radar.
   A missed Monday (laptop asleep) catches up later the same week.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from strategies.base import Strategy, TaskContext, TaskResult
from strategies.digital_asset_packager import ASSET_KIND, niche_title
from strategies.tech_stack_intel import INTEL_FIELDS, _norm_company
from tools.attribution import decode_ref
from tools.dispatcher import Attachment, Email
from tools.storefront import Order

SUB_KIND = "subscription"
DELIVERABLE = ("active", "trialing")
STRIPE_STATUSES = {"active", "trialing", "past_due", "canceled", "unpaid", "incomplete", "incomplete_expired", "paused"}


def _ts(value: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat(timespec="seconds") if value else None
    except (TypeError, ValueError, OSError):
        return None


def _id(value: Any) -> str | None:
    """Stripe fields are either an id string or an expanded object."""
    if isinstance(value, dict):
        return value.get("id")
    return value or None


def stripe_storefront(tools):
    sf = next((s for s in tools.storefronts if s.name == "stripe"), None)
    return sf if sf is not None and tools.config.stripe_secret_key else None


def subscription_asset(state, niche: str) -> dict[str, Any] | None:
    return next((a for a in state.list_assets() if a["kind"] == SUB_KIND and a.get("niche") == niche and a.get("checkout_url")), None)


def interval_label(interval: str) -> str:
    return {"month": "Monthly", "week": "Weekly", "year": "Yearly"}.get(interval, interval.title())


# ----------------------------------------------------------------------------- shared handlers
def activate_from_session(tools, session: dict[str, Any]) -> tuple[int | None, bool]:
    """Create (or refresh) the subscriber behind a subscription-mode Checkout Session."""
    sub_id = _id(session.get("subscription"))
    if not sub_id:
        return None, False
    state = tools.state
    asset = state.asset_for_product(session["payment_link"]) if session.get("payment_link") else None
    meta = session.get("metadata") or {}
    channel, campaign = decode_ref(session.get("client_reference_id"))
    interval = json.loads(asset.get("kind_meta") or "{}").get("interval") if asset else None
    paid = session.get("payment_status") in ("paid", "no_payment_required")
    return state.upsert_subscriber(
        "stripe", sub_id,
        customer_id=_id(session.get("customer")),
        email=(session.get("customer_details") or {}).get("email") or session.get("customer_email"),
        niche=(asset or {}).get("niche") or meta.get("niche"),
        asset_id=(asset or {}).get("id"), hypothesis_id=(asset or {}).get("hypothesis_id"),
        price_cents=session.get("amount_total") or (asset or {}).get("price_cents"),
        interval=interval or tools.config.subscription_interval,
        status="active" if paid else "incomplete",
        channel=channel, campaign=campaign,
    )


def apply_subscription_update(tools, sub: dict[str, Any]) -> int | None:
    """Mirror a Stripe Subscription object's status onto our subscriber row."""
    status = sub.get("status")
    if status not in STRIPE_STATUSES:
        return None
    meta = sub.get("metadata") or {}
    items = ((sub.get("items") or {}).get("data") or [{}])
    period_end = sub.get("current_period_end") or items[0].get("current_period_end")
    niche = meta.get("niche")
    asset = subscription_asset(tools.state, niche) if niche else None
    if (meta.get("tier") == "api" or meta.get("niche") == "api") and asset is None:
        from api.auth import api_asset

        asset, niche = api_asset(tools.state), None
    sid, _ = tools.state.upsert_subscriber(
        "stripe", sub["id"], customer_id=_id(sub.get("customer")), niche=niche,
        asset_id=(asset or {}).get("id"), hypothesis_id=(asset or {}).get("hypothesis_id"),
        status=status, current_period_end=_ts(period_end),
    )
    from api.auth import is_api_subscriber, sync_from_stripe_status

    if is_api_subscriber(tools.state, tools.state.subscriber(sid)):
        sync_from_stripe_status(tools, sid, status)  # webhook or polling: keys follow the subscription
    if status == "past_due":
        from strategies.retention_engine import on_past_due

        on_past_due(tools, sid)  # dunning opens once, whether we saw the failed invoice or not
    return sid


def restore_api_access(tools, inv: dict[str, Any]) -> int:
    """A paid invoice ends a payment-failure degradation at once (Stripe's own status update may
    arrive later)."""
    from api.auth import ApiKeys, is_api_subscriber

    sub_id = invoice_subscription_id(inv)
    sub = tools.state.get_subscriber(sub_id) if sub_id else None
    if not sub or not is_api_subscriber(tools.state, sub):
        return 0
    return ApiKeys(tools.state, tools.config).sync_subscriber(sub["id"], "active", f"invoice {inv.get('id')} paid")


def invoice_subscription_id(inv: dict[str, Any]) -> str | None:
    # Older API versions: invoice.subscription; newer: invoice.parent.subscription_details.subscription.
    return _id(inv.get("subscription")) or _id(((inv.get("parent") or {}).get("subscription_details") or {}).get("subscription"))


def record_invoice(tools, inv: dict[str, Any]) -> bool:
    """One paid invoice → one verified revenue event (idempotent by invoice id)."""
    sub_id = invoice_subscription_id(inv)
    amount = int(inv.get("amount_paid") or 0)
    if not sub_id or amount <= 0 or inv.get("status") not in (None, "paid"):
        return False
    state = tools.state
    subscriber = state.get_subscriber(sub_id)
    if subscriber is None:
        meta = ((inv.get("parent") or {}).get("subscription_details") or {}).get("metadata") or {}
        asset = subscription_asset(state, meta["niche"]) if meta.get("niche") else None
        state.upsert_subscriber("stripe", sub_id, customer_id=_id(inv.get("customer")), email=inv.get("customer_email"),
                                niche=meta.get("niche"), asset_id=(asset or {}).get("id"),
                                hypothesis_id=(asset or {}).get("hypothesis_id"), price_cents=amount)
        subscriber = state.get_subscriber(sub_id)
    order = Order(
        provider="stripe", order_id=str(inv["id"]), email=inv.get("customer_email") or subscriber.get("email"),
        gross_cents=amount, product_ref=None,
        occurred_at=_ts((inv.get("status_transitions") or {}).get("paid_at") or inv.get("created")) or state.now(),
        asset_id=subscriber.get("asset_id"), channel=subscriber.get("channel"), campaign=subscriber.get("campaign"),
        kind="subscription", status="subscription", product_name="",
    )
    sf = stripe_storefront(tools)
    rep = tools.revenue.record_orders(
        [order], sf.fee_pct if sf else tools.config.stripe_fee_pct, sf.fee_fixed_cents if sf else tools.config.stripe_fee_fixed_cents
    )
    return rep.new == 1


# ----------------------------------------------------------------------------- packages & emails
def weekly_package(tools, niche: str, since: datetime, period: str) -> tuple[str, int, str]:
    """Build (or reuse) ``assets/<niche>/weekly/<period>.zip``. Returns (path, delta_count, summary)."""
    files = tools.files
    rel = f"assets/{niche}/weekly/{period}.zip"
    meta_rel = f"assets/{niche}/weekly/{period}.json"
    if files.exists(rel) and files.exists(meta_rel):
        meta = files.read_json(meta_rel)
        return rel, meta["delta_count"], meta["summary"]
    intel_path = f"exports/intel/{niche}/tech_radar.json"
    records = files.read_json(intel_path) if files.exists(intel_path) else []
    first_seen: dict[str, list[str]] = {}
    for lead in tools.state.leads_for_niche(niche):
        first_seen.setdefault(_norm_company(lead.get("company", "")), []).append(lead.get("first_seen", ""))
    cutoff = since.isoformat(timespec="seconds")
    delta = []
    for r in records:
        seen = first_seen.get(_norm_company(r["company"]), [])
        recent = [s for s in seen if s >= cutoff]
        if recent:
            delta.append({**r, "change": "new company" if len(recent) == len(seen) else "new roles"})
    new_companies = sum(1 for r in delta if r["change"] == "new company")
    summary = (f"{len(delta)} companies changed since {since.date()}: {new_companies} new, "
               f"{len(delta) - new_companies} with new roles. {len(records)} companies tracked in total.")
    md = [f"# {niche_title(niche)} Tech Stack Intel: weekly update {period}", "", summary, "",
          "| Change | Company | Urgency | Signals | Stack |", "|---|---|---|---|---|"]
    for r in sorted(delta, key=lambda r: -r.get("urgency_score", 0)):
        md.append(f"| {r['change']} | {r['company'].replace('|', '/')} | {r.get('urgency_score', 0)} | "
                  f"{', '.join(r.get('intent_signals', [])) or '-'} | {', '.join(r.get('stack', [])[:5]) or '-'} |")

    def csv_bytes(rows: list[dict[str, Any]], fields: list[str]) -> bytes:
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: "; ".join(map(str, v)) if isinstance(v, list) else v for k, v in row.items()})
        return buf.getvalue().encode()

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        folder = f"{niche}-update-{period}"
        zf.writestr(f"{folder}/WEEKLY_UPDATE.md", "\n".join(md) + "\n")
        zf.writestr(f"{folder}/delta.json", json.dumps(delta, indent=2))
        zf.writestr(f"{folder}/delta.csv", csv_bytes(delta, ["change", *INTEL_FIELDS]))
        zf.writestr(f"{folder}/full/tech_radar.csv", csv_bytes(records, INTEL_FIELDS))
        radar = f"exports/intel/{niche}/EXECUTIVE_TECH_RADAR.md"
        if files.exists(radar):
            zf.writestr(f"{folder}/full/EXECUTIVE_TECH_RADAR.md", files.read_bytes(radar))
    files.write_bytes(rel, out.getvalue())
    files.write_json(meta_rel, {"delta_count": len(delta), "summary": summary, "since": cutoff})
    return rel, len(delta), summary


def send_welcome(tools, subscriber_id: int) -> str:
    """Email the current full dataset to a new subscriber (once)."""
    state = tools.state
    sub = next((s for s in state.list_subscribers() if s["id"] == subscriber_id), None)
    if not sub or not sub.get("email") or not sub.get("niche"):
        return "skipped"
    done = state.subscription_delivery(subscriber_id, "welcome")
    if done and done["status"] == "delivered":
        return "delivered"
    dataset = next((a for a in state.list_assets() if a["kind"] == ASSET_KIND and a.get("niche") == sub["niche"]), None)
    if dataset is None:
        return "skipped"
    path = tools.files.resolve(dataset["path"])
    label = interval_label(sub.get("interval") or "month").lower()
    email = Email(
        to=sub["email"], subject=f"Welcome: {niche_title(sub['niche'])} Tech Stack Intel updates", kind="delivery",
        body=(f"Hi,\n\nThanks for subscribing to {niche_title(sub['niche'])} Tech Stack Intel ({label} billing).\n"
              f"The full current dataset is attached ({path.name}). Every Monday you'll get what changed that week: "
              "new companies, new roles and shifting stacks.\n\nManage or cancel any time from your Stripe receipt email.\n\n"
              f"{tools.config.sender_name or 'AutoMonetize'}"),
        attachments=[Attachment(path.name, path.read_bytes())],
    )
    try:
        outcome = tools.dispatcher.send_transactional(email, audit_key=f"sub:{subscriber_id}:welcome")
    except Exception as exc:  # noqa: BLE001 - retried by the next sync/delivery pass
        state.record_subscription_delivery(subscriber_id, "welcome", "failed", detail=repr(exc))
        state.log_error("subscriptions", f"welcome to subscriber {subscriber_id} failed: {exc!r}")
        return "failed"
    state.record_subscription_delivery(subscriber_id, "welcome", outcome)
    return outcome


def delivery_moment(now: datetime, weekday: int, hour: int, tz: str) -> tuple[datetime, str]:
    """This ISO week's delivery time (tz-aware) and its period key, e.g. 2026-W40."""
    local = now.astimezone(ZoneInfo(tz))
    week_start = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    moment = week_start + timedelta(days=weekday, hours=hour)
    year, week, _ = local.isocalendar()
    return moment, f"{year}-W{week:02d}"


class SubscriptionEngine(Strategy):
    name = "subscription_engine"
    tasks = ("publish_subscription", "publish_api_tier", "sync_subscriptions", "deliver_subscriptions")

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        return getattr(self, task)(ctx)

    # ------------------------------------------------------------------ offer
    def publish_subscription(self, ctx: TaskContext) -> TaskResult:
        tools, cfg = ctx.tools, ctx.tools.config
        if cfg.subscription_price_cents <= 0:
            return TaskResult(True, "subscription tier disabled", {"published": False})
        sf = stripe_storefront(tools)
        if sf is None:
            return TaskResult(True, "subscriptions need a Stripe secret key", {"published": False})
        dataset = tools.state.latest_asset(ctx.hypothesis["id"], ASSET_KIND)
        if not dataset or dataset.get("status") != "published":
            return TaskResult(True, "no live one-off dataset yet", {"published": False})
        existing = subscription_asset(tools.state, ctx.niche)
        if existing:
            return TaskResult(True, f"subscription live: {existing['checkout_url']}", {"published": False, "live": True})
        if not (tools.dispatcher.can_deliver() or cfg.allow_manual_fulfillment):
            return TaskResult(True, "not selling subscriptions until weekly delivery email works", {"published": False, "blocked": "fulfilment"})
        interval = cfg.subscription_interval
        title = f"{niche_title(ctx.niche)} Tech Stack Intel: {interval_label(interval)} Subscription"
        summary = (f"Every Monday: the companies newly hiring for {niche_title(ctx.niche)} roles, their stacks and buying "
                   f"signals, plus the full refreshed dataset. ${cfg.subscription_price_cents / 100:.2f}/{interval}, cancel any time.")
        meta = {"niche": ctx.niche, "hypothesis_id": str(ctx.hypothesis["id"]), "asset_id": str(dataset["id"])}
        ref, url, price_id = sf.create_subscription_link(title, summary, cfg.subscription_price_cents, interval, meta)
        aid = tools.state.add_asset(ctx.hypothesis["id"], SUB_KIND, title, dataset["path"], 1, dataset["lead_count"],
                                    cfg.subscription_price_cents, product_ref=ref)
        tools.state.update_asset(aid, niche=ctx.niche, provider="stripe", checkout_url=url, status="published",
                                 kind_meta=json.dumps({"interval": interval, "price_id": price_id}))
        return TaskResult(True, f"subscription live at ${cfg.subscription_price_cents / 100:.2f}/{interval}: {url}",
                          {"published": True, "checkout_url": url})

    def publish_api_tier(self, ctx: TaskContext) -> TaskResult:
        """The Developer API product: $29/month recurring Payment Link, one per account."""
        from api.auth import API_KIND, api_asset

        tools, cfg = ctx.tools, ctx.tools.config
        if not cfg.api_enabled or cfg.api_price_cents <= 0:
            return TaskResult(True, "API tier disabled", {"published": False})
        existing = api_asset(tools.state)
        if existing:
            return TaskResult(True, f"API tier live: {existing['checkout_url']}", {"published": False, "live": True})
        sf = stripe_storefront(tools)
        if sf is None:
            return TaskResult(True, "the API tier needs a Stripe secret key", {"published": False})
        if not cfg.lead_capture_base:
            return TaskResult(True, "not selling API access until the API is publicly reachable (tunnel)",
                              {"published": False, "blocked": "tunnel"})
        if not (tools.dispatcher.can_deliver() or cfg.allow_manual_fulfillment):
            return TaskResult(True, "not selling API access until keys can be emailed", {"published": False, "blocked": "email"})
        if not tools.files.exists(f"exports/intel/{ctx.niche}/tech_radar.json"):
            return TaskResult(True, "no data to serve yet", {"published": False})
        title = "Developer API: Hiring & Buying-Intent Signals"
        summary = (f"REST API: every company hiring in the tracked niches with its tech stack, buying-intent tag, migration "
                   f"history and active postings. {cfg.api_daily_quota} requests/day. "
                   f"${cfg.api_price_cents / 100:.2f}/month, cancel any time. Key delivered by email instantly.")
        meta = {"niche": "api", "tier": "api", "hypothesis_id": str(ctx.hypothesis["id"])}
        ref, url, price_id = sf.create_subscription_link(title, summary, cfg.api_price_cents, "month", meta)
        aid = tools.state.add_asset(ctx.hypothesis["id"], API_KIND, title, f"{cfg.lead_capture_base}/docs/api", 1, 0,
                                    cfg.api_price_cents, product_ref=ref)
        tools.state.update_asset(aid, provider="stripe", checkout_url=url, status="published",
                                 kind_meta=json.dumps({"interval": "month", "price_id": price_id, "tier": "api"}))
        return TaskResult(True, f"API tier live at ${cfg.api_price_cents / 100:.2f}/month: {url}",
                          {"published": True, "checkout_url": url})

    # ------------------------------------------------------------------ reconciliation
    def sync_subscriptions(self, ctx: TaskContext) -> TaskResult:
        tools = ctx.tools
        sf = stripe_storefront(tools)
        if sf is None:
            return TaskResult(True, "no Stripe key: subscriptions not synced", {})
        new_subs = invoices = welcomes = 0
        from api.auth import API_KIND

        for asset in [a for a in tools.state.list_assets() if a["kind"] in (SUB_KIND, API_KIND) and a.get("product_ref")]:
            created = int(datetime.fromisoformat(asset["created_at"]).timestamp())
            for s in sf.client.completed_sessions(asset["product_ref"], created):
                if s.get("mode") == "subscription":
                    _, is_new = activate_from_session(tools, s)
                    new_subs += is_new
        for sub in tools.state.list_subscribers():
            if sub["subscription_status"] in ("canceled", "incomplete_expired"):
                continue
            try:
                apply_subscription_update(tools, sf.client.get_subscription(sub["subscription_id"]))
                for inv in sf.client.paid_invoices(sub["subscription_id"]):
                    invoices += record_invoice(tools, inv)
            except Exception as exc:  # noqa: BLE001 - one bad subscription must not stop the sweep
                tools.state.log_error("subscriptions", f"sync {sub['subscription_id']} failed: {exc!r}")
        for sub in tools.state.list_subscribers(DELIVERABLE):
            welcome = tools.state.subscription_delivery(sub["id"], "welcome")
            if welcome is None or welcome["status"] == "failed":
                welcomes += send_welcome(tools, sub["id"]) == "delivered"
        return TaskResult(True, f"{new_subs} new subscribers, {invoices} new invoices, {welcomes} welcomes sent",
                          {"new_subscribers": new_subs, "new_invoices": invoices, "welcomes": welcomes})

    # ------------------------------------------------------------------ weekly delivery
    def deliver_subscriptions(self, ctx: TaskContext) -> TaskResult:
        tools, cfg = ctx.tools, ctx.tools.config
        now = tools.state.clock()
        moment, period = delivery_moment(now, cfg.subscription_delivery_weekday, cfg.subscription_delivery_hour,
                                         cfg.subscription_timezone)
        if now < moment:
            return TaskResult(True, f"next delivery {moment.isoformat(timespec='minutes')}", {"due": False})
        sent = dry = failed = skipped = 0
        packages: dict[str, tuple[str, int, str]] = {}
        refreshed = self.refresh_subscribed_niches(ctx, period)
        for sub in tools.state.list_subscribers(DELIVERABLE):
            if not sub.get("email") or not sub.get("niche"):
                skipped += 1
                continue
            done = tools.state.subscription_delivery(sub["id"], period)
            if done and (done["status"] == "delivered" or (done["status"] == "dry_run" and not tools.dispatcher.live)):
                continue
            if sub["niche"] not in packages:
                packages[sub["niche"]] = weekly_package(tools, sub["niche"], moment - timedelta(days=7), period)
            rel, count, summary = packages[sub["niche"]]
            path = tools.files.resolve(rel)
            email = Email(
                to=sub["email"], kind="delivery",
                subject=f"{niche_title(sub['niche'])} Tech Stack Intel: {count} changes this week ({period})",
                body=f"Hi,\n\nYour weekly update is attached ({path.name}).\n\n{summary}\n\n"
                     f"{cfg.sender_name or 'AutoMonetize'}",
                attachments=[Attachment(path.name, path.read_bytes())],
            )
            try:
                outcome = tools.dispatcher.send_transactional(email, audit_key=f"sub:{sub['id']}:{period}")
            except Exception as exc:  # noqa: BLE001 - retried next cycle this week
                tools.state.record_subscription_delivery(sub["id"], period, "failed", count, repr(exc))
                failed += 1
                continue
            tools.state.record_subscription_delivery(sub["id"], period, outcome, count)
            sent += outcome == "delivered"
            dry += outcome == "dry_run"
        return TaskResult(True, f"{period}: {sent} sent, {dry} dry-run, {failed} failed, {skipped} skipped"
                          + (f", refreshed {', '.join(refreshed)}" if refreshed else ""),
                          {"due": True, "period": period, "sent": sent, "dry_run": dry, "failed": failed,
                           "refreshed": refreshed})

    def refresh_subscribed_niches(self, ctx: TaskContext, period: str) -> list[str]:
        """Re-collect data for subscribed niches the engine is no longer working on.

        After a pivot, only the active niche gets fresh data each cycle; without this, paying
        subscribers of an older niche would get "0 changes" every week until they churned. Runs
        once per niche per delivery period, right before its package is built."""
        from strategies.b2b_lead_aggregator import LeadAggregator
        from strategies.tech_stack_intel import TechStackIntel

        tools = ctx.tools
        niches = {s["niche"] for s in tools.state.list_subscribers(DELIVERABLE) if s.get("niche")} - {ctx.niche}
        refreshed = []
        for niche in sorted(niches):
            if tools.files.exists(f"assets/{niche}/weekly/{period}.zip"):
                continue  # this week's package is already built
            hyp = next((h for h in reversed(tools.state.list_hypotheses()) if h["params"].get("niche") == niche), None)
            if hyp is None:
                continue
            sub_ctx = TaskContext(tools, hyp, {})
            try:
                LeadAggregator().run("aggregate_leads", sub_ctx)
                TechStackIntel().run("build_intel", sub_ctx)
                refreshed.append(niche)
            except Exception as exc:  # noqa: BLE001 - deliver last week's data rather than nothing
                tools.state.log_error("subscriptions", f"refresh of {niche} failed: {exc!r}")
        return refreshed
