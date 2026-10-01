"""Phases 350-354: site searches as demand."""

import asyncio
import json

from aiohttp.test_utils import TestClient, TestServer

from agent.power import NullBackend, PowerManager
from strategies import search_demand as sd
from strategies import site_discovery
from strategies.customer_requests import TOPICS
from tests import test_business_ops
from tools.site_extras import legal_pages
from tools.storefront.webhook_listener import WebhookProcessor, build_app

kit = test_business_ops.kit  # the same live-mode toolkit fixture


def test_normalize_and_record(state, clock):
    assert sd.normalize("  Elixir   <script>Berlin!! ") == "elixir script berlin"
    assert sd.normalize("C# / .NET") == "c# .net" and len(sd.normalize("x" * 500)) == sd.MAX_LEN
    assert sd.record(state, "ab", True) is None  # too short
    sd.record(state, "Elixir jobs", True)
    assert not (state.get(TOPICS) or {})  # once isn't a pattern
    sd.record(state, "elixir jobs", True)
    assert (state.get(TOPICS) or {}).get("elixir") == 1
    sd.record(state, "elixir jobs", True)
    assert state.get(TOPICS)["elixir"] == 1  # counted once
    sd.record(state, "rust", False)
    assert sd.weekly_line(state) == "Searched for on the site but not on sale: elixir jobs (3)."
    clock.advance(days=sd.KEEP_DAYS + 1)
    sd.record(state, "kotlin", True)
    assert list(state.get(sd.KEY)) == ["kotlin"]  # old searches go


def test_endpoint(kit, state):
    app = build_app(WebhookProcessor(kit, use_sdk=False), power=PowerManager(NullBackend()))

    async def go():
        async with TestClient(TestServer(app)) as client:
            r = await client.post("/v1/search-log", data=json.dumps({"q": "elixir", "zero": True}),
                                  headers={"Content-Type": "text/plain;charset=UTF-8"})
            assert r.status == 204 and r.headers["X-Content-Type-Options"] == "nosniff"
            assert (await client.post("/v1/search-log", data=b"[1]")).status == 400
            assert (await client.post("/v1/search-log", data=b"x" * 600)).status == 413
            r = await client.post("/v1/search-log", data=json.dumps({"q": "spam", "website": "bot"}))
            assert r.status == 204

    asyncio.run(go())
    assert list(state.get(sd.KEY)) == ["elixir"]


def test_search_page_sends_only_with_an_address_and_respects_dnt(config):
    with_log = site_discovery.search_page(lambda t, b, d: b, "https://hooks.example.com/v1/search-log")
    assert 'data-log="https://hooks.example.com/v1/search-log"' in with_log
    assert 'data-log="' not in site_discovery.search_page(lambda t, b, d: b)
    js = site_discovery.SEARCH_JS
    assert "doNotTrack" in js and "sendBeacon" in js and "innerHTML" not in js and "cookie" not in js


def test_privacy_page_says_so(config):
    assert "Site search:" in legal_pages(config, lambda t, b, d: b)["legal/privacy/index.html"]
