"""Phases 325-329: what a product earns, where it's heading, what it takes, where to focus, where you see it."""

from strategies import growth_plan as gp
from strategies import product_factory as pf
from tests import test_business_ops, test_gui
from tests.test_catalog_hygiene import made_product, sell
from tests.test_product_factory import postings, unique_links

kit = test_business_ops.kit  # the same live-mode toolkit fixture
gui = test_gui.gui


def test_earnings_projection_and_target(kit, state, config, clock, transport):
    made = made_product(kit, state, clock, transport)
    assert gp.per_product_day(state)["products"] == 0  # too new to judge
    assert gp.what_it_takes(state, config)["note"].startswith("No factory product has sold")
    clock.advance(days=10)
    sell(state, made["asset_id"], 2)  # $10 over 10 days = $1/day for the one product
    earn = gp.per_product_day(state)
    assert earn == {"products": 1, "cents_per_product_day": 100.0, "net_cents": 1000}
    config.daily_target_cents = 1000
    p = gp.what_it_takes(state, config)
    assert p["needed"] == 10 and p["days"] is None and "isn't growing" in p["note"]  # made 10 days ago: no pace this week
    postings(state, clock, 25, ["rust"], location="Austin, TX, United States", prefix="us")
    unique_links(transport)
    config.factory_types = ["slice"]
    pf.tick(kit, force=True)
    p = gp.what_it_takes(state, config)
    assert p["pace_per_day"] == round(1 / 7, 2) and p["days"] == 56 and p["products_30"] == 6
    config.factory_max_live = 5
    assert "more than factory_max_live" in gp.what_it_takes(state, config)["note"]
    config.daily_target_cents = 100
    assert gp.what_it_takes(state, config)["note"].startswith("The catalog is big enough")


def test_focus_names_strong_and_silent_types(state, clock, monkeypatch):
    import json

    pf.ensure(state)
    old = (clock() - __import__("datetime").timedelta(days=40)).isoformat(timespec="seconds")
    for kind, n, sold in (("slice", 5, 0), ("salary", 5, 3)):
        for i in range(n):
            slug = f"{kind}-{i}"
            aid = state.add_asset(None, "micro", slug, f"assets/{slug}.zip", 1, 30, 500)
            state.update_asset(aid, niche=slug, status="published")
            state._exec("INSERT INTO factory_products (slug, asset_id, title, filters, rows, companies, keys_sample, status, "
                        "created_at, published_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (slug, aid, slug, json.dumps({"type": kind}), 30, 8, "[]", "live", old, old))
            if i < sold:
                state.record_order("stripe", f"cs-{slug}", "b@co.example", 700, None, aid, None, status="delivered")
    f = gp.focus(state)
    assert f["strong"] == ["salary"] and f["weak"] == ["slice"] and f["setting"].startswith("factory_types = ['salary'")
    lines = gp.describe(gp.plan(state, __import__("agent.config", fromlist=["Config"]).Config()))
    assert any(line.startswith("No sales in 30 days from: slice") for line in lines)


def test_where_you_see_it(gui, kit, state, config, clock, transport):
    made_product(kit, state, clock, transport)

    async def scenario(client):
        await test_gui.login(client)
        d = await (await client.get("/api/products")).json()
        assert d["plan"]["live"] == 1 and d["plan"]["lines"][0].startswith("Catalog: 1 on sale")

    test_gui.run(gui, scenario)
    from dashboard.cli import build_parser

    assert build_parser().parse_args(["factory", "--plan"]).plan
