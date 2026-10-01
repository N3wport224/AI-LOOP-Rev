"""Phases 55-59: team license, yearly plan, thank-you page, disposable-email filter, pricing page."""

import io
import json
import zipfile
from urllib.parse import parse_qs

import pytest

from strategies.plans import ANNUAL_KIND, TEAM_KIND, Plans, annual_price, plan_for, team_price
from tests import test_business_ops
from tests.test_business_ops import STRIPE, ctx, dataset
from tools.disposable import is_disposable
from tools.http_client import Response
from tools.offer_pages import render_pricing, render_thanks
from tools.page_builder import ProductPage, render_product_page

kit = test_business_ops.kit  # the same live-mode toolkit fixture


def page(**kw):
    base = dict(niche="py", title="Py Intel", summary="s", price_cents=1900, currency="usd", checkout_url="https://buy.stripe.com/py",
                sample_columns=[], sample_rows=[])
    return ProductPage(**{**base, **kw})


# ------------------------------------------------------------------ Phase 55: team license
def test_team_license_is_the_same_file_with_a_license_at_three_times_the_price(kit, state, transport):
    hid, aid = dataset(kit, state, "python-remote", price=1900)
    res = Plans().run("publish_team_license", ctx(kit))
    assert res.metrics == {"published": 1, "refreshed": 0}
    team = plan_for(state, TEAM_KIND, "python-remote")
    assert team["price_cents"] == team_price(1900, 3.0) == 5700 and team["checkout_url"]
    names = zipfile.ZipFile(io.BytesIO(kit.files.read_bytes(team["path"]))).namelist()
    assert "leads.csv" in names and "LICENSE-TEAM.txt" in names
    assert Plans().run("publish_team_license", ctx(kit)).metrics == {"published": 0, "refreshed": 0}

    v2 = state.add_asset(hid, "lead_directory", "python-remote Tech Stack Intel", state.get_asset(aid)["path"], 2, 80, 1900,
                         product_ref="plink_python-remote")
    state.update_asset(v2, provider="stripe", status="published", checkout_url="https://buy.stripe.com/python-remote")
    assert Plans().run("publish_team_license", ctx(kit)).metrics["refreshed"] == 1  # same link, new file
    state._exec("UPDATE assets SET price_cents = 2900 WHERE id = ?", (v2,))
    assert Plans().run("publish_team_license", ctx(kit)).metrics["published"] == 1
    assert state.get_asset(team["id"])["status"] == "retired" and plan_for(state, TEAM_KIND, "python-remote")["price_cents"] == 8700


def test_team_and_yearly_options_show_on_the_dataset_page_only():
    html = render_product_page(page(team_url="https://buy.stripe.com/team", team_price_cents=5700, team_seats=10,
                                    annual_url="https://buy.stripe.com/year", annual_price_cents=10000))
    assert "Team license for up to 10 people: $57" in html and "Yearly: $100/year" in html


def test_share_kit_skips_plan_variants(kit, state):
    from strategies.share_kit import shareable

    dataset(kit, state, "python-remote")
    Plans().run("publish_team_license", ctx(kit))
    assert [p["kind"] for p in shareable(state)] == ["lead_directory"]


# ------------------------------------------------------------------ Phase 56: yearly plan
def test_yearly_plan_costs_ten_months(kit, state, transport):
    hid, _ = dataset(kit, state, "python-remote")
    sub = state.add_asset(hid, "subscription", "Python Remote weekly", "assets/python-remote/python-remote-v1.zip", 1, 50, 1000,
                          product_ref="plink_sub")
    state.update_asset(sub, niche="python-remote", provider="stripe", status="published", checkout_url="https://buy.stripe.com/sub",
                       kind_meta=json.dumps({"interval": "month"}))
    assert Plans().run("publish_annual_plan", ctx(kit)).metrics["published"] == 1
    annual = plan_for(state, ANNUAL_KIND, "python-remote")
    assert annual["price_cents"] == annual_price(1000, 10) == 10000
    assert json.loads(annual["kind_meta"])["interval"] == "year"
    price_call = [c for c in transport.calls_to(f"{STRIPE}/prices", "POST") if b"recurring" in c["body"]][-1]
    assert parse_qs(price_call["body"].decode())["recurring[interval]"] == ["year"]
    assert Plans().run("publish_annual_plan", ctx(kit)).metrics["published"] == 0
    from strategies.subscription_engine import subscription_asset

    assert subscription_asset(state, "python-remote")["id"] == sub  # the monthly lookup is unchanged


# ------------------------------------------------------------------ Phases 57 & 59: thank-you and pricing pages
def test_thank_you_and_pricing_pages():
    pages = [page(subscription_url="https://buy.stripe.com/sub", subscription_price_cents=1000, team_url="https://buy.stripe.com/t",
                  team_price_cents=5700, team_seats=10, annual_url="https://buy.stripe.com/y", annual_price_cents=10000),
             page(niche="all", kind="bundle", title="All", price_cents=3400, checkout_url="https://buy.stripe.com/all")]
    thanks = render_thanks(pages, "Tech Stack Intel")
    assert "Your file is on its way to your inbox" in thanks and 'content="noindex"' in thanks
    assert "https://buy.stripe.com/all" in thanks and "Team license" in thanks
    pricing = render_pricing(pages, "Tech Stack Intel")
    assert all(x in pricing for x in ("$19", "$10/month", "$100/year", "$57", "$34"))


def test_links_switch_to_the_thank_you_page_once_it_is_online(kit, state, transport, config):
    from tools.storefront.stripe_pages_publisher import link_options

    dataset(kit, state, "python-remote")
    assert "off" in Plans().run("checkout_thank_you", ctx(kit)).summary
    config.pages_base_url = "https://me.github.io/d"
    assert "not online yet" in Plans().run("checkout_thank_you", ctx(kit)).summary
    assert "after_completion" not in link_options(config)
    transport.add("https://me.github.io/d/thanks/", Response(200, "x", b"ok", {}))
    transport.add_json(f"{STRIPE}/payment_links/plink_python-remote", {"id": "plink_python-remote"})
    assert Plans().run("checkout_thank_you", ctx(kit)).metrics["switched"] == 1
    body = parse_qs(transport.calls_to(f"{STRIPE}/payment_links/plink_python-remote", "POST")[-1]["body"].decode())
    assert body["after_completion[type]"] == ["redirect"]
    assert body["after_completion[redirect][url]"] == ["https://me.github.io/d/thanks/?session_id={CHECKOUT_SESSION_ID}"]
    assert link_options(config)["after_completion"]["type"] == "redirect"  # new links too
    assert Plans().run("checkout_thank_you", ctx(kit)).metrics["switched"] == 0


# ------------------------------------------------------------------ Phase 58: disposable emails
@pytest.mark.parametrize("email,blocked", [
    ("a@mailinator.com", True), ("a@x.mailinator.com", True), ("a@yopmail.com", True),
    ("a@gmail.com", False), ("a@company.example", False), ("a@notmailinator.com", False),
])
def test_disposable_domains(email, blocked):
    assert is_disposable(email) is blocked


def test_the_sample_form_refuses_throwaway_inboxes(kit, state, config):
    from strategies.lead_magnet import capture

    config.lead_magnet_enabled, config.public_webhook_url = True, "https://hooks.example.com/webhook"
    assert capture(kit, "x@mailinator.com", "python-remote", "", "", "", "1.1.1.1", None).status == "disposable"
    config.blocked_signup_domains = ["spam.example"]
    assert capture(kit, "x@spam.example", "python-remote", "", "", "", "1.1.1.2", None).status == "disposable"
    assert state.free_subscriber_counts() == {}


@pytest.mark.parametrize("task", ["publish_team_license", "publish_annual_plan", "checkout_thank_you"])
def test_new_tasks_are_planned_and_handled(config, state, toolkit, task):
    from agent.engine import PLAN, Engine

    engine = Engine(config, state=state, sleep=lambda s: None)
    assert task in dict(PLAN) and task in engine.handlers
    assert engine.breaker.max_actions_per_cycle >= len(PLAN) + 10
