"""Phases 210-214: retired pages come down, Stripe stays tidy, prices follow demand, disk stays tidy."""

import json

from strategies import catalog_hygiene as ch
from strategies import product_factory as pf
from strategies.product_types import refresh_due
from tests import test_business_ops
from tests.test_business_ops import STRIPE
from tests.test_page_builder import NOW, page
from tests.test_product_factory import postings, unique_links
from tools.http_client import Response
from tools.page_builder import SiteBuilder

kit = test_business_ops.kit  # the same live-mode toolkit fixture
GH = "https://api.github.com/repos/me/me.github.io/contents/"


def stripe_products(transport):
    """Every link's line items name product prod_l; link updates succeed."""
    def route(method, url, headers):
        if url.split("?")[0].endswith("/line_items"):
            return Response(200, url, json.dumps({"data": [{"price": {"product": "prod_l"}}]}).encode(), {})
        return Response(200, url, b'{"id": "x"}', {})
    transport.add(f"{STRIPE}/payment_links/", route)


def made_product(kit, state, clock, transport):
    unique_links(transport)
    stripe_products(transport)
    postings(state, clock, 30, ["rust"])
    return pf.tick(kit, force=True)["made"]


def sell(state, asset_id, n, prefix="cs"):
    for i in range(n):
        state.record_order("stripe", f"{prefix}_{i}", f"b{i}@co.example", 500, None, asset_id, None, status="delivered")


# ------------------------------------------------------------------ Phase 210
def site(config, toolkit, transport):
    config.github_token, config.github_pages_repo, config.pages_base_url = "ghp", "me/me.github.io", "https://me.github.io"
    config.github_pages_branch, config.github_pages_dir, config.og_images = "gh-pages", "", False
    toolkit.github.token = "ghp"
    transport.add(GH, lambda m, url, h: Response(200, url, json.dumps({"sha": "abc"}).encode(), {}) if m == "GET"
                  else Response(201, url, b"{}", {}))
    b = SiteBuilder(config, toolkit.files, toolkit.github)
    return b, b.build([page()], [], NOW)


def test_pages_no_longer_built_are_deleted_but_only_our_own(config, toolkit, transport, state):
    b, out = site(config, toolkit, transport)
    b.publish(out, state)
    key = next(k for k in state._all("SELECT key FROM kv") if k["key"].startswith("site_manifest:"))["key"]
    manifest = state.get(key)
    state.set(key, {**manifest, "old-product/index.html": "x"})
    assert b.publish(out, state) == 1  # nothing else changed: just the removal
    deletes = transport.calls_to(GH, "DELETE")
    assert [c["url"].split("/contents/")[1] for c in deletes] == ["old-product/index.html"]
    assert json.loads(deletes[0]["body"]) == {"message": "site: remove old-product/index.html", "sha": "abc", "branch": "gh-pages"}
    assert "old-product/index.html" not in state.get(key)
    assert b.publish(out, state) == 0 and len(transport.calls_to(GH, "DELETE")) == 1  # never a file we didn't publish


def test_a_build_that_looks_broken_deletes_nothing(config, toolkit, transport, state):
    b, out = site(config, toolkit, transport)
    b.publish(out, state)
    tiny = {"index.html": out["index.html"]}
    assert b.publish(tiny, state) == 0
    assert transport.calls_to(GH, "DELETE") == []
    assert state.recent_errors(5)[0]["message"].startswith("not removing")


# ------------------------------------------------------------------ Phase 211
def test_retiring_archives_the_stripe_product(kit, state, config, clock, transport):
    made = made_product(kit, state, clock, transport)
    clock.advance(days=config.factory_retire_days + 1)
    assert pf.retire_unsold(kit) == [made["slug"]]
    posts = transport.calls_to(f"{STRIPE}/products/prod_l", "POST")
    assert posts and b"active=false" in posts[-1]["body"]


# ------------------------------------------------------------------ Phases 212 + 214
def test_a_product_selling_well_moves_up_one_price_step(kit, state, config, clock, transport):
    made = made_product(kit, state, clock, transport)
    before = state.get_asset(made["asset_id"])
    sell(state, made["asset_id"], ch.RAISE_AFTER)
    clock.advance(days=8)
    postings(state, clock, 10, ["rust"], prefix="new")
    assert refresh_due(kit) == [made["slug"]]
    after = state.get_asset(state._one("SELECT asset_id FROM factory_products WHERE slug = ?", (made["slug"],))["asset_id"])
    step = next(p for p in ch.PRICE_STEPS if p > before["price_cents"])
    assert after["price_cents"] == step and after["product_ref"] != before["product_ref"]
    assert transport.calls_to(f"{STRIPE}/payment_links/{before['product_ref']}", "POST")  # the old link is closed
    from strategies.money_insight import price_history

    assert price_history(state, 1)[0]["new_cents"] == step  # shows in the Money tab
    described = [c for c in transport.calls_to(f"{STRIPE}/products/prod_l", "POST") if b"description=" in (c["body"] or b"")]
    assert described and b"40" in described[-1]["body"]  # Phase 214: the new row count


def test_slow_sellers_keep_their_price(kit, state, config, clock, transport):
    made = made_product(kit, state, clock, transport)
    sell(state, made["asset_id"], ch.RAISE_AFTER - 1)
    assert ch.next_price(state, made["slug"], 500) == 500
    assert ch.next_price(state, made["slug"], ch.PRICE_STEPS[-1]) == ch.PRICE_STEPS[-1]  # never past the top
    sell(state, made["asset_id"], 1, prefix="more")
    assert ch.next_price(state, made["slug"], 500) == 700
    clock.advance(days=31)
    assert ch.next_price(state, made["slug"], 500) == 500  # only the last 30 days count


# ------------------------------------------------------------------ Phase 213
def test_files_of_long_retired_unsold_products_are_deleted(kit, state, config, clock, transport):
    made = made_product(kit, state, clock, transport)
    clock.advance(days=config.factory_retire_days + 1)
    pf.retire_unsold(kit)
    folder = f"assets/{made['slug']}"
    assert kit.files.exists(folder)
    assert ch.prune_retired(state, kit.files) == 0  # retired too recently
    clock.advance(days=ch.DISK_DAYS + 1)
    assert ch.prune_retired(state, kit.files) == 1 and not kit.files.exists(folder)


def test_files_someone_bought_are_kept(kit, state, config, clock, transport):
    made = made_product(kit, state, clock, transport)
    state._exec("UPDATE factory_products SET status = 'retired', retired_at = ? WHERE slug = ?", (state.now(), made["slug"]))
    sell(state, made["asset_id"], 1)
    clock.advance(days=ch.DISK_DAYS + 1)
    assert ch.prune_retired(state, kit.files) == 0 and kit.files.exists(f"assets/{made['slug']}")
