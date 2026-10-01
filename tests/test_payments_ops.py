"""Phases 50-54: large-file links, webhook health, card-testing guard, duplicate purchases, Stripe Tax."""

import asyncio
import io
import zipfile
from datetime import timedelta
from urllib.parse import parse_qs

import pytest

from strategies.owner_todo import todo
from strategies.payment_guard import PaymentGuard, duplicates
from strategies.webhook_health import WebhookHealth
from tests import test_business_ops
from tests.test_business_ops import STRIPE, ctx, dataset
from tests.test_distribution import FakeSMTP
from tools import download_links
from tools.http_client import Response

kit = test_business_ops.kit  # the same live-mode toolkit fixture


def big_zip(kit, rel, size=2048):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("leads.csv", "x" * size)
    kit.files.write_bytes(rel, buf.getvalue())
    return kit.files.resolve(rel)


# ------------------------------------------------------------------ Phase 50: download links
def test_links_expire_and_run_out(kit, state, config, clock):
    config.public_webhook_url = "https://hooks.example.com/webhook"
    big_zip(kit, "assets/py/py.zip")
    url = download_links.issue(state, config, "assets/py/py.zip", "a@co.example")
    token = url.rsplit("/", 1)[1]
    assert url.startswith("https://hooks.example.com/d/") and len(token) >= 32
    assert state._one("SELECT COUNT(*) AS n FROM download_tokens WHERE token_hash = ?", (token,))["n"] == 0  # hash only
    for _ in range(config.download_link_uses):
        assert download_links.redeem(state, kit.files, token)[0].name == "py.zip"
    assert "used up" in download_links.redeem(state, kit.files, token)[1]
    fresh = download_links.issue(state, config, "assets/py/py.zip").rsplit("/", 1)[1]
    clock.advance(days=8)
    assert "expired" in download_links.redeem(state, kit.files, fresh)[1]
    assert download_links.redeem(state, kit.files, "nope")[1] == "invalid link"
    state._exec("INSERT INTO download_tokens (token_hash, path, created_at, expires_at, max_downloads) VALUES (?,?,?,?,?)",
                (download_links._h("evil"), "../../etc/passwd", state.now(), "2999-01-01T00:00:00+00:00", 5))
    assert download_links.redeem(state, kit.files, "evil") == (None, "file unavailable")


def test_too_big_to_attach_goes_as_a_link(kit, state, config, monkeypatch):
    from strategies.distribution_engine import fulfil_order
    from tools import dispatcher

    config.public_webhook_url = "https://hooks.example.com/webhook"
    monkeypatch.setattr(dispatcher, "MAX_ATTACHMENT_BYTES", 1000)
    _, aid = dataset(kit, state, "python-remote")
    big_zip(kit, state.get_asset(aid)["path"])
    state.record_order("stripe", "cs_big", "a@co.example", 1900, "plink_python-remote", aid, None)
    assert fulfil_order(kit, state.get_order("stripe", "cs_big")) == "delivered"
    msg = FakeSMTP.sent[-1]
    body = msg.get_body(("plain",)).get_content() if msg.is_multipart() else msg.get_content()
    assert "https://hooks.example.com/d/" in body and not list(msg.iter_attachments())


def test_the_public_server_serves_a_valid_link(kit, state, config):
    from aiohttp.test_utils import TestClient, TestServer

    from agent.power import NullBackend, PowerManager
    from tools.storefront.webhook_listener import WebhookProcessor, build_app

    config.public_webhook_url = "https://hooks.example.com/webhook"
    big_zip(kit, "assets/py/py.zip")
    token = download_links.issue(state, config, "assets/py/py.zip").rsplit("/", 1)[1]
    app = build_app(WebhookProcessor(kit, use_sdk=False), power=PowerManager(NullBackend()))

    async def go():
        async with TestClient(TestServer(app)) as client:
            r = await client.get(f"/d/{token}")
            assert r.status == 200 and 'filename="py.zip"' in r.headers["Content-Disposition"]
            assert zipfile.ZipFile(io.BytesIO(await r.read())).namelist() == ["leads.csv"]
            assert (await client.get("/d/bogus")).status == 404

    asyncio.run(go())


# ------------------------------------------------------------------ Phase 51: webhook health
def webhook_world(transport, endpoints, failed=0):
    transport.add("https://hooks.example.com/healthz", Response(200, "x", b'{"ok": true}', {}))
    transport.add_json(f"{STRIPE}/webhook_endpoints", {"data": endpoints})
    transport.add_json(f"{STRIPE}/events", {"data": [{"id": f"evt_{i}"} for i in range(failed)]})


def test_a_healthy_webhook_is_left_alone(kit, state, config, transport):
    from agent.setup_autonomous import webhook_events

    config.public_webhook_url = "https://hooks.example.com/webhook"
    webhook_world(transport, [{"id": "we_1", "url": config.public_webhook_url, "status": "enabled",
                               "enabled_events": webhook_events()}])
    res = WebhookHealth(env_file=config.data_dir / ".env").run("check_webhook", ctx(kit))
    assert res.metrics == {"problems": 0, "repaired": False}
    assert "webhook checked" in WebhookHealth().run("check_webhook", ctx(kit)).summary


def test_a_missing_endpoint_is_recreated_and_the_agent_reloads(kit, state, config, transport, tmp_path):
    config.public_webhook_url, config.stripe_webhook_secret = "https://hooks.example.com/webhook", ""
    webhook_world(transport, [])
    transport.add(f"{STRIPE}/webhook_endpoints", lambda method, url, headers: Response(
        200, url, b'{"id": "we_new", "secret": "whsec_new"}' if method == "POST" else b'{"data": []}', {}))
    reloads = []
    env = tmp_path / ".env"
    res = WebhookHealth(env_file=env, reload=reloads.append).run("check_webhook", ctx(kit))
    assert res.metrics["repaired"] and reloads and "STRIPE_WEBHOOK_SECRET=whsec_new" in env.read_text()


def test_tunnel_down_and_failed_deliveries_alert_once_a_day(kit, state, config, transport, clock):
    from agent.setup_autonomous import webhook_events

    config.public_webhook_url = "https://hooks.example.com/webhook"
    transport.add_json(f"{STRIPE}/webhook_endpoints", {"data": [{"id": "we_1", "url": config.public_webhook_url,
                                                                 "enabled_events": webhook_events()}]})
    transport.add_json(f"{STRIPE}/events", {"data": [{"id": "evt_1"}, {"id": "evt_2"}]})
    res = WebhookHealth().run("check_webhook", ctx(kit))
    assert res.metrics["problems"] == 2
    alert = state.recent_errors(1, kind="alert")[0]["message"]
    assert "tunnel" in alert and "2 Stripe event(s)" in alert
    clock.advance(hours=7)
    WebhookHealth().run("check_webhook", ctx(kit))
    assert len(state.recent_errors(5, kind="alert")) == 1


# ------------------------------------------------------------------ Phases 52-54: payment guard
def test_card_testing_bursts_alert(kit, state, transport, clock):
    transport.add_json(f"{STRIPE}/charges", {"data": [{"status": "failed"}] * 12 + [{"status": "succeeded"}]})
    transport.add_json(f"{STRIPE}/tax/settings", {"status": "active"})
    PaymentGuard().run("guard_payments", ctx(kit))
    alert = state.recent_errors(1, kind="alert")[0]["message"]
    assert "12 failed card payments" in alert and "Radar" in alert
    clock.advance(minutes=20)
    PaymentGuard().run("guard_payments", ctx(kit))
    assert len(state.recent_errors(5, kind="alert")) == 1  # at most every 6 hours


def test_double_purchases_are_flagged_once(kit, state, clock):
    _, aid = dataset(kit, state, "python-remote")
    for i, days in enumerate((3, 1)):
        state.record_order("stripe", f"cs_{i}", "a@co.example", 1900, None, aid, None, status="delivered",
                           occurred_at=(clock() - timedelta(days=days)).isoformat(timespec="seconds"))
    state.record_order("stripe", "cs_x", "b@co.example", 1900, None, aid, None, status="delivered")
    assert [(d["first"], d["second"]) for d in duplicates(state)] == [("cs_0", "cs_1")]
    PaymentGuard().run("guard_payments", ctx(kit))
    PaymentGuard().run("guard_payments", ctx(kit))
    alerts = [e for e in state.recent_errors(5, kind="alert") if "twice" in e["message"]]
    assert len(alerts) == 1 and "refund $19.00" in alerts[0]["message"]
    assert any(i["title"].startswith("Check a double purchase") for i in todo(state, kit.config))


def test_stripe_tax_is_applied_to_links_when_active(kit, state, transport, config):
    dataset(kit, state, "python-remote")
    transport.add_json(f"{STRIPE}/charges", {"data": []})
    transport.add_json(f"{STRIPE}/tax/settings", {"status": "pending"})
    PaymentGuard().run("guard_payments", ctx(kit))
    assert config.stripe_automatic_tax is False and state.get("payment_guard")["tax_status"] == "pending"
    state.set("payment_guard", {})
    transport.add_json(f"{STRIPE}/tax/settings", {"status": "active"})
    transport.add_json(f"{STRIPE}/payment_links/plink_python-remote", {"id": "plink_python-remote"})
    PaymentGuard().run("guard_payments", ctx(kit))
    assert config.stripe_automatic_tax is True
    call = transport.calls_to(f"{STRIPE}/payment_links/plink_python-remote", "POST")[-1]
    assert parse_qs(call["body"].decode())["automatic_tax[enabled]"] == ["true"]
    from tools.storefront.stripe_pages_publisher import link_options

    assert link_options(config) == {"automatic_tax": {"enabled": True}}


@pytest.mark.parametrize("task", ["check_webhook", "guard_payments"])
def test_new_tasks_are_planned_and_handled(config, state, toolkit, task):
    from agent.engine import PLAN, Engine

    engine = Engine(config, state=state, sleep=lambda s: None)
    assert task in dict(PLAN) and task in engine.handlers
    assert engine.breaker.max_actions_per_cycle >= len(PLAN) + 10
