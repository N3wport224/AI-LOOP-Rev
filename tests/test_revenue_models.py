"""Phases 155-159: custom datasets, name your price, sponsorship, lifetime pass, gift cards."""

import io
import itertools
import json
import zipfile
from urllib.parse import parse_qs

import pytest

from cli import growth
from strategies import revenue_models as rm
from strategies.base import TaskContext
from strategies.distribution_engine import fulfil_order
from tests import test_business_ops
from tests.test_business_ops import STRIPE
from tests.test_distribution import FakeSMTP
from tests.test_product_factory import postings
from tools.http_client import Response

kit = test_business_ops.kit  # the same live-mode toolkit fixture


def ctx(kit):
    return TaskContext(kit, {"id": 1, "params": {}, "iterations": 0}, {})


@pytest.fixture
def offers_live(kit, transport):
    ids = itertools.count(1)
    transport.add(f"{STRIPE}/payment_links", lambda m, url, h: Response(
        200, url, json.dumps({"id": f"plink_o{next(ids)}", "url": f"https://buy.stripe.com/o{next(ids)}"}).encode(), {}))
    transport.add_json(f"{STRIPE}/coupons", {"id": "co_gift"})
    transport.add_json(f"{STRIPE}/promotion_codes", {"id": "promo_gift"})
    res = rm.RevenueModels().run("publish_offers", ctx(kit))
    assert res.metrics["created"] == 5
    return kit


def buy(state, kind, email="buyer@co.example", gross=None, meta=None):
    aid = rm.offers(state)[kind]
    asset = state.get_asset(aid)
    state.record_order("stripe", f"cs_{kind}_{email}", email, gross or asset["price_cents"], asset["product_ref"], aid, None,
                       meta=meta)
    return state.get_order("stripe", f"cs_{kind}_{email}")


def body(msg):
    return msg.get_body(("plain",)).get_content() if msg.is_multipart() else msg.get_content()


def test_offers_are_created_once_with_the_right_checkout_options(offers_live, state, transport):
    links = [parse_qs(c["body"].decode()) for c in transport.calls_to(f"{STRIPE}/payment_links", "POST")]
    assert any("custom_fields[0][key]" in link and link["custom_fields[0][key]"] == ["request"] for link in links)
    prices = [parse_qs(c["body"].decode()) for c in transport.calls_to(f"{STRIPE}/prices", "POST")]
    assert any(p.get("custom_unit_amount[enabled]") == ["true"] and p["custom_unit_amount[minimum]"] == ["300"] for p in prices)
    assert all(link.get("allow_promotion_codes") == ["true"] for link in links)
    assert rm.RevenueModels().run("publish_offers", ctx(offers_live)).metrics["created"] == 0
    html = rm.more_page(state, lambda t, b, d: b)
    assert "Lifetime Pass" in html and "Name your price" in html


# ------------------------------------------------------------------ Phase 155: custom dataset
def test_a_custom_request_is_built_and_delivered(offers_live, state, clock):
    postings(state, clock, 12, ["rust"], location="Berlin, Germany")
    assert rm.parse_request("Rust jobs in Germany please, senior if possible") == {"tech": "rust", "region": "europe",
                                                                                  "level": "senior"}
    assert rm.parse_request("any Go roles in the US?")["tech"] == "golang"
    order = buy(state, "custom_request", meta={"field:request": "Rust in Europe"})
    assert fulfil_order(offers_live, order) == "delivered"
    msg = FakeSMTP.sent[-1]
    assert "Companies Hiring Rust Engineers in Europe" in msg["Subject"]
    zip_part = next(p for p in msg.iter_attachments())
    names = zipfile.ZipFile(io.BytesIO(zip_part.get_content())).namelist()
    assert any(n.endswith("leads.csv") for n in names)


def test_a_custom_request_that_cannot_be_built_waits_for_the_owner(offers_live, state):
    order = buy(state, "custom_request", meta={"field:request": "COBOL mainframe in Antarctica"})
    assert fulfil_order(offers_live, order) == "manual"
    assert state.get_order("stripe", order["order_id"])["status"] == "needs_manual_delivery"
    assert any("COBOL mainframe" in e["message"] for e in state.recent_errors(5) if e["kind"] == "alert")


# ------------------------------------------------------------------ Phase 156: name your price
def test_supporters_get_the_catalog(offers_live, state):
    order = buy(state, "pay_what_you_want", gross=1200)
    assert fulfil_order(offers_live, order) == "delivered"
    msg = FakeSMTP.sent[-1]
    assert "$12.00" in body(msg)
    csv_text = next(msg.iter_attachments()).get_content()
    csv_text = csv_text.decode() if isinstance(csv_text, bytes) else csv_text
    assert csv_text.startswith("title,price_usd,checkout_url") and "Lifetime Pass" not in csv_text


# ------------------------------------------------------------------ Phase 157: sponsorship
def test_a_sponsor_shows_only_after_approval_and_for_a_week(offers_live, state, clock, config, monkeypatch, capsys):
    order = buy(state, "sponsorship")
    assert fulfil_order(offers_live, order) == "manual"
    assert rm.sponsor_html(state) == ""
    with pytest.raises(ValueError):
        rm.approve_sponsor(state, order["id"], "Great tool", "http://not-https.example")
    monkeypatch.setattr(growth, "_setup", lambda: (config, state, None))
    assert growth.sponsor_main(["approve", str(order["id"]), "Hire faster with Acme", "https://acme.example"]) == 0
    assert 'rel="sponsored noopener"' in rm.sponsor_html(state) and "Hire faster with Acme" in rm.sponsor_html(state)
    assert state.get_order("stripe", order["order_id"])["status"] == "delivered"
    clock.advance(days=8)
    assert rm.sponsor_html(state) == "" and rm.expire_sponsors(state) == 1


# ------------------------------------------------------------------ Phase 158: lifetime pass
def test_lifetime_pass_holders_get_everything_then_weekly_news(offers_live, state, clock, transport, config):
    from tests.test_product_factory import unique_links
    from strategies import product_factory as pf

    config.public_webhook_url = "https://hooks.example.com/webhook"
    unique_links(transport)
    postings(state, clock, 30, ["rust"])
    pf.tick(offers_live, force=True)
    order = buy(state, "lifetime")
    assert fulfil_order(offers_live, order) == "delivered"
    first = body(FakeSMTP.sent[-1])
    assert "Companies Hiring Rust Engineers: https://hooks.example.com/d/" in first
    clock.advance(days=8)
    postings(state, clock, 30, ["kotlin"], prefix="k")
    pf.tick(offers_live, force=True)
    before = len(FakeSMTP.sent)
    assert rm.weekly_pass_digest(offers_live) == 1
    news = body(FakeSMTP.sent[-1])
    assert len(FakeSMTP.sent) == before + 1 and "Kotlin" in news and "Rust" not in news
    assert rm.weekly_pass_digest(offers_live) == 0


# ------------------------------------------------------------------ Phase 159: gift card
def test_a_gift_card_is_a_one_time_code_worth_what_was_paid(offers_live, state, transport):
    order = buy(state, "gift")
    assert fulfil_order(offers_live, order) == "delivered"
    coupon = parse_qs(transport.calls_to(f"{STRIPE}/coupons", "POST")[-1]["body"].decode())
    assert coupon["amount_off"] == ["2500"] and "applies_to[products][0]" not in coupon
    promo = parse_qs(transport.calls_to(f"{STRIPE}/promotion_codes", "POST")[-1]["body"].decode())
    assert promo["max_redemptions"] == ["1"] and promo["code"][0].startswith("GIFT-")
    assert promo["code"][0] in body(FakeSMTP.sent[-1])


def test_a_retried_gift_sends_the_same_code_with_the_same_stripe_parameters(offers_live, state, transport):
    # Audit after Phase 400: a retry (email failed after Stripe made the code) reuses the idempotency key,
    # so the parameters must match or Stripe refuses it and the buyer never gets a code.
    order = buy(state, "gift")
    rm.fulfil_gift(offers_live, order)
    rm.fulfil_gift(offers_live, order)
    coupons = [parse_qs(c["body"].decode()) for c in transport.calls_to(f"{STRIPE}/coupons", "POST")][-2:]
    promos = [parse_qs(c["body"].decode()) for c in transport.calls_to(f"{STRIPE}/promotion_codes", "POST")][-2:]
    assert coupons[0] == coupons[1] and promos[0] == promos[1]


def test_custom_fields_from_checkout_reach_the_order(kit, state, config):
    from tools.storefront.webhook_listener import WebhookProcessor

    proc = WebhookProcessor(kit, use_sdk=False)
    proc._record_paid({"id": "cs_cf", "payment_link": "plink_x", "customer_details": {"email": "a@co.example"},
                       "amount_total": 2900, "created": 1790000000, "metadata": {"kind": "custom_request"},
                       "custom_fields": [{"key": "request", "text": {"value": "Kotlin, remote"}}]})
    meta = json.loads(state.get_order("stripe", "cs_cf")["meta"])
    assert meta == {"kind": "custom_request", "field:request": "Kotlin, remote"}


def test_order_recovery_works_for_buyers_of_offers(offers_live, state):
    # Audit after Phase 400: an offer's path is "" (the data folder itself); recovery used to try to read
    # it as a file and crash, so a lifetime-pass or gift buyer couldn't recover anything.
    from tools.storefront.recovery_endpoint import RecoveryService

    order = buy(state, "gift", email="giver@co.example")
    state.set_order_status(order["id"], "delivered")
    assert RecoveryService(offers_live).dispatch("giver@co.example")["sent"] is True
