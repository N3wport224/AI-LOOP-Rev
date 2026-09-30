"""Developer API tier: keys, auth, throttling, quota, queries, OpenAPI conformance, Stripe lifecycle."""

import asyncio
import json
import re
from datetime import timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agent.power import NullBackend, PowerManager
from api.auth import API_KIND, KEY_RE, ApiKeys, hash_key, provision
from api.data import SignalIndex, company_id, record_history
from api.openapi import spec, validate
from api.ratelimit import FailureLimiter, TokenBucket
from api.server import seconds_to_utc_midnight
from strategies.base import TaskContext
from tests.conftest import NOW
from tests.test_distribution import FakeSMTP, go_live
from tools import build_toolkit
from tools.storefront.webhook_listener import WebhookProcessor, build_app, sign_payload

SECRET = "whsec_test_api"
DOC = spec("https://hooks.example.com")


def radar(prefix, n, stack, **extra):
    out = []
    for i in range(n):
        out.append({
            "company": f"{prefix}{i}", "domain": f"{prefix.lower()}{i}.example", "stack": stack, "openings": 1 + i % 3,
            "open_positions": [f"Engineer {i}"], "intent_score": 90 - 5 * i, "urgency_score": 40 + i,
            "intent_level": "High" if 90 - 5 * i >= 60 else "Medium", "intent_category": "migration",
            "intent_tag": f"Urgency: {'High' if 90 - 5 * i >= 60 else 'Medium'} (Cloud Migration)",
            "commercial_signals": ["Cloud Migration"], "migration_path": "On-prem → AWS",
            "latest_posted_at": (NOW - timedelta(days=i)).isoformat(), "careers_url": f"https://{prefix.lower()}{i}.example/jobs",
            "careers_url_verified": i % 2 == 0, "remote_friendly": True, "contact_email": "hr@x.example",
            "intent_evidence": ["secret phrase"], **extra,
        })
    return out


@pytest.fixture
def kit(config, state, breaker, transport, make_hypothesis):
    FakeSMTP.sent, FakeSMTP.fail = [], False
    config.stripe_webhook_secret = SECRET
    config.public_webhook_url = "https://hooks.example.com/webhook"
    config.api_burst, config.api_rate_per_second = 1000, 1000.0
    k = build_toolkit(config, state, breaker, transport=transport, sleep=lambda s: None, smtp_factory=FakeSMTP)
    k.files.write_json("exports/intel/python-remote/tech_radar.json", radar("Py", 12, ["Python", "AWS"]))
    both = radar("Py", 1, ["Kubernetes"])[0] | {"intent_score": 20, "intent_level": "Low", "intent_tag": "Urgency: Low (Head of Infrastructure)",
                                                 "commercial_signals": ["Head of Infrastructure"], "open_positions": ["SRE"]}
    k.files.write_json("exports/intel/devops-sre/tech_radar.json", radar("Ops", 4, ["Kubernetes", "Terraform"]) + [both])
    hyp = make_hypothesis()
    k.hyp = hyp
    return k


def app_for(kit):
    return build_app(WebhookProcessor(kit, use_sdk=False), power=PowerManager(NullBackend()))


def call(kit, scenario):
    app = app_for(kit)

    async def go():
        async with TestClient(TestServer(app)) as client:
            await scenario(client)

    asyncio.run(go())


def bearer(key):
    return {"Authorization": f"Bearer {key}"}


# ------------------------------------------------------------------ keys
def test_keys_are_random_hashed_and_never_stored(kit, state):
    keys = ApiKeys(state, kit.config)
    key, row = keys.issue(None, "Dev@Example.com")
    other, _ = keys.issue(None, "x@example.com")
    assert KEY_RE.match(key) and key != other and key.startswith("am_live_") and len(key) == 8 + 48
    assert row["key_hash"] == hash_key(key) and row["prefix"] == key[:14] and row["email"] == "dev@example.com"
    dump = json.dumps(state._all("SELECT * FROM api_keys"))
    assert key not in dump and key[14:] not in dump
    assert keys.lookup(key)["id"] == row["id"]
    assert keys.lookup(key[:-1] + ("0" if key[-1] != "0" else "1")) is None
    assert keys.lookup("am_live_short") is None and keys.lookup("") is None


def test_token_bucket_math():
    b = TokenBucket(capacity=3, rate=1.0, tokens=3, updated=0.0)
    assert [b.take(0.0)[0] for _ in range(4)] == [True, True, True, False]
    ok, wait = b.take(0.0)
    assert not ok and wait == pytest.approx(1.0)
    assert b.take(0.5) == (False, pytest.approx(0.5))
    assert b.take(1.0)[0] and not b.take(1.0)[0]
    assert b.take(100.0)[0] and b.remaining() == 2  # refills to capacity, never beyond
    lim = FailureLimiter(2, clock=lambda: 10.0)
    lim.record("1.1.1.1")
    assert lim.blocked("1.1.1.1") == 0
    lim.record("1.1.1.1")
    assert lim.blocked("1.1.1.1") > 3500 and lim.blocked("2.2.2.2") == 0


# ------------------------------------------------------------------ authentication
def test_authentication_errors(kit, state):
    keys = ApiKeys(state, kit.config)
    key, row = keys.issue(None, "d@example.com")
    dead, dead_row = keys.issue(None, "e@example.com")
    keys.set_status(dead_row["id"], "revoked", "subscription ended")
    kit.config.api_auth_failures_per_ip_hour = 3

    async def s(client):
        r = await client.get("/v1/signals")
        body = await r.json()
        assert r.status == 401 and body["error"]["code"] == "missing_api_key" and "Bearer" in r.headers["WWW-Authenticate"]
        validate(body, {"$ref": "#/components/schemas/Error"}, DOC)
        r = await client.get("/v1/signals", headers=bearer(dead))
        assert r.status == 401 and (await r.json())["error"]["code"] == "key_revoked"
        r = await client.get("/v1/signals", headers={"X-API-Key": key})
        assert r.status == 200 and r.headers["X-Request-Id"].startswith("req_")
        r = await client.get("/v1/signals?api_key=" + key)  # keys in URLs aren't accepted
        assert r.status == 401
        r = await client.get("/v1/signals", headers=bearer("am_live_" + "0" * 48))
        assert (await r.json())["error"]["code"] == "invalid_api_key"  # third failure (missing, key in URL, wrong)
        r = await client.get("/v1/signals", headers=bearer(key))  # this client is now blocked, even with a good key
        assert r.status == 429 and (await r.json())["error"]["code"] == "too_many_auth_failures" and int(r.headers["Retry-After"]) > 0
        r = await client.get("/v1/signals", headers={**bearer(key), "CF-Connecting-IP": "203.0.113.9"})
        assert r.status == 200  # another visitor behind the tunnel is unaffected
    call(kit, s)


# ------------------------------------------------------------------ queries
def test_signals_filters_merge_and_conform(kit, state):
    key, _ = ApiKeys(state, kit.config).issue(None, "d@example.com")

    async def s(client):
        r = await client.get("/v1/signals?limit=100", headers=bearer(key))
        body = await r.json()
        validate(body, {"$ref": "#/components/schemas/SignalList"}, DOC)
        assert body["pagination"]["total"] == 16  # 12 + 4, Py0 merged across both niches
        py0 = next(x for x in body["data"] if x["company_id"] == "py0")
        assert py0["niches"] == ["devops-sre", "python-remote"] and py0["stack"] == ["AWS", "Kubernetes", "Python"]
        assert py0["intent_score"] == 90 and py0["open_positions"] == ["Engineer 0", "SRE"]
        assert "contact_email" not in py0 and "intent_evidence" not in py0
        scores = [(x["intent_score"], x["urgency_score"]) for x in body["data"]]
        assert scores == sorted(scores, key=lambda t: (-t[0], -t[1]))
        r = await client.get("/v1/signals?tech=kubernetes&limit=100", headers=bearer(key))
        assert {x["company_id"] for x in (await r.json())["data"]} == {"py0", "ops0", "ops1", "ops2", "ops3"}
        r = await client.get("/v1/signals?intent_tag=high&min_urgency=42&limit=100", headers=bearer(key))
        data = (await r.json())["data"]
        assert data and all(x["intent_level"] == "High" and x["urgency_score"] >= 42 for x in data)
        since = (NOW - timedelta(days=2)).date().isoformat()
        r = await client.get(f"/v1/signals?since={since}&limit=100", headers=bearer(key))
        assert all(x["latest_posted_at"] >= since for x in (await r.json())["data"])
        a = await (await client.get("/v1/signals?tech=python&limit=3", headers=bearer(key))).text()
        b = await (await client.get("/v1/signals?tech=python&limit=3", headers=bearer(key))).text()
        assert a == b  # deterministic
    call(kit, s)


@pytest.mark.parametrize("query,param,code", [
    ("min_urgency=101", "min_urgency", "invalid_parameter"),
    ("min_urgency=abc", "min_urgency", "invalid_parameter"),
    ("limit=0", "limit", "invalid_parameter"),
    ("limit=1000", "limit", "invalid_parameter"),
    ("since=yesterday", "since", "invalid_parameter"),
    ("color=blue", "color", "invalid_parameter"),
    ("cursor=%%%", "cursor", "invalid_cursor"),
])
def test_bad_parameters_are_machine_readable(kit, state, query, param, code):
    key, _ = ApiKeys(state, kit.config).issue(None, "d@example.com")

    async def s(client):
        r = await client.get(f"/v1/signals?{query}", headers=bearer(key))
        body = await r.json()
        assert r.status == 400 and body["error"]["code"] == code and body["error"]["param"] == param
        assert body["error"]["doc_url"] == "https://hooks.example.com/docs/api#errors"
        assert "X-RateLimit-Remaining" in r.headers
    call(kit, s)


def test_pagination_walks_everything_once_and_cursors_are_bound(kit, state):
    key, _ = ApiKeys(state, kit.config).issue(None, "d@example.com")

    async def s(client):
        seen, cursor, pages = [], None, 0
        while True:
            url = "/v1/signals?limit=5" + (f"&cursor={cursor}" if cursor else "")
            body = await (await client.get(url, headers=bearer(key))).json()
            seen += [x["company_id"] for x in body["data"]]
            pages += 1
            cursor = body["pagination"]["next_cursor"]
            assert body["pagination"]["has_more"] is (cursor is not None)
            if not cursor:
                break
        assert pages == 4 and len(seen) == len(set(seen)) == 16
        first = await (await client.get("/v1/signals?limit=5", headers=bearer(key))).json()
        c = first["pagination"]["next_cursor"]
        r = await client.get(f"/v1/signals?limit=5&tech=python&cursor={c}", headers=bearer(key))
        assert (await r.json())["error"]["code"] == "invalid_cursor"
        kit.files.write_json("exports/intel/devops-sre/tech_radar.json", radar("Ops", 5, ["Kubernetes"]))  # data refresh
        r = await client.get(f"/v1/signals?limit=5&cursor={c}", headers=bearer(key))
        assert r.status == 400 and (await r.json())["error"]["code"] == "cursor_expired"
    call(kit, s)


def test_company_footprint_history_and_postings(kit, state, clock):
    key, _ = ApiKeys(state, kit.config).issue(None, "d@example.com")
    record_history(state, [{"company": "Py0", "domain": "py0.example", "stack": ["Python"], "intent_score": 30,
                            "intent_tag": "Urgency: Low (Legacy Modernization)"}], NOW - timedelta(days=10))
    record_history(state, [{"company": "Py0", "domain": "py0.example", "stack": ["Python"], "intent_score": 30,
                            "intent_tag": "Urgency: Low (Legacy Modernization)"}], NOW - timedelta(days=9))  # unchanged: folded
    record_history(state, radar("Py", 1, ["Python", "AWS"]), NOW)
    from strategies.b2b_lead_aggregator import POOL_NICHE

    state.upsert_lead("k1", POOL_NICHE, {"company": "Py0 Inc", "title": "Platform Engineer", "url": "https://board.example/1",
                                         "posted_at": NOW.isoformat(), "description": "SECRET DESCRIPTION",
                                         "contact_email": "hr@py0.example", "source": "remoteok", "remote": True})
    clock.advance(days=1)
    state.upsert_lead("k2", POOL_NICHE, {"company": "Other", "title": "x", "url": "u"})

    async def s(client):
        r = await client.get("/v1/companies/py0.example", headers=bearer(key))
        body = await r.json()
        validate(body, {"$ref": "#/components/schemas/Company"}, DOC)
        assert body["company"] == "Py0" and body["niches"] == ["devops-sre", "python-remote"]
        assert [h["intent_tag"] for h in body["history"]] == ["Urgency: Low (Legacy Modernization)", "Urgency: High (Cloud Migration)"]
        assert body["history"][-1]["migration_path"] == "On-prem → AWS"
        assert [p["title"] for p in body["active_postings"]] == ["Platform Engineer"]
        assert "SECRET" not in json.dumps(body) and "hr@py0" not in json.dumps(body)
        assert (await client.get("/v1/companies/py0", headers=bearer(key))).status == 200  # by company_id
        assert (await client.get("/v1/companies/www.py0.example", headers=bearer(key))).status == 200
        r = await client.get("/v1/companies/nobody.example", headers=bearer(key))
        assert r.status == 404 and (await r.json())["error"]["code"] == "not_found"
        r = await client.get("/v1/companies/bad%20name", headers=bearer(key))
        assert r.status == 400
    call(kit, s)
    assert company_id("ACME, Inc.") == "acme"


# ------------------------------------------------------------------ throttling and quota
def test_burst_limit_then_daily_quota(kit, state, clock):
    kit.config.api_burst, kit.config.api_rate_per_second = 3, 0.001
    kit.config.api_daily_quota = 5
    key, row = ApiKeys(state, kit.config).issue(None, "d@example.com")

    async def s(client):
        codes = [(await client.get("/v1/me", headers=bearer(key))).status for _ in range(3)]
        assert codes == [200, 200, 200]
        r = await client.get("/v1/me", headers=bearer(key))
        body = await r.json()
        assert r.status == 429 and body["error"]["code"] == "rate_limited" and int(r.headers["Retry-After"]) >= 1
        assert r.headers["X-RateLimit-Remaining"] == "2"  # a throttled request doesn't use quota
    call(kit, s)

    kit.config.api_burst, kit.config.api_rate_per_second = 100, 100.0

    async def s2(client):  # a fresh server (restart): the daily quota survives in SQLite
        remaining = []
        for _ in range(2):
            r = await client.get("/v1/me", headers=bearer(key))
            remaining.append(r.headers["X-RateLimit-Remaining"])
        assert remaining == ["1", "0"]
        r = await client.get("/v1/me", headers=bearer(key))
        body = await r.json()
        assert r.status == 429 and body["error"]["code"] == "quota_exceeded"
        assert int(r.headers["Retry-After"]) == seconds_to_utc_midnight(NOW) == 12 * 3600
        validate(body, {"$ref": "#/components/schemas/Error"}, DOC)
    call(kit, s2)
    clock.advance(hours=12, seconds=1)  # past 00:00 UTC

    async def s3(client):
        r = await client.get("/v1/me", headers=bearer(key))
        assert r.status == 200 and r.headers["X-RateLimit-Remaining"] == "4"
    call(kit, s3)
    assert ApiKeys(state, kit.config).volume(3, clock()) == [{"day": "2026-09-30", "requests": 5}, {"day": "2026-10-01", "requests": 1}]


def test_rotation(kit, state):
    key, row = ApiKeys(state, kit.config).issue(7, "d@example.com")

    async def s(client):
        r = await client.post("/v1/auth/rotate", headers=bearer(key))
        body = await r.json()
        assert r.status == 201
        validate(body, {"$ref": "#/components/schemas/RotatedKey"}, DOC)
        new = body["key"]
        assert new != key and body["previous_prefix"] == row["prefix"]
        r = await client.get("/v1/me", headers=bearer(key))
        assert r.status == 401 and (await r.json())["error"]["code"] == "key_revoked"
        r = await client.get("/v1/me", headers=bearer(new))
        info = await r.json()
        validate(info, {"$ref": "#/components/schemas/KeyInfo"}, DOC)
        assert r.status == 200 and info["used_today"] == 1
        assert (await client.get("/v1/auth/rotate", headers=bearer(new))).status == 405
    call(kit, s)
    assert ApiKeys(state, kit.config).for_subscriber(7)[0]["rotated_from"] == row["id"]


def test_routing_docs_and_openapi(kit):
    async def s(client):
        r = await client.get("/v1/nope")
        assert r.status == 404 and (await r.json())["error"]["code"] == "not_found"
        r = await client.delete("/v1/signals")
        assert r.status == 405 and (await r.json())["error"]["code"] == "method_not_allowed" and r.headers["Allow"] == "GET"
        doc = await (await client.get("/openapi.json")).json()
        assert doc["openapi"] == "3.1.0" and set(doc["paths"]) == {"/v1/signals", "/v1/companies/{domain}", "/v1/me", "/v1/auth/rotate",
                                                                  "/v1/orders/recover"}
        assert doc["servers"][0]["url"] == "https://hooks.example.com"
        assert {"bearerAuth", "apiKeyHeader"} == set(doc["components"]["securitySchemes"])
        for op in (doc["paths"]["/v1/signals"]["get"], doc["paths"]["/v1/companies/{domain}"]["get"]):
            assert {"200", "400", "401", "429"} <= set(op["responses"])
            assert "Retry-After" in op["responses"]["429"]["headers"]
        r = await client.get("/docs/api")
        html = await r.text()
        assert r.status == 200 and "script-src 'self'" in r.headers["Content-Security-Policy"]
        assert re.findall(r'<script src="([^"]+)"', html) == ["/docs/api/console.js"]  # no third-party JS
        assert "curl -H" in html and "/v1/signals" in html and "quota_exceeded" in html
        assert (await client.get("/docs/api/console.js")).status == 200
        assert (await client.post("/webhook", data=b"{}")).status == 400  # the webhook is untouched
    call(kit, s)


def test_api_disabled(kit):
    kit.config.api_enabled = False

    async def s(client):
        assert (await client.get("/v1/signals")).status == 404
        assert (await client.get("/openapi.json")).status == 404
    call(kit, s)


def test_signal_index_caches_until_files_change(kit, state):
    idx = SignalIndex(kit.files, state)
    v1 = idx.stats()["version"]
    assert idx.stats()["version"] == v1 and idx.stats()["companies"] == 16
    kit.files.write_json("exports/intel/new-niche/tech_radar.json", radar("New", 2, ["Rust"]))
    assert idx.stats()["version"] != v1 and idx.stats()["companies"] == 18


# ------------------------------------------------------------------ Stripe lifecycle
def event(etype, obj, eid):
    return json.dumps({"id": eid, "object": "event", "type": etype, "data": {"object": obj}}).encode()


def api_tier(state, hyp_id):
    aid = state.add_asset(hyp_id, API_KIND, "Developer API", "https://hooks.example.com/docs/api", 1, 0, 2900,
                          product_ref="plink_api")
    state.update_asset(aid, provider="stripe", checkout_url="https://buy.stripe.com/api", status="published",
                       kind_meta=json.dumps({"interval": "month", "tier": "api"}))
    return aid


def deliver(proc, etype, obj, eid):
    out = proc.handle(event(etype, obj, eid), sign_payload(event(etype, obj, eid), SECRET))
    for job in out.after:
        job()
    return out


def test_checkout_provisions_a_key_and_emails_it(kit, state, config):
    go_live(config)
    config.sender_email = "api@example.com"
    api_tier(state, kit.hyp["id"])
    proc = WebhookProcessor(kit, use_sdk=False)
    sess = {"id": "cs_api", "object": "checkout.session", "mode": "subscription", "payment_link": "plink_api",
            "subscription": "sub_api", "customer": "cus_1", "amount_total": 2900, "payment_status": "paid", "status": "complete",
            "customer_details": {"email": "dev@corp.example"}, "client_reference_id": "am--devto--w40"}
    out = deliver(proc, "checkout.session.completed", sess, "evt_a1")
    assert out.status == 200
    sub = state.get_subscriber("sub_api")
    assert sub["tier"] == "paid" and sub["channel"] == "devto" and sub["niche"] is None  # counts toward MRR, no dataset emails
    keys = ApiKeys(state, config).for_subscriber(sub["id"])
    assert len(keys) == 1 and keys[0]["status"] == "active" and keys[0]["daily_quota"] == config.api_daily_quota
    msg = FakeSMTP.sent[-1]
    body = msg.get_body(("plain",)).get_content()
    key = re.search(r"am_live_[0-9a-f]{48}", body).group(0)
    assert ApiKeys(state, config).lookup(key)["id"] == keys[0]["id"]
    assert "curl -H \"Authorization: Bearer" in body and "https://hooks.example.com/docs/api" in body and "/v1/auth/rotate" in body
    deliver(proc, "checkout.session.completed", sess, "evt_a2")  # a second delivery of the same checkout
    from strategies.subscription_engine import activate_from_session

    activate_from_session(kit, sess)  # and the polling sweep seeing it
    provision(kit, sub["id"])
    assert len(ApiKeys(state, config).list()) == 1 and len(FakeSMTP.sent) == 1

    # payment fails → degraded at once; paid → restored; cancelled → revoked
    inv = {"id": "in_2", "object": "invoice", "subscription": "sub_api", "amount_paid": 0, "status": "open"}
    deliver(proc, "invoice.payment_failed", inv, "evt_a3")
    row = ApiKeys(state, config).lookup(key)
    assert row["status"] == "degraded" and row["daily_quota"] == config.api_degraded_quota

    async def s(client):
        r = await client.get("/v1/me", headers=bearer(key))
        assert r.status == 200 and r.headers["X-API-Key-Status"] == "past_due" and r.headers["X-RateLimit-Limit"] == "50"
    call(kit, s)
    deliver(proc, "invoice.paid", {**inv, "id": "in_3", "amount_paid": 2900, "status": "paid"}, "evt_a4")
    assert ApiKeys(state, config).lookup(key)["status"] == "active"
    deliver(proc, "customer.subscription.deleted", {"id": "sub_api", "object": "subscription", "status": "canceled",
                                                    "metadata": {"tier": "api", "niche": "api"}}, "evt_a5")
    assert ApiKeys(state, config).lookup(key)["status"] == "revoked"

    async def s2(client):
        r = await client.get("/v1/signals", headers=bearer(key))
        assert r.status == 401 and (await r.json())["error"]["code"] == "key_revoked"
    call(kit, s2)


def test_polling_sweep_keeps_keys_in_sync_without_webhooks(kit, state, config):
    from strategies.subscription_engine import apply_subscription_update

    api_tier(state, kit.hyp["id"])
    sid, _ = state.upsert_subscriber("stripe", "sub_p", email="p@example.com", asset_id=api_tier(state, kit.hyp["id"]),
                                     price_cents=2900)
    assert apply_subscription_update(kit, {"id": "sub_p", "status": "active", "metadata": {"tier": "api"}}) == sid
    keys = ApiKeys(state, config)
    assert keys.for_subscriber(sid)[0]["status"] == "active"  # provisioned by the sweep
    apply_subscription_update(kit, {"id": "sub_p", "status": "past_due", "metadata": {"tier": "api"}})
    assert keys.for_subscriber(sid)[0]["status"] == "degraded"
    apply_subscription_update(kit, {"id": "sub_p", "status": "unpaid", "metadata": {"tier": "api"}})
    assert keys.for_subscriber(sid) == [] and keys.list()[0]["status"] == "revoked"


def test_dry_run_provisioning_raises_an_alert_and_reissue_recovers(kit, state, config, tmp_path):
    api_tier(state, kit.hyp["id"])
    sid, _ = state.upsert_subscriber("stripe", "sub_d", email="d@example.com", asset_id=api_tier(state, kit.hyp["id"]))
    assert provision(kit, sid) == "dry_run"
    assert any("api reissue" in e["message"] for e in state.recent_errors(5, kind="alert"))
    from dashboard import cli
    from rich.console import Console

    cfg_file = tmp_path / "a.toml"
    cfg_file.write_text(f'[automonetize]\ndata_dir = "{config.data_dir}"\nnetwork_check_hosts = []\n')
    assert cli.main(["-c", str(cfg_file), "api", "reissue", str(sid)], console=Console(record=True, width=200)) == 0
    statuses = sorted(r["status"] for r in ApiKeys(state, config).list())
    assert statuses == ["active", "rotated"]


def test_publish_api_tier_creates_the_29_dollar_product(kit, state, config, transport, make_hypothesis):
    from strategies.subscription_engine import SubscriptionEngine

    stripe = "https://api.stripe.com/v1"
    transport.add_json(f"{stripe}/products", {"id": "prod_api"})
    transport.add_json(f"{stripe}/prices", {"id": "price_api"})
    transport.add_json(f"{stripe}/payment_links", {"id": "plink_api", "url": "https://buy.stripe.com/api"})
    config.allow_manual_fulfillment = True
    ctx = TaskContext(kit, kit.hyp, {})
    config.stripe_secret_key = ""
    assert "Stripe" in SubscriptionEngine().run("publish_api_tier", ctx).summary
    config.stripe_secret_key = "sk_test_x"
    kit2 = build_toolkit(config, state, kit.breaker, transport=transport, sleep=lambda s: None)
    config.public_webhook_url = ""
    assert SubscriptionEngine().run("publish_api_tier", TaskContext(kit2, kit.hyp, {})).metrics.get("blocked") == "tunnel"
    config.public_webhook_url = "https://hooks.example.com/webhook"
    res = SubscriptionEngine().run("publish_api_tier", TaskContext(kit2, kit.hyp, {}))
    assert res.metrics["published"] and res.metrics["checkout_url"] == "https://buy.stripe.com/api"
    price = transport.calls_to(f"{stripe}/prices", "POST")[0]["body"].decode()
    assert "unit_amount=2900" in price and "recurring%5Binterval%5D=month" in price
    link = transport.calls_to(f"{stripe}/payment_links", "POST")[0]["body"].decode()
    assert "tier%5D=api" in link  # the subscription carries tier=api, so status webhooks find it
    asset = next(a for a in state.list_assets() if a["kind"] == API_KIND)
    assert asset["product_ref"] == "plink_api" and asset["price_cents"] == 2900
    assert not SubscriptionEngine().run("publish_api_tier", TaskContext(kit2, kit.hyp, {})).metrics["published"]  # once
