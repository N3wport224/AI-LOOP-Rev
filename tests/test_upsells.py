"""Phases 160-164: related-datasets email, often bought together, demand-led factory, thank-you offers, best sellers."""

from datetime import timedelta

from strategies import product_factory as pf
from strategies import upsells
from strategies.base import TaskContext
from strategies.inbound_syndicator import site_pages
from tests import test_business_ops
from tests.test_distribution import FakeSMTP
from tests.test_product_factory import postings, unique_links
from tools import contact_policy as contact

kit = test_business_ops.kit  # the same live-mode toolkit fixture


def ctx(kit):
    return TaskContext(kit, {"id": 1, "params": {}, "iterations": 0}, {})


def three_rust_products(kit, state, clock, transport):
    unique_links(transport)
    postings(state, clock, 30, ["rust"], location="Berlin, Germany")
    postings(state, clock, 30, ["rust"], location="Austin, TX, United States", prefix="us")
    postings(state, clock, 30, ["rust"], location="Remote", remote=True, prefix="rm")
    made = []
    for _ in range(3):
        out = pf.tick(kit, force=True)["made"]
        made.append(out)
    return made


def bought(state, clock, asset_id, email="buyer@co.example", days=6):
    when = (clock() - timedelta(days=days)).isoformat(timespec="seconds")
    state.record_order("stripe", f"cs_{asset_id}_{email}", email, 900, None, asset_id, None, status="delivered", occurred_at=when)
    state._exec("UPDATE orders SET delivered_at = ? WHERE order_id = ?", (when, f"cs_{asset_id}_{email}"))


# ------------------------------------------------------------------ Phase 160
def test_buyers_get_one_related_email_within_the_rules(kit, state, clock, transport, config):
    made = three_rust_products(kit, state, clock, transport)
    rust = [m for m in made if "rust" in m["slug"]]
    assert len(rust) >= 2
    bought(state, clock, rust[0]["asset_id"])
    before = len(FakeSMTP.sent)
    assert upsells.Upsells().run("offer_related", ctx(kit)).metrics["sent"] == 1
    msg = FakeSMTP.sent[-1]
    text = msg.get_content() if not msg.is_multipart() else msg.get_body(("plain",)).get_content()
    assert len(FakeSMTP.sent) == before + 1 and msg["Subject"] == "More on the same topic"
    assert f"- {rust[1]['title']} (" in text and f"- {rust[0]['title']} (" not in text and "unsubscribe" in text
    assert upsells.Upsells().run("offer_related", ctx(kit)).metrics["sent"] == 0  # once per order
    bought(state, clock, rust[1]["asset_id"], email="other@co.example")
    contact.set_quiet(state, True)
    assert upsells.Upsells().run("offer_related", ctx(kit)).metrics["sent"] == 0  # quiet mode holds it


# ------------------------------------------------------------------ Phase 161
def test_product_pages_show_related_products(kit, state, clock, transport):
    three_rust_products(kit, state, clock, transport)
    micro = [p for p in site_pages(kit) if p.kind == "micro"]
    assert micro and all("Often bought together" in p.related_html for p in micro)
    assert micro[0].slug not in micro[0].related_html


# ------------------------------------------------------------------ Phase 162
def test_the_factory_follows_what_sells(kit, state, clock, transport, config):
    unique_links(transport)
    postings(state, clock, 40, ["kotlin"], prefix="k")  # bigger
    postings(state, clock, 30, ["rust"], prefix="r")
    first = pf.candidates(state, config)[0]
    assert first["filters"]["tech"] == "kotlin"
    made = pf.tick(kit, force=True)["made"]
    rust = pf.slice_spec("rust")
    assert made["slug"] != rust["slug"]
    state.record_order("stripe", "cs_k", "a@co.example", 900, None, made["asset_id"], None, status="delivered")
    config.factory_min_rows = 20
    # kotlin sold: kotlin ideas outrank the bigger-but-unsold ones from now on
    assert pf.candidates(state, config)[0]["filters"]["tech"] == "kotlin"
    assert upsells.selling_techs(state, config)["kotlin"] == 1


# ------------------------------------------------------------------ Phases 163-164
def test_thank_you_offers_and_best_sellers(kit, state, clock, transport):
    from strategies.revenue_models import thanks_offers_html

    made = three_rust_products(kit, state, clock, transport)
    assert thanks_offers_html(state) == ""  # no offers created yet
    for m in made[:2]:
        bought(state, clock, m["asset_id"])
    bought(state, clock, made[1]["asset_id"], email="two@co.example")
    best = upsells.best_sellers(state)
    assert best[0] == {"niche": made[1]["slug"], "orders": 2}
    html = upsells.best_sellers_html(state, site_pages(kit))
    assert html.index(made[1]["title"]) < html.index(made[0]["title"]) and "Best sellers this month" in html
