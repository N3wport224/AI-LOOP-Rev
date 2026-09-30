import asyncio
import json
import threading
import time
import urllib.request

import pytest

from strategies.distribution_engine import fulfil_order
from tests.conftest import NOW
from tests.test_distribution import FakeSMTP, audit, go_live, pipeline
from tools import build_toolkit
from tools.storefront import Order
from tools.storefront.webhook_listener import (
    SignatureError, WebhookProcessor, WebhookServer, build_app, pending_key, sign_payload, verify_event,
)

SECRET = "whsec_test_secret"


def event(etype, obj, eid="evt_1"):
    return json.dumps({"id": eid, "object": "event", "type": etype, "data": {"object": obj}}).encode()


def session(sid="cs_1", link="plink_1", email="buyer@acme.example", amount=900, status="complete", paid="paid", pi="pi_1", **kw):
    s = {"id": sid, "object": "checkout.session", "payment_link": link, "amount_total": amount, "status": status,
         "payment_status": paid, "payment_intent": pi, "customer_details": {"email": email}, "created": int(NOW.timestamp())}
    s.update(kw)
    return s


@pytest.fixture
def smtp():
    FakeSMTP.sent = []
    FakeSMTP.fail = False
    return FakeSMTP


@pytest.fixture
def kit(config, state, breaker, transport, smtp):
    config.stripe_webhook_secret = SECRET
    return build_toolkit(config, state, breaker, transport=transport, sleep=lambda s: None, smtp_factory=smtp)


@pytest.fixture
def asset(kit, state, make_hypothesis):
    hyp = make_hypothesis()
    pipeline(kit, hyp)
    a = state.latest_asset(hyp["id"], "lead_directory")
    state.update_asset(a["id"], product_ref="plink_1", checkout_url="https://buy.stripe.com/t", provider="stripe", status="published")
    return state.get_asset(a["id"])


def post(proc, payload, secret=SECRET, ts=None):
    return proc.handle(payload, sign_payload(payload, secret, ts))


@pytest.fixture
def proc(kit):
    return WebhookProcessor(kit)  # the Stripe SDK checks timestamps against the real clock


# ------------------------------------------------------------------ verification
@pytest.mark.parametrize("use_sdk", [True, False])
def test_valid_signature_accepted_both_verifiers(use_sdk):
    payload = event("checkout.session.completed", session())
    now = time.time()
    ev = verify_event(payload, sign_payload(payload, SECRET, int(now)), SECRET, 300, lambda: now, use_sdk=use_sdk)
    assert ev["id"] == "evt_1" and ev["data"]["object"]["payment_link"] == "plink_1"


@pytest.mark.parametrize("use_sdk", [True, False])
def test_tampered_and_stale_payloads_rejected(use_sdk):
    payload = event("checkout.session.completed", session())
    now = time.time()
    header = sign_payload(payload, SECRET, int(now))
    with pytest.raises(SignatureError):
        verify_event(payload.replace(b"900", b"1"), header, SECRET, 300, lambda: now, use_sdk=use_sdk)
    with pytest.raises(SignatureError):
        verify_event(payload, sign_payload(payload, "whsec_other", int(now)), SECRET, 300, lambda: now, use_sdk=use_sdk)
    stale = sign_payload(payload, SECRET, int(now) - 301)  # correctly signed, but outside the tolerance: a replay
    with pytest.raises(SignatureError):
        verify_event(payload, stale, SECRET, 300, lambda: now, use_sdk=use_sdk)
    with pytest.raises(SignatureError):
        verify_event(payload, "garbage", SECRET, 300, lambda: now, use_sdk=use_sdk)
    with pytest.raises(SignatureError):
        verify_event(payload, None, SECRET)


def test_processor_rejects_bad_requests(proc, kit, state):
    payload = event("checkout.session.completed", session())
    assert proc.handle(payload, "t=1,v1=deadbeef").status == 400
    assert proc.handle(payload, None).status == 400
    assert state.recent_errors(1, kind="security")
    assert WebhookProcessor(kit, secret="").handle(payload, "x").status == 503
    assert proc.handle(b"x" * (600 * 1024), "x").status == 413
    assert state.list_orders() == []


# ------------------------------------------------------------------ reconciliation
def test_checkout_completed_records_revenue_and_queues_fulfilment(proc, state, asset, kit):
    out = post(proc, event("checkout.session.completed", session()))
    assert out.status == 200 and out.body["status"] == "processed"
    order = state.get_order("stripe", "cs_1")
    assert order["asset_id"] == asset["id"] and order["email"] == "buyer@acme.example" and order["status"] == "paid"
    assert [o["id"] for o in out.fulfil] == [order["id"]]
    assert state.revenue_for_hypothesis(asset["hypothesis_id"]) == 900 - 56
    assert kit.revenue.daily_summary()["net_cents"] == 844
    assert state.get("last_sale")["order"] == "cs_1"


def test_duplicate_event_is_idempotent(proc, state, asset):
    payload = event("checkout.session.completed", session())
    first = post(proc, payload)
    again = post(proc, payload, ts=int(time.time()) + 5)  # Stripe retry: fresh signature, same event id
    assert first.fulfil and again.status == 200 and again.body["duplicate"] and not again.fulfil
    assert len(state.list_orders()) == 1 and len(state.list_revenue()) == 1


def test_same_session_from_two_events_and_polling_counts_once(proc, state, asset, kit):
    post(proc, event("checkout.session.completed", session(), "evt_a"))
    out = post(proc, event("checkout.session.async_payment_succeeded", session(), "evt_b"))
    assert out.status == 200 and "already recorded" in out.body["detail"]  # re-offered for delivery: the claim makes that safe
    rep = kit.revenue.record_orders([Order("stripe", "cs_1", "buyer@acme.example", 900, "plink_1", NOW.isoformat())], 2.9, 30)
    assert rep.new == 0
    assert len(state.list_revenue()) == 1


def test_async_payment_completed_by_payment_intent(proc, state, asset):
    ts = int(NOW.timestamp())
    out = post(proc, event("checkout.session.completed", session(paid="unpaid"), "evt_a"))
    assert out.body["detail"] == "awaiting asynchronous payment" and state.list_orders() == []
    pi = {"id": "pi_1", "object": "payment_intent", "amount_received": 900, "created": ts}
    out = post(proc, event("payment_intent.succeeded", pi, "evt_b"))
    assert state.get_order("stripe", "cs_1")["status"] == "paid" and out.fulfil
    # a second payment_intent event for the same intent adds nothing
    assert post(proc, event("payment_intent.succeeded", pi, "evt_c")).body["status"] == "ignored"


def test_payment_intent_without_session_is_ignored(proc, state):
    out = post(proc, event("payment_intent.succeeded", {"id": "pi_x", "amount_received": 500}))
    assert out.body["status"] == "ignored" and state.list_orders() == []


def test_expired_session_counts_as_initiation(proc, state):
    post(proc, event("checkout.session.expired", session(status="expired", paid="unpaid")))
    assert state.initiations_for_refs(["plink_1"]) == 1 and state.list_orders() == []


def test_metadata_asset_attribution_without_payment_link(proc, state, asset):
    s = session(link=None, metadata={"asset_id": str(asset["id"])})
    post(proc, event("checkout.session.completed", s))
    assert state.get_order("stripe", "cs_1")["asset_id"] == asset["id"]


def test_unmatched_sale_still_counts_but_needs_manual_delivery(proc, state, kit):
    out = post(proc, event("checkout.session.completed", session(link="plink_unknown")))
    assert "manual delivery" in out.body["detail"] and not out.fulfil
    assert state.get_order("stripe", "cs_1")["status"] == "needs_manual_delivery"
    assert kit.revenue.daily_summary()["net_cents"] == 844


def test_processing_failure_returns_500_and_allows_retry(proc, state, asset, monkeypatch):
    calls = {"n": 0}
    real = proc.tools.revenue.record_orders

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db busy")
        return real(*a, **k)

    monkeypatch.setattr(proc.tools.revenue, "record_orders", flaky)
    payload = event("checkout.session.completed", session())
    assert post(proc, payload).status == 500
    assert post(proc, payload).body["status"] == "processed"
    assert len(state.list_orders()) == 1


def test_unhandled_event_types_acknowledged(proc, state):
    out = post(proc, event("customer.created", {"id": "cus_1"}))
    assert out.status == 200 and out.body["ignored"] == "customer.created"


# ------------------------------------------------------------------ fulfilment
def test_instant_fulfilment_sends_zip_and_receipt_once(proc, state, asset, config, smtp):
    go_live(config)
    out = post(proc, event("checkout.session.completed", session()))
    order = out.fulfil[0]
    results = []
    threads = [threading.Thread(target=lambda: results.append(fulfil_order(proc.tools, order))) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count("delivered") >= 1 and len(smtp.sent) == 1, results
    msg = smtp.sent[0]
    body = msg.get_body(("plain",)).get_content()
    assert "Receipt" in body and "stripe:cs_1" in body and "$9.00 USD" in body
    assert list(msg.iter_attachments())[0].get_filename().endswith(".zip")
    assert state.get_order("stripe", "cs_1")["status"] == "delivered"


def test_stale_delivery_claim_is_released(state, asset, clock):
    state.record_order("stripe", "cs_9", "b@x.com", 900, "plink_1", asset["id"], asset["hypothesis_id"])
    oid = state.get_order("stripe", "cs_9")["id"]
    assert state.claim_order_for_delivery(oid) and not state.claim_order_for_delivery(oid)
    assert state.reset_stale_deliveries() == 0
    clock.advance(minutes=16)
    assert state.reset_stale_deliveries() == 1 and state.get_order("stripe", "cs_9")["status"] == "paid"


# ------------------------------------------------------------------ HTTP server
def test_aiohttp_app_end_to_end(kit, state, asset, config):
    from aiohttp.test_utils import TestClient, TestServer

    proc = WebhookProcessor(kit)  # real clock: sign with the current time

    async def scenario():
        app = build_app(proc, "/webhook")
        async with TestClient(TestServer(app)) as client:
            payload = event("checkout.session.completed", session())
            r = await client.post("/webhook", data=payload, headers={"Stripe-Signature": sign_payload(payload, SECRET)})
            body = await r.json()
            bad = await client.post("/webhook", data=payload, headers={"Stripe-Signature": "t=1,v1=00"})
            health = await (await client.get("/healthz")).json()
            await asyncio.wait(list(app[pending_key()]), timeout=5) if app[pending_key()] else None
            return r.status, body, bad.status, health

    status, body, bad, health = asyncio.run(scenario())
    assert (status, bad) == (200, 400) and body["status"] == "processed"
    assert health["ok"] and health["events"] == {"processed": 1}
    # fulfilment ran after the response: dry run -> delivery written to the audit log
    assert any(r["kind"] == "delivery" for r in audit(config))


def test_webhook_server_on_real_socket(kit, state, asset):
    server = WebhookServer(kit, host="127.0.0.1", port=0)
    stop = threading.Event()
    t = threading.Thread(target=server.run, args=(stop,), daemon=True)
    t.start()
    assert server.started.wait(5)
    payload = event("checkout.session.completed", session())
    req = urllib.request.Request(
        f"http://127.0.0.1:{server.bound_port}/webhook", data=payload, method="POST",
        headers={"Stripe-Signature": sign_payload(payload, SECRET), "Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=5) as resp:
        assert resp.status == 200
    stop.set()
    t.join(5)
    assert not t.is_alive()
    assert state.get_order("stripe", "cs_1") is not None
