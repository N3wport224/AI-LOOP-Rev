"""Phase 8: dunning, grace periods, recovery and churn analytics."""

import re
from datetime import timedelta

import pytest

from api.auth import ApiKeys, sync_from_stripe_status
from strategies.base import TaskContext
from strategies.retention_engine import RetentionEngine, churn_summary, open_case
from strategies.subscription_engine import apply_subscription_update
from tests.test_api import SECRET, api_tier, bearer, call, deliver
from tests.test_distribution import FakeSMTP, go_live
from tools import build_toolkit
from tools.storefront.webhook_listener import WebhookProcessor

STRIPE = "https://api.stripe.com/v1"
PORTAL = "https://billing.stripe.com/p/session/test_abc"


@pytest.fixture
def kit(config, state, breaker, transport, make_hypothesis):
    FakeSMTP.sent, FakeSMTP.fail = [], False
    go_live(config)
    config.stripe_secret_key, config.stripe_webhook_secret = "sk_test", SECRET
    config.public_webhook_url = "https://hooks.example.com/webhook"
    config.pages_base_url = "https://pages.example.com"
    config.api_burst, config.api_rate_per_second = 1000, 1000.0
    transport.add_json(f"{STRIPE}/billing_portal/sessions", {"id": "bps_1", "url": PORTAL})
    k = build_toolkit(config, state, breaker, transport=transport, sleep=lambda s: None, smtp_factory=FakeSMTP)
    k.hyp = make_hypothesis()
    return k


@pytest.fixture
def customer(kit, state, config):
    """An active $29/month API subscriber with one key; returns (proc, subscriber, key)."""
    api_tier(state, kit.hyp["id"])
    proc = WebhookProcessor(kit, use_sdk=False)
    deliver(proc, "checkout.session.completed", {
        "id": "cs_r", "object": "checkout.session", "mode": "subscription", "payment_link": "plink_api", "subscription": "sub_r",
        "customer": "cus_r", "amount_total": 2900, "payment_status": "paid", "status": "complete",
        "customer_details": {"email": "dev@corp.example"}}, "evt_r0")
    key = re.search(r"am_live_[0-9a-f]{48}", FakeSMTP.sent[-1].get_body(("plain",)).get_content()).group(0)
    FakeSMTP.sent.clear()
    return proc, state.get_subscriber("sub_r"), key


def failed(proc, eid="evt_f1", inv="in_f1"):
    return deliver(proc, "invoice.payment_failed", {"id": inv, "object": "invoice", "subscription": "sub_r", "amount_due": 2900,
                                                    "status": "open", "customer": "cus_r"}, eid)


def paid(proc, eid="evt_p1", inv="in_f1"):
    return deliver(proc, "invoice.paid", {"id": inv, "object": "invoice", "subscription": "sub_r", "amount_paid": 2900,
                                          "status": "paid", "customer": "cus_r"}, eid)


def dun(kit):
    return RetentionEngine().run("run_dunning", TaskContext(kit, kit.hyp, {}))


def test_payment_failed_opens_case_degrades_key_and_sends_portal_link(kit, state, config, customer, transport):
    proc, sub, key = customer
    out = failed(proc)
    assert out.status == 200
    case = open_case(state, sub["id"])
    assert case["status"] == "open" and case["invoice_id"] == "in_f1"
    row = ApiKeys(state, config).lookup(key)
    assert row["status"] == "degraded" and row["daily_quota"] == config.api_degraded_quota == 50
    assert case["grace_until"].startswith((state.clock() + timedelta(days=7)).date().isoformat())
    msg = FakeSMTP.sent[-1]
    body = msg.get_body(("plain",)).get_content()
    assert msg["To"] == "dev@corp.example" and "didn't go through" in msg["Subject"]
    assert PORTAL in body and "50 requests a day" in body
    portal = transport.calls_to(f"{STRIPE}/billing_portal/sessions", "POST")[-1]
    assert b"customer=cus_r" in portal["body"]
    failed(proc, "evt_f2", "in_f2")  # Stripe's retry fails again: same case, no second "first" reminder
    assert len(FakeSMTP.sent) == 1
    assert state._one("SELECT COUNT(*) AS n FROM dunning_cases")["n"] == 1

    async def s(client):
        r = await client.get("/v1/me", headers=bearer(key))
        assert r.status == 200 and r.headers["X-API-Key-Status"] == "past_due" and r.headers["X-RateLimit-Limit"] == "50"
    call(kit, s)


def test_reminders_on_schedule_then_grace_expiry_suspends(kit, state, config, customer, clock):
    proc, sub, key = customer
    failed(proc)
    assert dun(kit).metrics["reminders"] == 0  # day 0 already sent by the webhook
    clock.advance(days=3, minutes=1)
    assert dun(kit).metrics["reminders"] == 1 and "Reminder" in FakeSMTP.sent[-1]["Subject"]
    assert dun(kit).metrics["reminders"] == 0  # idempotent within the day
    clock.advance(days=3)
    assert dun(kit).metrics["reminders"] == 1
    assert open_case(state, sub["id"])["reminders_sent"] == 3
    clock.advance(days=1)
    res = dun(kit)
    assert res.metrics["expired"] == 1
    assert ApiKeys(state, config).lookup(key)["status"] == "suspended"
    assert open_case(state, sub["id"])["status"] == "expired"
    # a later "past_due" status sync must not lift the suspension back to degraded
    assert sync_from_stripe_status(kit, sub["id"], "past_due") is None
    assert ApiKeys(state, config).lookup(key)["status"] == "suspended"

    async def s(client):
        r = await client.get("/v1/signals", headers=bearer(key))
        assert r.status == 402 and (await r.json())["error"]["code"] == "payment_required"
    call(kit, s)

    paid(proc)  # the customer finally pays: full access back
    assert ApiKeys(state, config).lookup(key)["status"] == "active"
    assert open_case(state, sub["id"]) is None
    assert state._one("SELECT status FROM dunning_cases")["status"] == "recovered"


def test_invoice_paid_recovers_within_grace(kit, state, config, customer):
    proc, sub, key = customer
    failed(proc)
    paid(proc)
    row = ApiKeys(state, config).lookup(key)
    assert row["status"] == "active" and row["daily_quota"] == config.api_daily_quota
    assert churn_summary(state, state.clock() - timedelta(days=1))["dunning"]["recovered"] == 1


def test_polling_past_due_opens_case_and_reminds(kit, state, config, customer):
    _, sub, key = customer
    apply_subscription_update(kit, {"id": "sub_r", "status": "past_due", "customer": "cus_r", "metadata": {"tier": "api"}})
    assert open_case(state, sub["id"])["status"] == "open"
    assert ApiKeys(state, config).lookup(key)["status"] == "degraded"
    assert len(FakeSMTP.sent) == 1
    apply_subscription_update(kit, {"id": "sub_r", "status": "past_due", "customer": "cus_r", "metadata": {"tier": "api"}})
    assert len(FakeSMTP.sent) == 1  # seen again by the next sweep: no duplicate
    apply_subscription_update(kit, {"id": "sub_r", "status": "active", "customer": "cus_r", "metadata": {"tier": "api"}})
    dun(kit)  # paid without an invoice.paid webhook: the sweep closes the case quietly
    assert open_case(state, sub["id"]) is None
    assert ApiKeys(state, config).lookup(key)["status"] == "active"


def test_subscription_deleted_logs_churn_and_revokes(kit, state, config, customer, clock):
    proc, sub, key = customer
    failed(proc)
    clock.advance(days=10)
    deliver(proc, "customer.subscription.deleted", {"id": "sub_r", "object": "subscription", "status": "canceled",
                                                    "metadata": {"tier": "api"}}, "evt_d1")
    deliver(proc, "customer.subscription.deleted", {"id": "sub_r", "object": "subscription", "status": "canceled",
                                                    "metadata": {"tier": "api"}}, "evt_d2")  # redelivered
    assert state.get_subscriber("sub_r")["subscription_status"] == "canceled"
    assert ApiKeys(state, config).lookup(key)["status"] == "revoked"
    rows = state._all("SELECT * FROM churn_events")
    assert len(rows) == 1
    ev = rows[0]
    assert ev["product"] == "api" and ev["reason"] == "payment_failed" and ev["had_dunning"] == 1
    assert ev["mrr_lost_cents"] == 2900 and ev["tenure_days"] >= 10
    assert state._one("SELECT status FROM dunning_cases")["status"] == "canceled"
    summary = churn_summary(state, clock() - timedelta(days=30))
    assert summary["churned"] == 1 and summary["involuntary"] == 1 and summary["mrr_lost_cents"] == 2900


def test_voluntary_cancel_uses_stripe_reason(kit, state, customer):
    proc, _, _ = customer
    deliver(proc, "customer.subscription.deleted", {"id": "sub_r", "object": "subscription", "status": "canceled",
                                                    "cancellation_details": {"reason": "cancellation_requested"},
                                                    "metadata": {"tier": "api"}}, "evt_v1")
    ev = state._one("SELECT * FROM churn_events")
    assert ev["reason"] == "cancellation_requested" and ev["had_dunning"] == 0


def test_portal_failure_falls_back_to_lander(kit, state, customer, transport):
    proc, _, _ = customer
    transport.add_json(f"{STRIPE}/billing_portal/sessions", {"error": {"message": "portal not configured"}}, status=400)
    failed(proc)
    assert "https://pages.example.com" in FakeSMTP.sent[-1].get_body(("plain",)).get_content()
    assert any("billing portal" in e["message"] for e in state.recent_errors(5))


def test_unknown_subscription_is_ignored(kit, state):
    proc = WebhookProcessor(kit, use_sdk=False)
    out = deliver(proc, "invoice.payment_failed", {"id": "in_x", "object": "invoice", "subscription": "sub_nope"}, "evt_x")
    assert out.status == 200 and state._one("SELECT COUNT(*) AS n FROM dunning_cases")["n"] == 0
