"""Phases 320-324: technology passes."""

import json

import pytest

from strategies import product_factory as pf
from strategies import tech_passes as tp
from strategies.inbound_syndicator import site_pages
from strategies.subscription_engine import activate_from_session
from tests import test_business_ops
from tests.test_distribution import FakeSMTP
from tests.test_product_factory import postings, unique_links
from tools.page_builder import render_product_page

kit = test_business_ops.kit  # the same live-mode toolkit fixture


@pytest.fixture
def four_rust_products(kit, state, config, clock, transport):
    config.public_webhook_url = "https://hooks.example.com/webhook"  # download links need the public address
    config.factory_types = ["slice"]
    unique_links(transport)
    postings(state, clock, 30, ["rust"])
    postings(state, clock, 25, ["rust"], location="Austin, TX, United States", prefix="us")
    postings(state, clock, 25, ["rust"], remote=True, location="Remote", prefix="rm")
    postings(state, clock, 22, ["rust"], seniority="senior", prefix="sr")
    made = [pf.tick(kit, force=True)["made"] for _ in range(4)]
    assert all(made)
    pf.tick(kit)  # the next factory run puts the pass on sale
    return made


def subscribe(kit, state, email="sub@co.example", sub_id="sub_1"):
    asset = tp.pass_asset(state, "rust")
    sid, _ = activate_from_session(kit, {"subscription": sub_id, "payment_link": asset["product_ref"], "payment_status": "paid",
                                         "customer_details": {"email": email}, "amount_total": tp.PASS_PRICE})
    return sid


def test_a_pass_goes_on_sale_with_four_products(kit, state, four_rust_products, transport):
    asset = tp.pass_asset(state, "rust")
    assert asset and asset["title"] == "Every Rust Dataset, Updated Weekly" and asset["price_cents"] == tp.PASS_PRICE
    assert json.loads(asset["kind_meta"]) ["tech"] == "rust" and asset["niche"] == "pass-rust"
    created = [c for c in transport.calls_to(f"{test_business_ops.STRIPE}/prices", "POST")]
    assert any(b"recurring" in (c["body"] or b"") for c in created)
    assert tp.publish(kit) == []  # only one per technology


def test_welcome_then_weekly_links(kit, state, config, clock, four_rust_products):
    sid = subscribe(kit, state)
    out = tp.deliver(kit)
    assert out["welcome"] == 1
    welcome = FakeSMTP.sent[-1]
    assert welcome["To"] == "sub@co.example" and welcome["Subject"] == "Welcome: every Rust dataset"
    assert welcome.get_body().get_content().count("https://hooks.example.com/d/") == 4
    assert tp.deliver(kit) == {"welcome": 0, "weekly": 0}  # nothing twice
    clock.advance(days=8)
    assert tp.deliver(kit)["weekly"] == 1 and FakeSMTP.sent[-1]["Subject"].startswith("This week's Rust datasets")
    assert tp.deliver(kit)["weekly"] == 0
    state._exec("UPDATE subscribers SET subscription_status = 'canceled' WHERE id = ?", (sid,))
    clock.advance(days=7)
    assert tp.deliver(kit) == {"welcome": 0, "weekly": 0}  # cancelled: nothing


def test_the_standard_subscription_skips_passes(kit, state, clock, four_rust_products):
    from strategies.base import TaskContext
    from strategies.subscription_engine import SubscriptionEngine, send_welcome

    sid = subscribe(kit, state)
    assert send_welcome(kit, sid) == "skipped"
    clock.advance(days=8)
    hyp = {"id": 1, "params": {"niche": "x"}, "iterations": 1}
    res = SubscriptionEngine().deliver_subscriptions(TaskContext(kit, hyp, {}))
    assert res.metrics.get("sent", 0) == 0 and res.metrics.get("failed", 0) == 0


def test_product_pages_offer_the_pass_and_the_report_counts_it(kit, state, four_rust_products):
    page = next(p for p in site_pages(kit) if p.slug == four_rust_products[0]["slug"])
    html = render_product_page(page)
    assert "Every dataset on this technology, updated weekly: $19.00/month" in html
    assert tp.weekly_line(state) == ""
    subscribe(kit, state)
    assert tp.weekly_line(state) == "Technology passes: 1 subscriber(s), $19.00 a month."


def test_no_pass_without_enough_products_or_public_links(kit, state, config, clock, transport):
    config.factory_types = ["slice"]
    unique_links(transport)
    postings(state, clock, 30, ["rust"])
    pf.tick(kit, force=True)
    assert tp.pass_asset(state, "rust") is None
