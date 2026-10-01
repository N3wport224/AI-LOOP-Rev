"""Phases 360-364: Trends tab, searchable catalog, per-product actions, plan and insights in the panel."""

from strategies import product_controls as pc
from tests import test_business_ops, test_gui
from tests.test_catalog_hygiene import made_product
from tests.test_product_factory import postings

gui = test_gui.gui  # the control-panel fixture
kit = test_business_ops.kit


def test_trends_api(gui, state, clock):
    postings(state, clock, 6, ["rust"], prefix="old", days_ago=20)
    postings(state, clock, 14, ["rust"], prefix="new", days_ago=3)

    async def scenario(client):
        await test_gui.login(client)
        d = await (await client.get("/api/trends")).json()
        assert d["rising"][0] == {"tech": "Rust", "growth_pct": 133, "before": 6, "recent": 14}
        assert ["Germany", 20] in d["countries"] and d["at"]

    test_gui.run(gui, scenario)


def test_catalog_search_and_actions(gui, kit, state, clock, transport):
    made = made_product(kit, state, clock, transport)
    pc.apply(kit, made["slug"], "pin")

    async def scenario(client):
        csrf = await test_gui.login(client)
        d = await (await client.get("/api/catalog?q=rust")).json()
        assert d["items"][0]["slug"] == made["slug"] and d["items"][0]["pinned"] and not d["items"][0]["hidden"]
        assert (await (await client.get("/api/catalog?q=kotlin")).json())["items"] == []
        assert (await (await client.get("/api/catalog?status=retired")).json())["items"] == []
        r = await (await client.post("/api/products/action", json={"slug": made["slug"], "action": "hide"},
                                     headers={"X-CSRF-Token": csrf})).json()
        assert r["ok"]
        assert (await (await client.get("/api/catalog")).json())["items"][0]["hidden"]

    test_gui.run(gui, scenario)


def test_the_panel_has_the_new_parts(gui):
    async def scenario(client):
        await test_gui.login(client)
        html = await (await client.get("/")).text()
        for part in ('data-tab="trends"', 'id="tab-trends"', 'id="catalog-table"', 'id="factory-plan"', 'id="factory-insights"'):
            assert part in html, part
        js = await (await client.get("/static/app.js")).text()
        assert "loadTrends" in js and "productAction" in js and "innerHTML" not in js.split("\n", 2)[2]

    test_gui.run(gui, scenario)
