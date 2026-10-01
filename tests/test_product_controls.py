"""Phases 280-284: pin, retire, price, hide, rebuild."""

import pytest

from strategies import catalog_hygiene as ch
from strategies import product_controls as pc
from strategies import product_factory as pf
from strategies import site_discovery as sd
from strategies.inbound_syndicator import site_pages
from strategies.money_insight import price_history
from strategies.product_types import refresh_due
from tests import test_business_ops, test_gui
from tests.test_catalog_hygiene import made_product, sell
from tests.test_product_factory import postings

kit = test_business_ops.kit  # the same live-mode toolkit fixture
gui = test_gui.gui


def status(state, slug):
    return state._one("SELECT status FROM factory_products WHERE slug = ?", (slug,))["status"]


def test_pinned_products_are_neither_retired_nor_repriced(kit, state, config, clock, transport):
    made = made_product(kit, state, clock, transport)
    assert pc.apply(kit, made["slug"], "pin").startswith("Pinned")
    clock.advance(days=config.factory_retire_days + 1)
    assert pf.retire_unsold(kit) == [] and status(state, made["slug"]) == "live"
    sell(state, made["asset_id"], ch.RAISE_AFTER)
    postings(state, clock, 5, ["rust"], prefix="more")
    refresh_due(kit)
    row = state._one("SELECT asset_id FROM factory_products WHERE slug = ?", (made["slug"],))
    assert state.get_asset(row["asset_id"])["price_cents"] == made["price_cents"]
    pc.apply(kit, made["slug"], "unpin")
    assert not pc.pinned(state, made["slug"])


def test_retire_now(kit, state, clock, transport):
    made = made_product(kit, state, clock, transport)
    ref = state.get_asset(made["asset_id"])["product_ref"]
    assert pc.apply(kit, made["slug"], "retire").startswith("Retired")
    assert status(state, made["slug"]) == "retired" and transport.calls_to(f"{test_business_ops.STRIPE}/payment_links/{ref}", "POST")
    with pytest.raises(ValueError, match="isn't on sale"):
        pc.apply(kit, made["slug"], "retire")


def test_set_a_price(kit, state, clock, transport):
    made = made_product(kit, state, clock, transport)
    before = state.get_asset(made["asset_id"])
    assert pc.apply(kit, made["slug"], "price", "1900").endswith("now costs $19.00 (new checkout; the old link is closed).")
    after = state.get_asset(made["asset_id"])
    assert after["price_cents"] == 1900 and after["product_ref"] != before["product_ref"]
    assert price_history(state, 1)[0]["new_cents"] == 1900
    assert "now costs $9.00" in pc.apply(kit, made["slug"], "price", "$9")  # dollars work too
    assert state.get_asset(made["asset_id"])["price_cents"] == 900
    for bad in ("50", "abc", "999999", "$0.5"):
        with pytest.raises(ValueError):
            pc.apply(kit, made["slug"], "price", bad)


def test_hide_and_show(kit, state, config, clock, transport):
    made = made_product(kit, state, clock, transport)
    pc.apply(kit, made["slug"], "hide")
    assert made["slug"] not in {p.slug for p in site_pages(kit)}
    assert state.get_asset(made["asset_id"])["checkout_url"]  # the checkout still works
    assert f'href="../{made["slug"]}/"' not in sd.changes_page(state, lambda t, b, d: b)  # no dead link
    pc.apply(kit, made["slug"], "show")
    assert made["slug"] in {p.slug for p in site_pages(kit)}


def test_rebuild_now(kit, state, clock, transport):
    made = made_product(kit, state, clock, transport)
    postings(state, clock, 5, ["rust"], prefix="more")
    assert pc.apply(kit, made["slug"], "rebuild").startswith("Rebuilt")
    row = state._one("SELECT asset_id FROM factory_products WHERE slug = ?", (made["slug"],))
    assert state.get_asset(row["asset_id"])["version"] == 2


def test_unknown_products_and_actions(kit, state):
    with pytest.raises(ValueError, match="no product"):
        pc.apply(kit, "nope", "pin")


def test_panel_action(gui, state, clock):
    async def scenario(client):
        csrf = await test_gui.login(client)
        r = await client.post("/api/products/action", json={"slug": "nope", "action": "pin"}, headers={"X-CSRF-Token": csrf})
        assert r.status == 400 and "no product" in (await r.json())["message"]
        r = await client.post("/api/products/action", json={"slug": "nope", "action": "pin"})
        assert r.status == 403  # no CSRF token

    test_gui.run(gui, scenario)


def test_cli_parses():
    from dashboard.cli import build_parser

    args = build_parser().parse_args(["product", "hiring-rust", "price", "900"])
    assert (args.slug, args.action, args.value) == ("hiring-rust", "price", "900")
