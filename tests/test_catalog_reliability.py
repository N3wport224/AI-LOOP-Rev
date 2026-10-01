"""Phases 240-243: current version after a price change, stubs for retired products, no pile-up, integrity."""

from strategies import catalog_hygiene as ch
from strategies import catalog_reliability as cr
from strategies import product_factory as pf
from strategies.product_types import refresh_due
from tests import test_business_ops
from tests.test_catalog_hygiene import made_product, sell
from tests.test_product_factory import postings, unique_links
from tests.test_ops_robustness import alerts

kit = test_business_ops.kit  # the same live-mode toolkit fixture
SHELL = lambda title, body, desc: f"<html><head><title>{title}</title></head><body>{body}</body></html>"  # noqa: E731


# ------------------------------------------------------------------ Phase 240
def test_an_order_on_the_previous_checkout_gets_the_current_version(kit, state, config, clock, transport):
    made = made_product(kit, state, clock, transport)
    before = state.get_asset(made["asset_id"])
    sell(state, made["asset_id"], ch.RAISE_AFTER)
    clock.advance(days=8)
    postings(state, clock, 10, ["rust"], prefix="new")
    refresh_due(kit)  # a price rise: new checkout, the old one closed
    current = state.get_asset(state._one("SELECT asset_id FROM factory_products WHERE slug = ?", (made["slug"],))["asset_id"])
    assert current["product_ref"] != before["product_ref"]
    assert state.asset_for_product(before["product_ref"])["id"] == current["id"]
    assert state.asset_for_product(current["product_ref"])["id"] == current["id"]


def test_other_assets_keep_their_lookup(kit, state):
    from tests.test_business_ops import dataset

    _, aid = dataset(kit, state, "python-remote")
    state.update_asset(aid, status="superseded", niche="python-remote")
    assert state.asset_for_product("plink_python-remote")["id"] == aid  # not a factory product: unchanged


# ------------------------------------------------------------------ Phase 241
def test_retired_products_get_a_noindex_stub_with_alternatives(kit, state, config, clock, transport):
    unique_links(transport)
    postings(state, clock, 30, ["rust"])
    postings(state, clock, 25, ["rust"], location="Austin, TX, United States", prefix="us")
    config.factory_types = ["slice"]
    a = pf.tick(kit, force=True)["made"]
    b = pf.tick(kit, force=True)["made"]
    state._exec("UPDATE factory_products SET status = 'retired', retired_at = ? WHERE slug = ?", (state.now(), a["slug"]))
    built = {f"{b['slug']}/index.html", "catalog/index.html"}
    stubs = cr.retired_stubs(state, built, SHELL)
    page = stubs[f"{a['slug']}/index.html"]
    assert '<meta name="robots" content="noindex">' in page and "no longer on sale" in page
    assert f'<a href="../{b["slug"]}/">' in page and 'href="../catalog/"' in page
    assert cr.retired_stubs(state, built | {f"{a['slug']}/index.html"}, SHELL) == {}  # still built: no stub
    clock.advance(days=cr.STUB_DAYS + 1)
    assert cr.retired_stubs(state, built, SHELL) == {}  # after 90 days the page goes


# ------------------------------------------------------------------ Phase 242
def test_the_factory_stops_when_too_many_wait_for_a_checkout(kit, state, config, clock, transport, monkeypatch):
    unique_links(transport)
    postings(state, clock, 30, ["rust"])
    monkeypatch.setattr(cr, "STAGED_MAX", 1)
    config.stripe_secret_key = ""  # no checkout possible: products wait staged
    first = pf.tick(kit, force=True)
    assert first["made"] and first["status"] == "staged"
    second = pf.tick(kit, force=True)
    assert second["made"] is None and second["why"].startswith("1 product(s) are waiting for a checkout")


# ------------------------------------------------------------------ Phase 243
def test_a_missing_download_is_rebuilt(kit, state, config, clock, transport):
    made = made_product(kit, state, clock, transport)
    asset = state.get_asset(made["asset_id"])
    kit.files.resolve(asset["path"]).unlink()
    clock.advance(hours=2)
    out = pf.tick(kit)
    assert made["slug"] in out["refreshed"]
    current = state.get_asset(state._one("SELECT asset_id FROM factory_products WHERE slug = ?", (made["slug"],))["asset_id"])
    assert kit.files.exists(current["path"]) and current["checkout_url"] == asset["checkout_url"]
    assert any("had no download file" in a for a in alerts(state))
    assert cr.check_integrity(kit) == []  # hourly


def test_a_download_that_cannot_be_rebuilt_is_taken_off_sale(kit, state, config, clock, transport):
    made = made_product(kit, state, clock, transport)
    asset = state.get_asset(made["asset_id"])
    kit.files.resolve(asset["path"]).unlink()
    state._exec("DELETE FROM leads")  # its postings are gone: nothing to rebuild from
    clock.advance(hours=2)
    pf.tick(kit)
    assert state._one("SELECT status FROM factory_products WHERE slug = ?", (made["slug"],))["status"] == "live"  # one more try
    clock.advance(hours=2)
    pf.tick(kit)
    assert state._one("SELECT status FROM factory_products WHERE slug = ?", (made["slug"],))["status"] == "retired"
    assert transport.calls_to(f"{test_business_ops.STRIPE}/payment_links/{asset['product_ref']}", "POST")
    assert any("taken off sale" in a for a in alerts(state))
