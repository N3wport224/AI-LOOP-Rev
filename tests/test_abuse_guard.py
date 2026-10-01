"""Phases 345-349: honeypot, no links, one budget, blocks, counts."""

import asyncio

from aiohttp.test_utils import TestClient, TestServer

from agent.power import NullBackend, PowerManager
from strategies import buyer_experience as bx
from strategies import opt_out as oo
from strategies.customer_requests import TOPICS
from tests import test_business_ops
from tools import abuse_guard as ag
from tools.storefront.webhook_listener import WebhookProcessor, build_app

kit = test_business_ops.kit  # the same live-mode toolkit fixture


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


def test_guard_rules(state, monkeypatch):
    clock = Clock()
    g = ag.Guard(state, clock)
    assert g.check("1.1.1.1", {"request": "rust jobs in berlin"}, ("request",)) is None
    assert g.check("1.1.1.1", {"request": "see https://spam.example"}, ("request",)) == "links"
    assert g.check("1.1.1.1", {"request": "write to me@spam.example"}, ("request",)) == "links"
    assert g.check("1.1.1.1", {"request": "buy cheap pills.xyz"}, ("request",)) == "links"
    assert g.check("1.1.1.1", {"request": "ok", "website": "http://x"}, ("request",)) == "honeypot"
    monkeypatch.setattr(ag, "GLOBAL_PER_HOUR", 7)
    g2 = ag.Guard(state, clock)
    for _ in range(7):
        assert g2.check("2.2.2.2", {}, ()) is None
    assert g2.check("2.2.2.2", {}, ()) == "busy"
    clock.t += 3601
    assert g2.check("2.2.2.2", {}, ()) is None  # a new hour


def test_persistent_offenders_are_blocked(state):
    clock = Clock()
    g = ag.Guard(state, clock)
    for _ in range(ag.BLOCK_AFTER):
        g.check("3.3.3.3", {"website": "x"}, ())
    assert g.check("3.3.3.3", {"request": "fine"}, ("request",)) == "blocked"
    assert g.check("4.4.4.4", {"request": "fine"}, ("request",)) is None  # others unaffected
    clock.t += ag.BLOCK_HOURS * 3600 + 1
    assert g.check("3.3.3.3", {"request": "fine"}, ("request",)) is None
    assert ag.today(state)["honeypot"] == ag.BLOCK_AFTER and ag.today(state)["blocked"] == 1


def test_forms_carry_the_honeypot(config):
    config.public_webhook_url = "https://hooks.example.com/webhook"
    for page in (bx.request_page(config, lambda t, b, d: b), oo.remove_page(config, lambda t, b, d: b)):
        assert 'name="website"' in page and 'aria-hidden="true"' in page


def test_endpoints(kit, state):
    app = build_app(WebhookProcessor(kit, use_sdk=False), power=PowerManager(NullBackend()))

    async def go():
        async with TestClient(TestServer(app)) as client:
            r = await client.post("/v1/requests", data={"request": "rust jobs", "website": "bot"})
            assert r.status == 200 and "Request received" in await r.text()  # looks accepted...
            r = await client.post("/v1/requests", data={"request": "see http://spam.example"})
            assert r.status == 400 and "without links" in await r.text()
            r = await client.post("/v1/optout", data={"company": "Acme", "email": "a@acme.example", "website": "x"})
            assert r.status == 202

    asyncio.run(go())
    assert not (state.get(TOPICS) or {}) and oo.pending(state) == []  # ...but nothing was kept
    assert ag.today(state) == {"honeypot": 2, "links": 1}


def test_doctor_shows_todays_counts(config, state):
    from cli.doctor import Doctor
    from tests.test_autopilot import Ctl, runner

    ag.Guard(state).check("5.5.5.5", {"website": "x"}, ())
    findings = {f.name: f for f in Doctor(config, state, Ctl(pid=1), run=runner({}), system="Linux").checks(deep=False)}
    assert findings["Public forms"].detail == "turned away today: 1 honeypot"
