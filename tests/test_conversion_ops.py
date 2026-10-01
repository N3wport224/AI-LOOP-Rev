"""Phases 36-39: bundle upgrade credit, sample-to-paid offer, testimonials, quarterly sale."""

import json
from datetime import timedelta
from urllib.parse import parse_qs

import pytest

from strategies.bundle_upgrade import BundleUpgrade
from strategies.owner_reports import OwnerReports
from strategies.owner_todo import todo
from strategies.sample_offer import SampleOffer
from strategies.seasonal_sale import SeasonalSale, active_sale
from strategies.share_kit import build_kit
from strategies.support_desk import SupportDesk
from strategies.testimonials import approved_for, extract, listing, set_status
from tests import test_business_ops, test_gui
from tests.test_business_ops import STRIPE, ctx, dataset
from tests.test_distribution import FakeSMTP

kit = test_business_ops.kit  # the same live-mode toolkit fixture
gui = test_gui.gui  # the control-panel fixture


def body_of(msg):
    return msg.get_body(("plain",)).get_content() if msg.is_multipart() else msg.get_content()


def stripe_codes(transport, *refs):
    for ref in refs:
        transport.add_json(f"{STRIPE}/payment_links/{ref}/line_items", {"data": [{"price": {"product": f"prod_{ref}"}}]})
    transport.add_json(f"{STRIPE}/coupons", {"id": "co_1"})
    transport.add_json(f"{STRIPE}/promotion_codes", {"id": "promo_1"})


def form(transport, prefix):
    return parse_qs(transport.calls_to(prefix, "POST")[-1]["body"].decode())


def bought(state, aid, email, order_id, clock, days_ago=12, gross=1900):
    state.record_order("stripe", order_id, email, gross, None, aid, None, status="delivered",
                       occurred_at=(clock() - timedelta(days=days_ago)).isoformat(timespec="seconds"))


def bundle(state, kit, price=3400):
    hid = state.create_hypothesis("bundle:all:g1", "bundle", "all", {"niche": "all"})
    aid = state.add_asset(hid, "bundle", "All datasets", "assets/python-remote/python-remote-v1.zip", 1, 0, price,
                          product_ref="plink_bundle")
    state.update_asset(aid, niche="all", provider="stripe", status="published", checkout_url="https://buy.stripe.com/bundle")
    return aid


# ------------------------------------------------------------------ Phase 36: bundle upgrade credit
def test_buyers_get_the_bundle_with_what_they_paid_counted(kit, state, transport, clock):
    _, py = dataset(kit, state, "python-remote")
    dataset(kit, state, "ml-ai")
    bundle(state, kit)
    bought(state, py, "a@co.example", "cs_a", clock)
    bought(state, py, "new@co.example", "cs_n", clock, days_ago=2)  # too recent
    stripe_codes(transport, "plink_bundle")
    assert BundleUpgrade().run("offer_bundle_upgrade", ctx(kit)).metrics["sent"] == 1
    msg = FakeSMTP.sent[-1]
    body = body_of(msg)
    assert msg["To"] == "a@co.example" and "You have 1 of my 2 hiring datasets" in body
    assert "it's $15 for you" in body and "prefilled_promo_code=UPG" in body
    coupon = form(transport, f"{STRIPE}/coupons")
    assert coupon["amount_off"] == ["1900"] and coupon["applies_to[products][0]"] == ["prod_plink_bundle"]
    assert form(transport, f"{STRIPE}/promotion_codes")["max_redemptions"] == ["1"]
    assert BundleUpgrade().run("offer_bundle_upgrade", ctx(kit)).metrics["sent"] == 0  # once


def test_no_upgrade_offer_for_owners_of_everything_or_without_a_bundle(kit, state, transport, clock):
    _, py = dataset(kit, state, "python-remote")
    _, ml = dataset(kit, state, "ml-ai")
    assert BundleUpgrade().run("offer_bundle_upgrade", ctx(kit)).summary == "no bundle on sale"
    bundle(state, kit)
    bought(state, py, "all@co.example", "cs_1", clock)
    bought(state, ml, "all@co.example", "cs_2", clock)
    stripe_codes(transport, "plink_bundle")
    assert BundleUpgrade().run("offer_bundle_upgrade", ctx(kit)).metrics["sent"] == 0


def test_the_credit_never_makes_the_bundle_free(kit, state, transport, clock):
    _, py = dataset(kit, state, "python-remote")
    dataset(kit, state, "ml-ai")
    bundle(state, kit, price=2000)
    bought(state, py, "a@co.example", "cs_a", clock, gross=1900)
    bought(state, py, "a@co.example", "cs_b", clock, gross=1900)  # bought twice
    stripe_codes(transport, "plink_bundle")
    BundleUpgrade().run("offer_bundle_upgrade", ctx(kit))
    assert form(transport, f"{STRIPE}/coupons")["amount_off"] == ["1900"]  # 2000 - 100


# ------------------------------------------------------------------ Phase 37: sample-to-paid
def free_signup(state, clock, email, niche="python-remote", days_ago=20, status="active"):
    sid, _ = state.capture_free_subscriber(email, niche, f"tok-{email}", status=status)
    state._exec("UPDATE subscribers SET started_at = ? WHERE id = ?",
                ((clock() - timedelta(days=days_ago)).isoformat(timespec="seconds"), sid))
    return sid


def test_free_sample_signups_get_one_offer(kit, state, transport, clock, config):
    config.public_webhook_url = "https://hooks.example.com/webhook"
    _, py = dataset(kit, state, "python-remote")
    free_signup(state, clock, "lead@co.example")
    free_signup(state, clock, "fresh@co.example", days_ago=3)
    free_signup(state, clock, "buyer@co.example")
    bought(state, py, "buyer@co.example", "cs_b", clock)
    stripe_codes(transport, "plink_python-remote")
    assert SampleOffer().run("offer_sample_upgrade", ctx(kit)).metrics["sent"] == 1
    msg = FakeSMTP.sent[-1]
    body = body_of(msg)
    assert msg["To"] == "lead@co.example" and "$14.25 instead of $19" in body and "prefilled_promo_code=SAMPLE" in body
    assert "https://hooks.example.com/lead-magnet/unsubscribe?t=tok-lead@co.example" in body
    assert "List-Unsubscribe-Post" in msg
    assert SampleOffer().run("offer_sample_upgrade", ctx(kit)).metrics["sent"] == 0


def test_sample_offers_wait_for_compliance(kit, state, config):
    config.public_webhook_url = ""
    assert "wait" in SampleOffer().run("offer_sample_upgrade", ctx(kit)).summary
    config.sample_offer = False
    assert SampleOffer().run("offer_sample_upgrade", ctx(kit)).summary == "sample offers off"


# ------------------------------------------------------------------ Phase 38: testimonials
@pytest.mark.parametrize("body,quote", [
    ("Hi,\nWe use it every Monday to pick which companies our recruiters call. OK to quote.\nThanks!",
     "We use it every Monday to pick which companies our recruiters call"),
    ("Great data, saved me hours. You can quote me. Reach me at me@x.com or https://me.example",
     "Great data, saved me hours"),
    ("Great data, saved me hours.", None),                       # no consent: never used
    ("ok to quote", None),                                       # nothing to quote
])
def test_only_consenting_words_become_quotes(body, quote):
    assert extract("Re: Quick check", body) == quote


def test_quotes_need_your_ok_before_they_show(kit, state, clock):
    _, aid = dataset(kit, state, "python-remote")
    bought(state, aid, "fan@co.example", "cs_f", clock)
    inbox = [{"sender": "fan@co.example", "subject": "Re: Quick check", "message_id": "<q1>",
              "body": "Our SDRs live in this list now. OK to quote."}]
    res = SupportDesk(scan=lambda *a: inbox).run("answer_support", ctx(kit))
    assert res.metrics["quotes"] == 1 and res.metrics["alerted"] == 0
    [pending] = listing(state, "pending")
    assert pending["niche"] == "python-remote" and approved_for(state, "python-remote") == []
    assert any(i["title"] == "Approve 1 customer quote(s)" for i in todo(state, kit.config))
    assert set_status(state, [pending["id"]], "approved") == 1
    assert approved_for(state, "python-remote") == ["Our SDRs live in this list now"]


def test_approved_quotes_render_on_the_product_page():
    from tools.page_builder import ProductPage, render_product_page

    page = ProductPage(niche="py", title="Py", summary="s", price_cents=1900, currency="usd", checkout_url="https://buy.stripe.com/x",
                       sample_columns=[], sample_rows=[], testimonials=["Saved us <hours>"])
    html = render_product_page(page)
    assert "“Saved us &lt;hours&gt;”" in html and "Verified buyer" in html


def test_quote_approval_in_the_gui(gui, state):
    from strategies.testimonials import add_pending


    qid = add_pending(state, "x@co.example", "py", "Useful every week")
    assert add_pending(state, "x@co.example", "py", "Useful every week") == 0  # no duplicates
    with pytest.raises(ValueError):
        set_status(state, [qid], "published")

    async def scenario(client):
        csrf = await test_gui.login(client)
        data = await (await client.get("/api/testimonials")).json()
        assert [(t["text"], t["status"]) for t in data["testimonials"]] == [("Useful every week", "pending")]
        assert "email" not in data["testimonials"][0]  # the panel never needs the customer's address
        assert (await client.post("/api/testimonials", json={"action": "approve", "ids": [qid]})).status == 403
        r = await client.post("/api/testimonials", json={"action": "approve", "ids": [qid]}, headers={"X-CSRF-Token": csrf})
        assert (await r.json())["changed"] == 1
        assert (await client.post("/api/testimonials", json={"action": "x", "ids": [qid]},
                                  headers={"X-CSRF-Token": csrf})).status == 400

    test_gui.run(gui, scenario)
    assert approved_for(state, "py") == ["Useful every week"]


# ------------------------------------------------------------------ Phase 39: quarterly sale
def open_store(state, clock, days=40):
    state._exec("UPDATE assets SET created_at = ?", ((clock() - timedelta(days=days)).isoformat(timespec="seconds"),))


def test_a_sale_starts_quarterly_and_reaches_buyers_and_posts(kit, state, transport, clock):
    _, py = dataset(kit, state, "python-remote")
    dataset(kit, state, "ml-ai")
    bought(state, py, "a@co.example", "cs_a", clock, days_ago=40)
    open_store(state, clock)
    stripe_codes(transport, "plink_python-remote", "plink_ml-ai")
    res = SeasonalSale().run("run_sale", ctx(kit))
    assert res.metrics == {"sent": 1, "code": "SALESEP26"}
    coupon = form(transport, f"{STRIPE}/coupons")
    assert coupon["percent_off"] == ["25"] and len([k for k in coupon if k.startswith("applies_to")]) == 2
    msg = FakeSMTP.sent[-1]
    body = body_of(msg)
    assert msg["To"] == "a@co.example" and "ml-ai Tech Stack Intel: $14.25 instead of $19" in body
    assert "python-remote" not in body.split("SALESEP26.")[1].split("The links")[0]  # they own it already
    posts = build_kit(state, kit.files)["posts"]
    assert posts and all("SALESEP26" in p["text"] for p in posts)
    assert "Sale running: 25% off with SALESEP26" in OwnerReports.digest_body(kit, clock())
    assert SeasonalSale().run("run_sale", ctx(kit)).metrics["sent"] == 0  # announced once
    clock.advance(days=4)
    assert active_sale(state) is None and SeasonalSale().run("run_sale", ctx(kit)).summary == "no sale running"
    clock.advance(days=90)
    assert SeasonalSale().run("run_sale", ctx(kit)).metrics["code"].startswith("SALE")  # next quarter


def test_no_sale_for_a_new_store_or_in_dry_run(kit, state, transport, clock, config):
    dataset(kit, state, "python-remote")
    open_store(state, clock, days=5)
    assert SeasonalSale().run("run_sale", ctx(kit)).summary == "no sale running"
    open_store(state, clock)
    config.dry_run = True
    assert SeasonalSale().run("run_sale", ctx(kit)).summary == "no sale running"
    assert not transport.calls_to(f"{STRIPE}/coupons")


@pytest.mark.parametrize("task", ["offer_bundle_upgrade", "offer_sample_upgrade", "run_sale"])
def test_new_tasks_are_planned_and_handled(config, state, toolkit, task):
    from agent.engine import PLAN, Engine

    engine = Engine(config, state=state, sleep=lambda s: None)
    assert task in dict(PLAN) and task in engine.handlers
    assert engine.breaker.max_actions_per_cycle >= len(PLAN) + 10


def test_a_supplied_toolkit_gets_the_same_action_floor(config, state, breaker, transport):
    from agent.engine import PLAN, Engine
    from tools import build_toolkit

    breaker.max_actions_per_cycle = 10
    toolkit = build_toolkit(config, state, breaker, transport=transport, sleep=lambda s: None)
    assert Engine(config, state=state, toolkit=toolkit, sleep=lambda s: None).breaker.max_actions_per_cycle == len(PLAN) + 10
    assert json.dumps(len(PLAN))
