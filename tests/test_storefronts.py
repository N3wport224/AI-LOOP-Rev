import json
import urllib.parse
from datetime import timedelta

import pytest

from tests.conftest import NOW
from tools.http_client import Response
from tools.storefront import GumroadStaging, Listing, Order, build_storefronts, price_for
from tools.storefront.lemon_squeezy import LemonSqueezyStorefront, build_checkout_payload, validate_price
from tools.storefront.stripe_pages_publisher import (
    PagesDeployer, StripeStorefront, flatten_form, render_lander_html, render_lander_md,
)

LS = "https://api.lemonsqueezy.com/v1"
STRIPE = "https://api.stripe.com/v1"


def listing(**kw):
    base = dict(
        asset_id=7, hypothesis_id=3, niche="python-remote", title="Python Remote Tech Stack Intel",
        summary="42 companies", description_md="# x", price_cents=900, zip_path="assets/python-remote/x.zip",
        sample_rows=[{"company": "<b>Acme</b>", "stack": ["Python", "AWS"], "urgency_score": 80}],
        sample_columns=["company", "stack", "urgency_score"],
    )
    base.update(kw)
    return Listing(**base)


# ------------------------------------------------------------------ pricing & selection
def test_price_tiers_clamped_to_band():
    tiers = [[0, 500], [25, 900], [75, 1500]]
    assert price_for(3, tiers) == 500
    assert price_for(25, tiers) == 900
    assert price_for(500, tiers) == 1500
    assert price_for(1000, [[0, 99], [10, 5000]]) == 1900
    assert price_for(1, [[0, 99]]) == 500


def test_storefront_selection_and_fallback(config, toolkit):
    assert [s.name for s in build_storefronts(config, toolkit.http)] == ["gumroad"]
    config.lemonsqueezy_api_key, config.lemonsqueezy_store_id, config.lemonsqueezy_variant_id = "k", "1", "2"
    config.stripe_secret_key = "sk_test"
    assert [s.name for s in build_storefronts(config, toolkit.http)] == ["stripe", "lemonsqueezy", "gumroad"]
    config.storefront_provider = "lemonsqueezy"
    assert build_storefronts(config, toolkit.http)[0].name == "lemonsqueezy"
    config.lemonsqueezy_api_key = ""
    # asked for Lemon Squeezy without credentials: fall back instead of publishing nowhere
    assert build_storefronts(config, toolkit.http)[0].name == "stripe"
    config.storefront_provider = "nope"
    with pytest.raises(ValueError):
        build_storefronts(config, toolkit.http)


def test_gumroad_staging_fallback(config):
    res = GumroadStaging(config).publish(listing(), None)
    assert not res.live and "manual" in res.detail
    config.storefront_url = "https://x.gumroad.com/l/abc"
    assert GumroadStaging(config).publish(listing(), None).live


# ------------------------------------------------------------------ Lemon Squeezy
def test_checkout_payload_shape():
    p = build_checkout_payload("11", "22", listing(), redirect_url="https://me.github.io/x")
    d = p["data"]
    assert d["type"] == "checkouts"
    assert d["attributes"]["custom_price"] == 900
    assert d["attributes"]["product_options"]["name"] == "Python Remote Tech Stack Intel"
    assert d["attributes"]["product_options"]["enabled_variants"] == [22]
    assert d["attributes"]["product_options"]["redirect_url"] == "https://me.github.io/x"
    assert d["attributes"]["checkout_data"]["custom"] == {"asset_id": "7", "niche": "python-remote", "hypothesis_id": "3"}
    assert d["relationships"]["store"]["data"] == {"type": "stores", "id": "11"}
    assert d["relationships"]["variant"]["data"] == {"type": "variants", "id": "22"}


@pytest.mark.parametrize("cents", [0, 499, 1901, 5000])
def test_price_band_enforced(cents):
    with pytest.raises(ValueError):
        validate_price(cents)


def _ls(config, toolkit, transport, files=None):
    config.lemonsqueezy_api_key, config.lemonsqueezy_store_id, config.lemonsqueezy_variant_id = "ls_key", "11", "22"
    transport.add_json(f"{LS}/variants/", {"data": {"id": "22", "attributes": {"status": "published"}}})
    transport.add_json(f"{LS}/files", {"data": files or []})
    transport.add_json(f"{LS}/checkouts", {"data": {"id": "c1", "attributes": {"url": "https://store.lemonsqueezy.com/checkout/custom/abc"}}}, 201)
    return LemonSqueezyStorefront(config, toolkit.http)


def test_lemonsqueezy_publish_shared_variant(config, toolkit, transport):
    sf = _ls(config, toolkit, transport)
    res = sf.publish(listing(), None)
    assert res.live and res.checkout_url.endswith("/abc")
    assert res.product_ref == "ls-shared:22:7"  # shared variant can't identify the dataset
    assert not res.provider_delivers
    post = transport.calls_to(f"{LS}/checkouts", "POST")[0]
    assert post["headers"]["Authorization"] == "Bearer ls_key"
    assert post["headers"]["Content-Type"] == "application/vnd.api+json"
    assert json.loads(post["body"])["data"]["attributes"]["custom_price"] == 900
    assert "filter%5Bvariant_id%5D=22" in transport.calls_to(f"{LS}/files")[0]["url"]


def test_lemonsqueezy_dedicated_variant_with_file_delivers(config, toolkit, transport):
    sf = _ls(config, toolkit, transport, files=[{"id": "f1"}])
    config.lemonsqueezy_variant_map = {"python-remote": "33"}
    res = sf.publish(listing(), None)
    assert res.product_ref == "33" and res.provider_delivers


def test_lemonsqueezy_orders_parsing(config, toolkit, transport):
    sf = _ls(config, toolkit, transport)
    transport.add_json(f"{LS}/orders", {
        "meta": {"page": {"lastPage": 1}},
        "data": [
            {"id": "501", "attributes": {"status": "paid", "user_email": "Buyer@x.com", "total": 1080, "tax": 180,
                                         "created_at": (NOW - timedelta(hours=2)).isoformat(),
                                         "first_order_item": {"variant_id": 33, "product_name": "Python Remote Tech Stack Intel"}}},
            {"id": "502", "attributes": {"status": "refunded", "refunded": True, "total": 900, "tax": 0,
                                         "created_at": NOW.isoformat(), "first_order_item": {"variant_id": 33}}},
            {"id": "503", "attributes": {"status": "pending", "total": 900, "created_at": NOW.isoformat()}},
            {"id": "400", "attributes": {"status": "paid", "total": 900, "created_at": (NOW - timedelta(days=30)).isoformat()}},
        ],
    })
    orders = sf.fetch_orders([], NOW - timedelta(days=3))
    assert [o.order_id for o in orders] == ["501", "502"]
    assert orders[0].gross_cents == 900 and orders[0].product_ref == "33" and orders[0].email == "Buyer@x.com"
    assert orders[1].refunded


# ------------------------------------------------------------------ Stripe
def test_flatten_form():
    assert flatten_form({"line_items": [{"price": "p", "quantity": 1}], "metadata": {"a": 1}, "active": False}) == {
        "line_items[0][price]": "p", "line_items[0][quantity]": "1", "metadata[a]": "1", "active": "false",
    }


def _stripe(config, toolkit, transport):
    config.stripe_secret_key = "sk_test_123"
    transport.add_json(f"{STRIPE}/products", {"id": "prod_1"})
    transport.add_json(f"{STRIPE}/prices", {"id": "price_1"})
    transport.add_json(f"{STRIPE}/payment_links", {"id": "plink_1", "url": "https://buy.stripe.com/test_1"})
    return StripeStorefront(config, toolkit.http)


def test_stripe_publish_creates_product_price_link(config, toolkit, transport):
    res = _stripe(config, toolkit, transport).publish(listing(), None)
    assert res.live and res.checkout_url == "https://buy.stripe.com/test_1" and res.product_ref == "plink_1"
    posts = [c for c in transport.calls if c["method"] == "POST"]
    assert [c["url"].rsplit("/", 1)[-1] for c in posts] == ["products", "prices", "payment_links"]
    assert all(c["headers"]["Authorization"] == "Bearer sk_test_123" for c in posts)
    assert all(c["headers"]["Idempotency-Key"].startswith("automonetize-3-7-900") for c in posts)
    price_form = urllib.parse.parse_qs(posts[1]["body"].decode())
    assert price_form == {"product": ["prod_1"], "unit_amount": ["900"], "currency": ["usd"]}
    link_form = urllib.parse.parse_qs(posts[2]["body"].decode())
    assert link_form["line_items[0][price]"] == ["price_1"] and link_form["metadata[asset_id]"] == ["7"]


def test_stripe_reuses_link_when_price_unchanged(config, toolkit, transport):
    sf = _stripe(config, toolkit, transport)
    prev = {"provider": "stripe", "product_ref": "plink_0", "checkout_url": "https://buy.stripe.com/old", "price_cents": 900}
    res = sf.publish(listing(), prev)
    assert res.checkout_url == "https://buy.stripe.com/old" and not transport.calls
    res2 = sf.publish(listing(price_cents=1500), prev)
    assert res2.product_ref == "plink_1"
    assert transport.calls_to(f"{STRIPE}/payment_links/plink_0", "POST"), "old link deactivated"


def test_stripe_without_key_uses_preconfigured_link(config, toolkit):
    config.stripe_payment_links = {"python-remote": "https://buy.stripe.com/pre"}
    sf = StripeStorefront(config, toolkit.http)
    res = sf.publish(listing(), None)
    assert res.live and res.checkout_url == "https://buy.stripe.com/pre" and res.product_ref is None
    assert not sf.publish(listing(niche="other"), None).live
    assert sf.fetch_orders(["plink_1"], NOW) == []


def test_stripe_orders_from_sessions_with_pagination(config, toolkit, transport):
    sf = _stripe(config, toolkit, transport)
    transport.add(f"{STRIPE}/checkout/sessions", [
        Response(200, "u", json.dumps({"has_more": True, "data": [
            {"id": "cs_1", "payment_status": "paid", "amount_total": 900, "created": int(NOW.timestamp()),
             "customer_details": {"email": "a@b.com"}}]}).encode()),
        Response(200, "u", json.dumps({"has_more": False, "data": [
            {"id": "cs_2", "payment_status": "unpaid", "amount_total": 900, "created": int(NOW.timestamp())}]}).encode()),
    ])
    orders = sf.fetch_orders(["plink_1", "ls-shared:1:2", None], NOW - timedelta(days=1))
    assert [(o.order_id, o.email, o.gross_cents, o.product_ref) for o in orders] == [("cs_1", "a@b.com", 900, "plink_1")]
    calls = transport.calls_to(f"{STRIPE}/checkout/sessions")
    assert "payment_link=plink_1" in calls[0]["url"] and "starting_after=cs_1" in calls[1]["url"]


# ------------------------------------------------------------------ landers
def test_lander_escapes_and_links(config, toolkit):
    page = render_lander_html(listing(), "https://buy.stripe.com/x")
    assert "<b>Acme</b>" not in page and "&lt;b&gt;Acme&lt;/b&gt;" in page
    assert 'href="https://buy.stripe.com/x"' in page and "$9.00" in page
    assert "Checkout opens soon" in render_lander_html(listing(), "")
    assert "[Buy the full dataset ($9.00)](https://buy.stripe.com/x)" in render_lander_md(listing(), "https://buy.stripe.com/x")


def test_pages_deployer_local_and_github(config, toolkit, transport):
    deployer = PagesDeployer(config, toolkit.files, toolkit.github)
    assert deployer.deploy(listing(), "https://c") == ""
    assert toolkit.files.exists("site/python-remote/index.html")
    config.github_token, config.github_pages_repo, config.pages_base_url = "ghp", "me/pages", "https://me.github.io/pages"
    toolkit.github.token = "ghp"
    transport.add("https://api.github.com/repos/me/pages/contents/", [Response(404, "u"), Response(201, "u", b'{"content": {"html_url": "h"}}')])
    assert deployer.deploy(listing(), "https://c") == "https://me.github.io/pages/python-remote/"
    put = transport.calls_to("https://api.github.com/repos/me/pages/contents/docs/python-remote/index.html", "PUT")[0]
    assert json.loads(put["body"])["branch"] == "main" and put["headers"]["Authorization"] == "Bearer ghp"


# ------------------------------------------------------------------ order recording & attribution
def test_record_orders_attribution_and_fulfilment_queue(state, toolkit, make_hypothesis):
    hyp = make_hypothesis()
    aid = state.add_asset(hyp["id"], "lead_directory", "Python Remote Tech Stack Intel", "a.zip", 1, 10, 900, product_ref="plink_1")
    orders = [
        Order("stripe", "cs_1", "a@b.com", 900, "plink_1", NOW.isoformat()),
        Order("lemonsqueezy", "501", "c@d.com", 900, "999", NOW.isoformat(), product_name="Python Remote Tech Stack Intel"),
        Order("lemonsqueezy", "502", "e@f.com", 900, "999", NOW.isoformat(), product_name="Unknown"),
        Order("lemonsqueezy", "503", "g@h.com", 900, "999", NOW.isoformat(), refunded=True),
    ]
    rep = toolkit.revenue.record_orders(orders, fee_pct=2.9, fee_fixed_cents=30)
    assert (rep.fetched, rep.new, rep.skipped) == (4, 3, 1)
    assert rep.net_cents_added == 3 * (900 - 56)
    by_id = {o["order_id"]: o for o in state.list_orders()}
    assert by_id["cs_1"]["asset_id"] == aid and by_id["cs_1"]["status"] == "paid"
    assert by_id["501"]["asset_id"] == aid  # matched by product name
    assert by_id["502"]["status"] == "needs_manual_delivery"
    assert state.revenue_for_hypothesis(hyp["id"]) == 2 * (900 - 56)
    assert toolkit.revenue.daily_summary()["net_cents"] == 3 * (900 - 56)  # unattributed still counts
    assert toolkit.revenue.record_orders(orders, 2.9, 30).new == 0  # idempotent
