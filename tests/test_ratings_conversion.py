"""Phases 100-104: trust row, 1-click ratings and their follow-through, most-popular badge, per-dataset feed."""

import asyncio
import re
import xml.etree.ElementTree as ET

from aiohttp.test_utils import TestClient, TestServer

from agent import tunnel
from agent.power import NullBackend, PowerManager
from strategies import ratings
from strategies.buyer_followup import BuyerFollowup
from strategies.inbound_syndicator import site_pages
from strategies.owner_todo import todo
from tests import test_business_ops
from tests.test_business_ops import ctx, dataset
from tests.test_distribution import FakeSMTP
from tests.test_growth_ops import sale
from tools.offer_pages import render_pricing
from tools.page_builder import ProductPage, SiteBuilder, render_index, render_product_page
from tools.site_extras import changelog_feed, popular_niche
from tools.storefront.webhook_listener import WebhookProcessor, build_app

kit = test_business_ops.kit  # the same live-mode toolkit fixture


def page(**kw):
    base = dict(niche="python-remote", title="Python Intel", summary="s", price_cents=1900, currency="usd",
                checkout_url="https://buy.stripe.com/x", sample_columns=["company"], sample_rows=[{"company": "A"}])
    return ProductPage(**(base | kw))


def body_of(msg):
    return msg.get_body(("plain",)).get_content() if msg.is_multipart() else msg.get_content()


def serve(kit, scenario):
    app = build_app(WebhookProcessor(kit, use_sdk=False), power=PowerManager(NullBackend()))

    async def go():
        async with TestClient(TestServer(app)) as client:
            await scenario(client)

    asyncio.run(go())


# ------------------------------------------------------------------ Phase 100: trust row
def test_trust_row_under_the_buy_button():
    html = render_product_page(page(refund_days=14))
    assert "Secure checkout by Stripe" in html and '<a href="../legal/refunds/">14-day refund</a>' in html
    assert "day refund" not in render_product_page(page(refund_days=0))
    assert "Secure checkout" not in render_product_page(page(checkout_url=""))


# ------------------------------------------------------------------ Phases 101-102: ratings
def test_followup_carries_rating_links_and_a_click_is_recorded_once(kit, state, clock, config):
    config.public_webhook_url = "https://hooks.example.com/webhook"
    _, aid = dataset(kit, state, "python-remote")
    sale(state, aid, "cs_due", "due@co.example", delivered_days_ago=4, clock=clock)
    assert BuyerFollowup().run("follow_up_buyers", ctx(kit)).metrics["sent"] == 1
    links = re.findall(r"https://hooks\.example\.com/r/([\w-]+)/(\d)", body_of(FakeSMTP.sent[-1]))
    assert sorted(int(s) for _, s in links) == [1, 2, 3] and len({t for t, _ in links}) == 1
    token = links[0][0]
    assert ratings.lookup(state, token)["email"] == "due@co.example"
    assert token not in str(state._all("SELECT * FROM ratings"))  # only the hash is stored

    async def scenario(client):
        r = await client.get(f"/r/{token}/1")  # a link scanner's GET records nothing
        assert r.status == 200 and 'method="post"' in await r.text()
        assert ratings.lookup(state, token)["score"] is None
        r = await client.post(f"/r/{token}/1")
        assert r.status == 200 and "Sorry about that" in await r.text() and r.headers["X-Robots-Tag"] == "noindex"
        r = await client.post(f"/r/{token}/3")
        assert "already in" in await r.text()
        assert ratings.lookup(state, token)["score"] == 1  # the first answer stands
        assert (await client.post("/r/nope/3")).status == 404
        assert (await client.get(f"/r/{token}/9")).status == 404

    serve(kit, scenario)
    assert ratings.record(state, token, 3) == "already" and ratings.record(state, token + "x", 3) == "invalid"
    assert any(e["kind"] == "alert" and "not good" in e["message"] for e in state.recent_errors(10))
    assert any(i["title"] == "Reach out to 1 unhappy buyer(s)" for i in todo(state, config))
    assert ratings.summary(state) == "Ratings in the last 30 days: 😀 0 · 😐 0 · ☹️ 1"
    clock.advance(days=15)
    assert not ratings.unhappy(state)


def test_no_rating_links_without_a_public_url(kit, state, config):
    config.public_webhook_url = ""
    assert ratings.links(state, config, {"id": 1, "email": "a@co.example"}) == {}


def test_rating_paths_are_routed_through_the_tunnel(tmp_path):
    assert "/r/*" in tunnel.EXPOSED_PATHS and "/d/*" in tunnel.EXPOSED_PATHS
    old = tmp_path / "old.yml"
    uuid = "12345678-1234-1234-1234-123456789abc"
    old.write_text(tunnel.render_config(uuid, "/c.json", "hooks.example.com", paths=("/webhook", "/healthz")))
    assert "/r/*" in tunnel.missing_paths(old) and "/webhook" not in tunnel.missing_paths(old)
    new = tmp_path / "new.yml"
    new.write_text(tunnel.render_config(uuid, "/c.json", "hooks.example.com"))
    assert tunnel.missing_paths(new) == [] and tunnel.missing_paths(tmp_path / "none.yml") == []


# ------------------------------------------------------------------ Phase 103: most popular
def test_most_popular_needs_two_kept_orders(kit, state, clock):
    _, py = dataset(kit, state, "python-remote")
    _, ml = dataset(kit, state, "ml-ai")
    state.update_asset(py, niche="python-remote")
    state.update_asset(ml, niche="ml-ai")
    sale(state, py, "cs_1", "a@co.example")
    assert popular_niche(state) == ""
    sale(state, ml, "cs_2", "b@co.example")
    sale(state, ml, "cs_3", "c@co.example")
    sale(state, py, "cs_4", "d@co.example", status="refunded")
    sale(state, py, "cs_5", "e@co.example", status="disputed")
    assert popular_niche(state) == "ml-ai"
    by = {p.niche: p for p in site_pages(kit) if p.kind == "dataset"}
    assert by["ml-ai"].popular and not by["python-remote"].popular and by["ml-ai"].refund_days > 0
    pages = list(by.values())
    assert render_index(pages, "", "Site").count("Most popular") == 1
    assert render_pricing(pages, "Site").count("Most popular") == 1


# ------------------------------------------------------------------ Phase 104: per-dataset feed
def test_each_dataset_has_its_own_update_feed(kit, config, clock):
    p = page(versions=[{"version": 2, "date": "2026-09-29", "rows": 120}, {"version": 1, "date": "2026-09-01", "rows": 80}])
    xml = changelog_feed(p, "https://me.github.io/site", "Site")
    items = ET.fromstring(xml).findall("./channel/item")
    assert [i.findtext("guid") for i in items] == ["python-remote-v2", "python-remote-v1"]
    assert items[0].findtext("link") == "https://me.github.io/site/python-remote/" and "120 rows" in items[0].findtext("title")
    assert 'href="changelog/feed.xml"' in render_product_page(p)
    assert "changelog/feed.xml" not in render_product_page(page())
    out = SiteBuilder(config, kit.files).build([p], [], clock())
    assert "python-remote/changelog/feed.xml" in out
