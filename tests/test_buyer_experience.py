"""Phases 205-209: library, free sample CSV, real ratings, request a dataset, file list."""

import asyncio
import json
import re

from aiohttp.test_utils import TestClient, TestServer

from agent.power import NullBackend, PowerManager
from strategies import buyer_experience as bx
from strategies import product_factory as pf
from strategies.inbound_syndicator import site_pages
from strategies.ratings import ensure as ensure_ratings
from tests import test_business_ops
from tests.test_distribution import FakeSMTP
from tests.test_product_factory import postings, unique_links
from tools.page_builder import render_product_page
from tools.storefront.webhook_listener import WebhookProcessor, build_app

kit = test_business_ops.kit  # the same live-mode toolkit fixture


def serve(kit, scenario):
    app = build_app(WebhookProcessor(kit, use_sdk=False), power=PowerManager(NullBackend()))

    async def go():
        async with TestClient(TestServer(app)) as client:
            await scenario(client)

    asyncio.run(go())


def product(kit, state, clock, transport):
    unique_links(transport)
    postings(state, clock, 30, ["rust"])
    return pf.tick(kit, force=True)["made"]


# ------------------------------------------------------------------ Phase 205
def test_library_page_and_html_reply_for_the_form(kit, state, config):
    config.public_webhook_url = "https://hooks.example.com/webhook"
    page = bx.library_page(config, lambda t, b, d: b)
    assert 'action="https://hooks.example.com/v1/orders/recover"' in page and 'name="email"' in page

    async def scenario(client):
        r = await client.post("/v1/orders/recover", data={"email": "a@co.example"}, headers={"Accept": "text/html"})
        assert r.status == 202 and "Check your inbox" in await r.text() and r.headers["Content-Type"].startswith("text/html")
        r = await client.post("/v1/orders/recover", json={"email": "a@co.example"})
        assert (await r.json())["status"] == "accepted"  # scripts still get JSON

    serve(kit, scenario)


# ------------------------------------------------------------------ Phases 206 + 209
def test_sample_csv_and_file_list(kit, state, clock, transport):
    made = product(kit, state, clock, transport)
    page = next(p for p in site_pages(kit) if p.slug == made["slug"])
    csv_text = bx.sample_csv(page.sample_columns, page.sample_rows)
    assert csv_text.splitlines()[0] == "company,title,location,remote,stack" and len(csv_text.splitlines()) == 6
    html = render_product_page(page)
    assert 'href="sample.csv" download' in html and "In the download:</b> leads.csv" in html


# ------------------------------------------------------------------ Phase 207
def test_ratings_show_from_three_verified_buyers(kit, state, clock, transport):
    made = product(kit, state, clock, transport)
    ensure_ratings(state)
    for i, score in enumerate((3, 3)):
        state.record_order("stripe", f"cs_{i}", f"b{i}@co.example", 500, None, made["asset_id"], None, status="delivered")
        oid = state.get_order("stripe", f"cs_{i}")["id"]
        state._exec("INSERT INTO ratings (token_hash, order_pk, email, score, created_at, rated_at) VALUES (?,?,?,?,?,?)",
                    (f"h{i}", oid, "x", score, state.now(), state.now()))
    page = next(p for p in site_pages(kit) if p.slug == made["slug"])
    assert page.ratings_html == "" and page.aggregate_rating is None  # 2 ratings: nothing shown
    state.record_order("stripe", "cs_9", "c@co.example", 500, None, made["asset_id"], None, status="delivered")
    state._exec("INSERT INTO ratings (token_hash, order_pk, email, score, created_at, rated_at) VALUES (?,?,?,?,?,?)",
                ("h9", state.get_order("stripe", "cs_9")["id"], "x", 2, state.now(), state.now()))
    page = next(p for p in site_pages(kit) if p.slug == made["slug"])
    assert "Rated by 3 verified buyers: 😀 2 · 😐 1 · ☹️ 0" in page.ratings_html
    html = render_product_page(page)
    data = json.loads(re.search(r'<script type="application/ld\+json">\s*(\{.*?"@type": "Product".*?\})\s*</script>', html, re.S).group(1))
    assert data["aggregateRating"] == {"@type": "AggregateRating", "ratingValue": "2.67", "bestRating": "3", "worstRating": "1",
                                       "ratingCount": 3}


# ------------------------------------------------------------------ Phase 208
def test_requests_feed_demand_and_notify_once(kit, state, config, clock, transport):
    from strategies.customer_requests import TOPICS

    config.public_webhook_url = "https://hooks.example.com/webhook"
    assert 'action="https://hooks.example.com/v1/requests"' in bx.request_page(config, lambda t, b, d: b)

    async def scenario(client):
        r = await client.post("/v1/requests", data={"request": "Senior Rust jobs in Europe", "email": "Want@Co.example",
                                                     "notify": "1"})
        assert r.status == 200 and "once when it&#x27;s on sale" in await r.text()
        bad = await client.post("/v1/requests", data={"request": "x"})
        assert bad.status == 400
        for junk in (b"[1, 2]", b"{nope"):
            r = await client.post("/v1/requests", data=junk, headers={"Content-Type": "application/json"})
            assert r.status == 400
        for _ in range(bx.REQUESTS_PER_IP_HOUR):
            await client.post("/v1/requests", data={"request": "kotlin please"})
        assert (await client.post("/v1/requests", data={"request": "kotlin please"})).status == 429

    serve(kit, scenario)
    assert (state.get(TOPICS) or {}).get("rust") == 1
    assert state.get(bx.WATCH)[0]["email"] == "want@co.example"
    assert bx.notify_watchers(kit) == 0  # nothing on sale yet
    clock.advance(minutes=5)
    product(kit, state, clock, transport)
    before = len(FakeSMTP.sent)
    assert bx.notify_watchers(kit) == 1 and FakeSMTP.sent[-1]["To"] == "want@co.example"
    assert len(FakeSMTP.sent) == before + 1 and state.get(bx.WATCH) == []  # once, then forgotten
