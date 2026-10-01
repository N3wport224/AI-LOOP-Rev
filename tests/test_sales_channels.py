"""Phases 165-169: marketplace kits, affiliates, public catalog, GitHub teasers, product leaderboard."""

import asyncio
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agent.power import NullBackend, PowerManager
from strategies import product_factory as pf
from strategies import sales_channels as sc
from strategies.base import TaskContext
from tests import test_business_ops
from tests.test_product_factory import postings, unique_links
from tools.http_client import Response
from tools.storefront.webhook_listener import WebhookProcessor, build_app

kit = test_business_ops.kit  # the same live-mode toolkit fixture


@pytest.fixture
def two_products(kit, state, clock, transport):
    unique_links(transport)
    postings(state, clock, 30, ["rust"])
    postings(state, clock, 30, ["kotlin"], prefix="k")
    return [pf.tick(kit, force=True)["made"] for _ in range(2)]


# ------------------------------------------------------------------ Phase 165
def test_marketplace_kits(kit, state, config, two_products):
    config.pages_base_url = "https://me.github.io/site"
    path, n = sc.marketplace_export(state, config, kit.files)
    assert n == 2 and kit.files.exists("exports/marketplace/index.csv")
    slug = two_products[0]["slug"]
    text = kit.files.read_text(f"exports/marketplace/{slug}/listing.txt")
    assert text.startswith(f"Title: {two_products[0]['title']}\nPrice: $") and f"https://me.github.io/site/{slug}/" in text
    assert kit.files.exists(f"exports/marketplace/{slug}/{slug}-v1.zip")


# ------------------------------------------------------------------ Phase 166
def test_affiliates_earn_on_kept_sales_and_payouts_are_recorded(kit, state, config, two_products):
    config.pages_base_url = "https://me.github.io/site"
    aff = sc.add_affiliate(state, "Partner@Blog.example")
    assert sc.add_affiliate(state, "partner@blog.example")["code"] == aff["code"]  # once per person
    with pytest.raises(ValueError):
        sc.add_affiliate(state, "not-an-email")
    email = sc.welcome_email(state, config, aff)
    link = next(line for line in email.body.splitlines() if line.startswith("https://"))
    assert parse_qs(urlsplit(link).query) == {"utm_source": ["aff"], "utm_medium": ["affiliate"], "utm_campaign": [aff["code"]]}
    aid = two_products[0]["asset_id"]
    state.record_order("stripe", "cs_a1", "x@co.example", 1000, None, aid, None, status="delivered", channel="aff", campaign=aff["code"])
    state.record_order("stripe", "cs_a2", "y@co.example", 1000, None, aid, None, status="refunded", channel="aff", campaign=aff["code"])
    row = sc.commissions(state, config)[0]
    assert (row["orders"], row["earned_cents"], row["owed_cents"]) == (1, 300, 300)
    assert sc.mark_paid(state, config, aff["code"]) == 300 and sc.commissions(state, config)[0]["owed_cents"] == 0
    page = sc.affiliates_page(config, lambda t, b, d: b)
    assert "30%" in page and "mailto:" in page


# ------------------------------------------------------------------ Phase 167
def test_public_catalog_endpoint(kit, state, config, two_products):
    app = build_app(WebhookProcessor(kit, use_sdk=False), power=PowerManager(NullBackend()))

    async def go():
        async with TestClient(TestServer(app)) as client:
            r = await client.get("/v1/catalog")
            return r.status, await r.json(), r.headers

    status, data, headers = asyncio.run(go())
    assert status == 200 and {p["title"] for p in data["products"]} == {m["title"] for m in two_products}
    assert headers["Access-Control-Allow-Origin"] == "*" and data["products"][0]["checkout_url"].startswith("https://")


# ------------------------------------------------------------------ Phase 168
def test_teasers_go_to_github_without_contact_details(kit, state, config, transport, two_products):
    config.github_samples_repo, config.github_token = "me/free-hiring-data", "ghp_x"
    kit.github.token = "ghp_x"
    puts = []

    def contents(method, url, headers):
        if method == "GET":
            return Response(404, url, b"{}")
        puts.append(url)
        return Response(201, url, json.dumps({"content": {"sha": "s"}}).encode())

    transport.add("https://api.github.com/repos/me/free-hiring-data/contents/", contents)
    res = sc.SalesChannels().run("publish_teasers", TaskContext(kit, {"id": 1, "params": {}}, {}))
    assert res.metrics["published"] == 2 and any(u.endswith("/README.md") for u in puts)
    body = json.loads([c for c in transport.calls if c["method"] == "PUT" and "data/" in c["url"]][0]["body"])
    import base64

    teaser = base64.b64decode(body["content"]).decode()
    assert teaser.splitlines()[0] == ",".join(sc.TEASER_FIELDS) and len(teaser.splitlines()) == 11 and "contact_email" not in teaser
    assert sc.SalesChannels().run("publish_teasers", TaskContext(kit, {"id": 1, "params": {}}, {})).metrics["published"] == 0


# ------------------------------------------------------------------ Phase 169
def test_leaderboard_ranks_by_revenue_and_flags_never_sold(kit, state, two_products):
    state.record_order("stripe", "cs_1", "a@co.example", 900, None, two_products[1]["asset_id"], None, status="delivered")
    board = sc.leaderboard(state)
    assert board[0]["niche"] == two_products[1]["slug"] and board[0]["orders"] == 1
    assert board[1]["never_sold"] and not board[0]["never_sold"]
