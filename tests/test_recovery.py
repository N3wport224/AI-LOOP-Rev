"""Phase 8: self-service order recovery (POST /v1/orders/recover)."""

import re
import pytest

from api.auth import ApiKeys
from strategies.lead_magnet import CaptureLimiter
from tests.test_api import api_tier, call, deliver
from tests.test_distribution import FakeSMTP
from tests import test_retention
from tools.storefront.recovery_endpoint import GENERIC, RecoveryService
from tools.storefront.webhook_listener import WebhookProcessor

kit = test_retention.kit  # shared fixture

BUYER = "buyer@shop.example"


class Tick:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def bought(kit, state):
    """One delivered $14 dataset order for BUYER."""
    kit.files.write_bytes("assets/python-remote/python-remote-v1.zip", b"PK\x05\x06" + b"\0" * 18)
    aid = state.add_asset(kit.hyp["id"], "lead_directory", "Python Remote Leads", "assets/python-remote/python-remote-v1.zip", 1, 40, 1400)
    state.record_order("stripe", "cs_order_12345678", BUYER, 1400, "plink_1", aid, kit.hyp["id"], status="delivered")
    return aid


def svc_for(kit, per_ip=3):
    return RecoveryService(kit, CaptureLimiter(per_ip, Tick()))


def test_validation_and_identical_responses(kit, bought):
    svc = svc_for(kit, per_ip=100)
    for bad in ({}, {"email": ""}, {"email": "not-an-email"}, {"email": 42}, {"email": "a@b"}, []):
        status, body, _, after = svc.request(bad, "1.1.1.1")
        assert status == 400 and body["error"]["code"] == "invalid_parameter" and after is None
    s1, b1, _, a1 = svc.request({"email": BUYER}, "1.1.1.1")
    s2, b2, _, a2 = svc.request({"email": "stranger@nowhere.example"}, "1.1.1.1")
    assert s1 == s2 == 202 and b1 == b2 and b1["message"] == GENERIC  # no customer enumeration
    assert a2() == {"sent": False, "reason": "no purchases"} and FakeSMTP.sent == []
    assert a1()["sent"] and FakeSMTP.sent[-1]["To"] == BUYER


def test_ip_rate_limit_three_per_hour(kit):
    tick = Tick()
    svc = RecoveryService(kit, CaptureLimiter(kit.config.recovery_per_ip_hour, tick))
    assert [svc.request({"email": BUYER}, "9.9.9.9")[0] for _ in range(4)] == [202, 202, 202, 429]
    status, body, headers, after = svc.request({"email": BUYER}, "9.9.9.9")
    assert status == 429 and headers["Retry-After"] == "3600" and body["error"]["code"] == "rate_limited" and after is None
    assert svc.request({"email": BUYER}, "8.8.8.8")[0] == 202  # per IP
    tick.t += 3601
    assert svc.request({"email": BUYER}, "9.9.9.9")[0] == 202


def test_per_email_daily_cap(kit, bought, clock):
    svc = svc_for(kit, per_ip=100)
    results = [svc.dispatch(BUYER)["sent"] for _ in range(4)]
    assert results == [True, True, True, False] and len(FakeSMTP.sent) == 3
    clock.advance(days=1, seconds=1)
    assert svc.dispatch(BUYER)["sent"]


def test_resends_the_zip_to_the_buyer_only(kit, bought):
    out = svc_for(kit).dispatch(BUYER.upper())  # normalised by request(); dispatch takes the normalised form
    assert out == {"sent": False, "reason": "no purchases"}
    out = svc_for(kit).dispatch(BUYER)
    assert out["sent"] and out["files"] == 1 and out["api_links"] == 0
    msg = FakeSMTP.sent[-1]
    names = [p.get_filename() for p in msg.iter_attachments()]
    assert msg["To"] == BUYER and names == ["python-remote-v1.zip"]
    assert "Python Remote Leads" in msg.get_body(("plain",)).get_content()


def test_http_route_is_public_and_limited(kit, bought):
    async def s(client):
        r = await client.post("/v1/orders/recover", json={"email": "nope"})
        assert r.status == 400 and (await r.json())["error"]["param"] == "email"
        r = await client.post("/v1/orders/recover", data={"email": BUYER})  # an HTML form works too
        assert r.status == 202 and (await r.json())["message"] == GENERIC
        r = await client.post("/v1/orders/recover", json={"email": "x@y.example"})
        assert r.status == 202
        r = await client.post("/v1/orders/recover", json={"email": BUYER})
        assert r.status == 429 and r.headers["Retry-After"] == "3600"
        r = await client.post("/v1/orders/recover", data=b"{", headers={"Content-Type": "application/json"})
        assert r.status in (400, 429)
    call(kit, s)


def test_api_key_needs_a_confirmed_click(kit, state, config, clock):
    api_tier(state, kit.hyp["id"])
    proc = WebhookProcessor(kit, use_sdk=False)
    deliver(proc, "checkout.session.completed", {
        "id": "cs_k", "object": "checkout.session", "mode": "subscription", "payment_link": "plink_api", "subscription": "sub_k",
        "customer": "cus_k", "amount_total": 2900, "payment_status": "paid", "status": "complete",
        "customer_details": {"email": "dev@corp.example"}}, "evt_k0")
    old_key = re.search(r"am_live_[0-9a-f]{48}", FakeSMTP.sent[-1].get_body(("plain",)).get_content()).group(0)
    svc = svc_for(kit)
    out = svc.dispatch("dev@corp.example")
    assert out["sent"] and out["api_links"] == 1
    body = FakeSMTP.sent[-1].get_body(("plain",)).get_content()
    token = re.search(r"/v1/orders/recover/confirm\?t=([\w-]+)", body).group(1)
    assert ApiKeys(state, config).lookup(old_key)["status"] == "active"  # asking changed nothing
    assert state._one("SELECT token_hash FROM recovery_tokens")["token_hash"] != token  # stored hashed

    async def s(client):
        r = await client.get(f"/v1/orders/recover/confirm?t={token}")  # a mail scanner's prefetch
        assert r.status == 200 and "Issue a new API key" in await r.text()
        assert ApiKeys(state, config).lookup(old_key)["status"] == "active"
        r = await client.post("/v1/orders/recover/confirm", data={"t": token})
        assert r.status == 200 and "1 new key" in await r.text()
        r = await client.post("/v1/orders/recover/confirm", data={"t": token})  # one-time
        assert r.status == 404
        r = await client.get("/v1/orders/recover/confirm?t=bogus")
        assert r.status == 404
    call(kit, s)
    assert ApiKeys(state, config).lookup(old_key)["status"] == "rotated"
    new_key = re.search(r"am_live_[0-9a-f]{48}", FakeSMTP.sent[-1].get_body(("plain",)).get_content()).group(0)
    assert new_key != old_key and ApiKeys(state, config).lookup(new_key)["status"] == "active"

    svc.dispatch("dev@corp.example")  # a second link expires after 24 h
    token2 = re.search(r"confirm\?t=([\w-]+)", FakeSMTP.sent[-1].get_body(("plain",)).get_content()).group(1)
    clock.advance(hours=24, seconds=1)
    assert svc.confirm(token2) == -1
