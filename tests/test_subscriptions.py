import io
import json
import urllib.parse
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from strategies.base import TaskContext
from strategies.subscription_engine import (
    SubscriptionEngine, delivery_moment, invoice_subscription_id, record_invoice, weekly_package,
)
from tests.conftest import NOW
from tests.test_distribution import FakeSMTP, audit, go_live, pipeline
from tests.test_webhook import SECRET, event
from tools import build_toolkit
from tools.storefront.stripe_pages_publisher import StripeStorefront
from tools.storefront.webhook_listener import WebhookProcessor, sign_payload

STRIPE = "https://api.stripe.com/v1"
MONDAY_9 = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)  # NOW is Wednesday 2026-09-30


@pytest.fixture
def smtp():
    FakeSMTP.sent = []
    FakeSMTP.fail = False
    return FakeSMTP


@pytest.fixture
def stripe_kit(config, state, breaker, transport, smtp):
    config.stripe_secret_key, config.stripe_webhook_secret = "sk_test", SECRET
    transport.add_json(f"{STRIPE}/products", {"id": "prod_s"})
    transport.add_json(f"{STRIPE}/prices", {"id": "price_s"})
    transport.add_json(f"{STRIPE}/payment_links", {"id": "plink_sub", "url": "https://buy.stripe.com/sub"})
    return build_toolkit(config, state, breaker, transport=transport, sleep=lambda s: None, smtp_factory=smtp)


@pytest.fixture
def live_niche(stripe_kit, state, make_hypothesis, config):
    """A published one-off dataset plus a live subscription offer for python-remote."""
    go_live(config)
    hyp = make_hypothesis()
    pipeline(stripe_kit, hyp)
    ds = state.latest_asset(hyp["id"], "lead_directory")
    state.update_asset(ds["id"], status="published", product_ref="plink_1", checkout_url="https://buy.stripe.com/one",
                       provider="stripe")
    res = SubscriptionEngine().publish_subscription(TaskContext(stripe_kit, hyp, {}))
    assert res.metrics["published"], res.summary
    return hyp, state.get_asset(ds["id"])


def post(kit, payload):
    return WebhookProcessor(kit).handle(payload, sign_payload(payload, SECRET))


def sub_session(**kw):
    s = {"id": "cs_sub_1", "object": "checkout.session", "mode": "subscription", "status": "complete",
         "payment_status": "paid", "payment_link": "plink_sub", "subscription": "sub_1", "customer": "cus_1",
         "amount_total": 1000, "customer_details": {"email": "Sub@Acme.example"},
         "client_reference_id": "am--devto--radar_python_remote_2026_w40", "created": int(NOW.timestamp())}
    s.update(kw)
    return s


def invoice(inv_id="in_1", sub="sub_1", amount=1000, new_shape=False, **kw):
    inv = {"id": inv_id, "object": "invoice", "status": "paid", "amount_paid": amount, "customer": "cus_1",
           "customer_email": "sub@acme.example", "billing_reason": "subscription_create",
           "status_transitions": {"paid_at": int(NOW.timestamp())}}
    if new_shape:
        inv["parent"] = {"subscription_details": {"subscription": sub, "metadata": {"niche": "python-remote"}}}
    else:
        inv["subscription"] = sub
    inv.update(kw)
    return inv


# ------------------------------------------------------------------ Stripe payloads
def test_subscription_link_payloads(config, toolkit, transport):
    config.stripe_secret_key = "sk_test"
    transport.add_json(f"{STRIPE}/products", {"id": "prod_s"})
    transport.add_json(f"{STRIPE}/prices", {"id": "price_s"})
    transport.add_json(f"{STRIPE}/payment_links", {"id": "plink_sub", "url": "https://buy.stripe.com/sub"})
    sf = StripeStorefront(config, toolkit.http)
    ref, url, price_id = sf.create_subscription_link("T", "S", 1000, "month", {"niche": "python-remote", "asset_id": "3"})
    assert (ref, url, price_id) == ("plink_sub", "https://buy.stripe.com/sub", "price_s")
    price = urllib.parse.parse_qs(transport.calls_to(f"{STRIPE}/prices", "POST")[0]["body"].decode())
    assert price == {"product": ["prod_s"], "unit_amount": ["1000"], "currency": ["usd"], "recurring[interval]": ["month"]}
    link = urllib.parse.parse_qs(transport.calls_to(f"{STRIPE}/payment_links", "POST")[0]["body"].decode())
    assert link["line_items[0][price]"] == ["price_s"]
    assert link["subscription_data[metadata][niche]"] == ["python-remote"]
    assert link["subscription_data[metadata][kind]"] == ["subscription"]
    with pytest.raises(ValueError):
        sf.create_subscription_link("T", "S", 1000, "fortnight", {})


def test_one_off_prices_are_not_recurring(config, toolkit, transport):
    config.stripe_secret_key = "sk_test"
    for path, body in (("products", {"id": "p"}), ("prices", {"id": "pr"}), ("payment_links", {"id": "pl", "url": "u"})):
        transport.add_json(f"{STRIPE}/{path}", body)
    StripeStorefront(config, toolkit.http).create_link("T", "S", 1400, {"niche": "n"})
    assert "recurring[interval]" not in transport.calls_to(f"{STRIPE}/prices", "POST")[0]["body"].decode()


def test_publish_subscription_offer(live_niche, state, stripe_kit):
    hyp, _ = live_niche
    offer = next(a for a in state.list_assets() if a["kind"] == "subscription")
    assert offer["price_cents"] == 1000 and offer["checkout_url"] == "https://buy.stripe.com/sub"
    assert json.loads(offer["kind_meta"]) == {"interval": "month", "price_id": "price_s"}
    again = SubscriptionEngine().publish_subscription(TaskContext(stripe_kit, hyp, {}))
    assert again.metrics == {"published": False, "live": True}


def test_publish_subscription_requires_working_delivery(config, stripe_kit, state, make_hypothesis):
    hyp = make_hypothesis()
    pipeline(stripe_kit, hyp)
    ds = state.latest_asset(hyp["id"], "lead_directory")
    state.update_asset(ds["id"], status="published")
    res = SubscriptionEngine().publish_subscription(TaskContext(stripe_kit, hyp, {}))
    assert res.metrics.get("blocked") == "fulfilment"  # dry run: no way to deliver weekly emails yet


# ------------------------------------------------------------------ activation, revenue, status
def test_checkout_activates_subscriber_and_welcome_is_sent(live_niche, stripe_kit, state, smtp):
    hyp, _ = live_niche
    out = post(stripe_kit, event("checkout.session.completed", sub_session()))
    assert out.status == 200 and "activated" in out.body["detail"] and not out.fulfil
    s = state.get_subscriber("sub_1")
    assert s["subscription_status"] == "active" and s["email"] == "sub@acme.example" and s["niche"] == "python-remote"
    assert (s["channel"], s["campaign"]) == ("devto", "radar_python_remote_2026_w40")
    assert state.list_revenue() == []  # the session is not revenue; the invoice is
    for job in out.after:
        job()
    welcome = smtp.sent[-1]
    assert welcome["To"] == "sub@acme.example" and welcome["Subject"].startswith("Welcome")
    assert list(welcome.iter_attachments())[0].get_filename().endswith(".zip")
    assert state.subscription_delivery(s["id"], "welcome")["status"] == "delivered"


@pytest.mark.parametrize("new_shape", [False, True])
def test_invoice_paid_records_recurring_revenue_once(live_niche, stripe_kit, state, new_shape):
    hyp, _ = live_niche
    post(stripe_kit, event("checkout.session.completed", sub_session(), "evt_a"))
    post(stripe_kit, event("invoice.paid", invoice(new_shape=new_shape), "evt_b"))
    assert post(stripe_kit, event("invoice.paid", invoice(new_shape=new_shape), "evt_c")).body["status"] == "ignored"
    assert state.revenue_for_hypothesis(hyp["id"]) == 1000 - 59  # 2.9% + 30c
    order = state.get_order("stripe", "in_1")
    assert (order["kind"], order["status"], order["channel"]) == ("subscription", "subscription", "devto")
    assert order not in state.orders_to_deliver()
    post(stripe_kit, event("invoice.paid", invoice("in_2", billing_reason="subscription_cycle", new_shape=new_shape), "evt_d"))
    assert state.revenue_for_hypothesis(hyp["id"]) == 2 * (1000 - 59)


def test_invoice_helpers():
    assert invoice_subscription_id(invoice()) == "sub_1"
    assert invoice_subscription_id(invoice(new_shape=True)) == "sub_1"
    assert invoice_subscription_id({"id": "in_x"}) is None


def test_zero_amount_and_non_subscription_invoices_ignored(stripe_kit, state):
    assert not record_invoice(stripe_kit, invoice(amount=0))
    assert not record_invoice(stripe_kit, {"id": "in_x", "amount_paid": 500, "status": "paid"})
    assert state.list_revenue() == []


def test_status_updates_and_cancellation(live_niche, stripe_kit, state, clock):
    post(stripe_kit, event("checkout.session.completed", sub_session(), "evt_a"))
    sub = {"id": "sub_1", "object": "subscription", "status": "past_due", "customer": "cus_1",
           "metadata": {"niche": "python-remote"}, "items": {"data": [{"current_period_end": int(NOW.timestamp()) + 86400 * 30}]}}
    post(stripe_kit, event("customer.subscription.updated", sub, "evt_b"))
    s = state.get_subscriber("sub_1")
    assert s["subscription_status"] == "past_due" and s["current_period_end"].startswith("2026-10-30")
    clock.advance(days=2)
    post(stripe_kit, event("customer.subscription.deleted", {**sub, "status": "active"}, "evt_c"))
    s = state.get_subscriber("sub_1")
    assert s["subscription_status"] == "canceled" and s["canceled_at"].startswith("2026-10-02")


# ------------------------------------------------------------------ polling reconciliation
def test_polling_skips_subscription_sessions_and_sync_discovers_them(live_niche, stripe_kit, state, transport, smtp):
    transport.add_json(f"{STRIPE}/checkout/sessions", {"has_more": False, "data": [sub_session()]})
    assert stripe_kit.storefront.fetch_orders(["plink_sub"], NOW - timedelta(days=1)) == []
    transport.add_json(f"{STRIPE}/subscriptions/sub_1", {"id": "sub_1", "status": "active", "customer": "cus_1",
                                                          "metadata": {"niche": "python-remote"}})
    transport.add_json(f"{STRIPE}/invoices", {"has_more": False, "data": [invoice(), invoice("in_2")]})
    res = SubscriptionEngine().sync_subscriptions(TaskContext(stripe_kit, live_niche[0], {}))
    assert res.metrics == {"new_subscribers": 1, "new_invoices": 2, "welcomes": 1}
    assert "status=paid" in transport.calls_to(f"{STRIPE}/invoices")[0]["url"]
    again = SubscriptionEngine().sync_subscriptions(TaskContext(stripe_kit, live_niche[0], {}))
    assert again.metrics == {"new_subscribers": 0, "new_invoices": 0, "welcomes": 0}


# ------------------------------------------------------------------ weekly delivery
def test_delivery_moment_and_timezone():
    moment, period = delivery_moment(NOW, 0, 8, "UTC")
    assert moment.isoformat() == "2026-09-28T08:00:00+00:00" and period == "2026-W40"
    ny, _ = delivery_moment(datetime(2026, 10, 5, 11, 0, tzinfo=timezone.utc), 0, 8, "America/New_York")
    assert ny.astimezone(timezone.utc).hour == 12  # 08:00 EDT


def test_weekly_package_marks_new_companies_and_new_roles(stripe_kit, state, make_hypothesis, clock):
    hyp = make_hypothesis()
    pipeline(stripe_kit, hyp)  # leads first seen at NOW
    clock.advance(days=10)
    state.upsert_lead("fresh-acme-role", "python-remote", {"company": "Acme Inc", "title": "Staff Engineer", "tags": []})
    rel, count, summary = weekly_package(stripe_kit, "python-remote", clock() - timedelta(days=7), "2026-W41")
    assert count == 1 and "0 new, 1 with new roles" in summary
    zf = zipfile.ZipFile(io.BytesIO(stripe_kit.files.read_bytes(rel)))
    names = zf.namelist()
    assert "python-remote-update-2026-W41/WEEKLY_UPDATE.md" in names and "python-remote-update-2026-W41/full/tech_radar.csv" in names
    delta_csv = zf.read("python-remote-update-2026-W41/delta.csv").decode()
    assert delta_csv.splitlines()[1].startswith("new roles,Acme Inc")
    # cached per period
    assert weekly_package(stripe_kit, "python-remote", clock(), "2026-W41")[1] == 1


def test_monday_delivery_once_per_week_to_active_subscribers(live_niche, stripe_kit, state, clock, smtp):
    post(stripe_kit, event("checkout.session.completed", sub_session(), "evt_a"))
    post(stripe_kit, event("checkout.session.completed", sub_session(id="cs_2", subscription="sub_2",
                                                                   customer_details={"email": "late@x.example"}), "evt_b"))
    post(stripe_kit, event("customer.subscription.updated", {"id": "sub_2", "status": "past_due", "metadata": {}}, "evt_c"))
    eng = SubscriptionEngine()
    ctx = TaskContext(stripe_kit, live_niche[0], {})
    assert eng.deliver_subscriptions(ctx).metrics == {"due": True, "period": "2026-W40", "sent": 1, "dry_run": 0, "failed": 0, "refreshed": []}
    clock.now = datetime(2026, 10, 5, 7, 59, tzinfo=timezone.utc)
    assert eng.deliver_subscriptions(ctx).metrics["due"] is False  # before Monday 08:00
    clock.now = MONDAY_9
    smtp.sent.clear()
    res = eng.deliver_subscriptions(ctx)
    assert res.metrics["sent"] == 1 and res.metrics["period"] == "2026-W41"
    msg = smtp.sent[0]
    assert msg["To"] == "sub@acme.example" and "2026-W41" in msg["Subject"]  # past_due subscriber excluded
    assert list(msg.iter_attachments())[0].get_filename() == "2026-W41.zip"
    clock.advance(days=2)  # Wednesday, same ISO week: nothing more
    assert eng.deliver_subscriptions(ctx).metrics["sent"] == 0
    s = state.get_subscriber("sub_1")
    assert s["last_delivered_at"] and state.subscription_delivery(s["id"], "2026-W41")["status"] == "delivered"


def test_dry_run_then_live_in_same_week(live_niche, stripe_kit, state, clock, smtp, config):
    post(stripe_kit, event("checkout.session.completed", sub_session(), "evt_a"))
    clock.now = MONDAY_9
    config.dry_run = True
    eng, ctx = SubscriptionEngine(), TaskContext(stripe_kit, live_niche[0], {})
    smtp.sent.clear()
    assert eng.deliver_subscriptions(ctx).metrics["dry_run"] == 1 and smtp.sent == []
    assert eng.deliver_subscriptions(ctx).metrics["dry_run"] == 0  # audited once
    assert any("2026-W41" in r["subject"] for r in audit(config))
    config.dry_run = False
    assert eng.deliver_subscriptions(ctx).metrics["sent"] == 1


def test_missed_monday_catches_up_later_that_week(live_niche, stripe_kit, state, clock, smtp):
    post(stripe_kit, event("checkout.session.completed", sub_session(), "evt_a"))
    clock.now = datetime(2026, 10, 8, 20, 0, tzinfo=timezone.utc)  # Thursday: machine was asleep on Monday
    assert SubscriptionEngine().deliver_subscriptions(TaskContext(stripe_kit, live_niche[0], {})).metrics["sent"] == 1


def test_default_one_off_tiers_and_subscription_price():
    from agent.config import Config
    from tools.storefront import price_for

    c = Config()
    assert c.price_matrix == [900, 1400, 1900] and c.subscription_price_cents == 1000
    assert [price_for(n, c.price_tiers) for n in (3, 30, 100)] == [900, 1400, 1900]
