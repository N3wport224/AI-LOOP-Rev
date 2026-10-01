"""Phases 285-289: the form, you decide, left out everywhere, confirmation, privacy page."""

import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agent.power import NullBackend, PowerManager
from strategies import opt_out as oo
from strategies import product_factory as pf
from tests import test_business_ops
from tests.test_distribution import FakeSMTP
from tests.test_product_factory import postings
from tests.test_ops_robustness import alerts
from tools.site_extras import legal_pages
from tools.storefront.webhook_listener import WebhookProcessor, build_app

kit = test_business_ops.kit  # the same live-mode toolkit fixture


def serve(kit, scenario):
    app = build_app(WebhookProcessor(kit, use_sdk=False), power=PowerManager(NullBackend()))

    async def go():
        async with TestClient(TestServer(app)) as client:
            await scenario(client)

    asyncio.run(go())


def test_the_form_and_the_endpoint(kit, state, config, monkeypatch):
    monkeypatch.setattr(oo, "REQUESTS_PER_IP_HOUR", 5)
    config.public_webhook_url = "https://hooks.example.com/webhook"
    assert 'action="https://hooks.example.com/v1/optout"' in oo.remove_page(config, lambda t, b, d: b)

    async def scenario(client):
        r = await client.post("/v1/optout", data={"company": "Co 3 GmbH", "email": "Jo@co3.example", "note": "please"})
        assert r.status == 202 and "check the request and email you" in await r.text() and r.headers["X-Content-Type-Options"] == "nosniff"
        assert (await client.post("/v1/optout", data={"company": "x", "email": "a@b.example"})).status == 400
        assert (await client.post("/v1/optout", data={"company": "Acme", "email": "nope"})).status == 400
        assert (await client.post("/v1/optout", data=b"[1]", headers={"Content-Type": "application/json"})).status == 400
        assert (await client.post("/v1/optout", data={"company": "Acme", "email": "a@acme.example"})).status == 202
        assert (await client.post("/v1/optout", data={"company": "Beta", "email": "a@beta.example"})).status == 429

    serve(kit, scenario)
    req = oo.pending(state)[0]
    assert req["company"] == "Co 3 GmbH" and req["email"] == "jo@co3.example" and req["domain_matches"]
    assert any("asked to be left out" in a for a in alerts(state))


def test_domain_check():
    assert oo.domain_matches("Acme Inc", "jo@acme.com") and oo.domain_matches("Acme", "jo@acme-corp.io")
    assert not oo.domain_matches("Acme", "jo@gmail.com")


def test_approved_companies_leave_every_product(kit, state, config, clock):
    postings(state, clock, 30, ["rust"])
    entry = oo.record(state, "CO 3, Inc.", "jo@co.example")
    assert oo.record(state, "Co 3", "other@co.example")["id"] == entry["id"]  # one pending request per company
    assert any(r["company"] == "Co 3" for r in pf.fresh_leads(state, 90))
    oo.decide(kit, entry["id"], approve=True)
    assert not any(r["company"] == "Co 3" for r in pf.fresh_leads(state, 90))
    assert FakeSMTP.sent[-1]["To"] == "jo@co.example" and "left out of our datasets" in FakeSMTP.sent[-1]["Subject"]
    with pytest.raises(ValueError):
        oo.decide(kit, entry["id"], approve=True)  # already decided


def test_rejected_requests_change_nothing(kit, state, clock):
    postings(state, clock, 30, ["rust"])
    entry = oo.record(state, "Co 3", "jo@co.example")
    oo.decide(kit, entry["id"], approve=False)
    assert any(r["company"] == "Co 3" for r in pf.fresh_leads(state, 90)) and oo.excluded(state) == set()


def test_privacy_page_explains_it(config):
    page = legal_pages(config, lambda t, b, d: b)["legal/privacy/index.html"]
    assert 'href="../../remove/"' in page and "ask to be left out" in page


def test_cli_lists_and_decides(kit, state, capsys, monkeypatch):
    from cli import growth

    monkeypatch.setattr(growth, "_setup", lambda: (kit.config, state, None))
    oo.record(state, "Acme", "jo@gmail.com")
    growth.optout_main([])
    assert "domain does NOT match" in capsys.readouterr().out
    assert growth.optout_main(["reject", oo.pending(state)[0]["id"]]) == 0
