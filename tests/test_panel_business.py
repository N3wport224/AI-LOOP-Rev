"""Phases 200-204: Products, Marketing and Money tabs; sponsor and affiliate actions; AM DRAFTS."""

from agent.owner_commands import execute
from strategies import marketing_engine as me
from strategies import revenue_models as rm
from tests import test_business_ops, test_gui
from tests.test_product_factory import postings

gui = test_gui.gui  # the control-panel fixture
kit = test_business_ops.kit  # the same live-mode toolkit fixture


def test_products_tab_shows_the_catalog_and_can_make_one(gui, state, config, clock):
    postings(state, clock, 30, ["rust"])

    async def scenario(client):
        csrf = await test_gui.login(client)
        d = await (await client.get("/api/products")).json()
        assert d["catalog"] == {} and d["next"]["title"] == "Companies Hiring Rust Engineers" and d["interval_minutes"] == 10
        r = await client.post("/api/products/make", json={})
        assert r.status == 403  # no CSRF token
        r = await (await client.post("/api/products/make", json={}, headers={"X-CSRF-Token": csrf})).json()
        assert r["ok"] and r["message"].startswith("Made Companies Hiring Rust Engineers")
        d = await (await client.get("/api/products")).json()
        assert sum(d["catalog"].values()) == 1

    test_gui.run(gui, scenario)


def test_marketing_tab_lists_drafts_and_marks_them(gui, state):
    pid = me.add_play(state, "x_post", "draft", "queued")
    state._exec("UPDATE marketing_plays SET title = 'T', body = 'Post this' WHERE id = ?", (pid,))

    async def scenario(client):
        csrf = await test_gui.login(client)
        d = await (await client.get("/api/marketing")).json()
        assert d["drafts"][0]["body"] == "Post this" and d["drafts"][0]["strategy"] == "X post" and d["scores"]
        bad = await client.post("/api/marketing/mark", json={"id": pid, "status": "deleted"}, headers={"X-CSRF-Token": csrf})
        assert bad.status == 400
        r = await (await client.post("/api/marketing/mark", json={"id": pid, "status": "posted"},
                                     headers={"X-CSRF-Token": csrf})).json()
        assert r["ok"] and (await (await client.get("/api/marketing")).json())["drafts"] == []

    test_gui.run(gui, scenario)


def test_money_tab_approves_sponsors(gui, state):
    state.record_order("stripe", "cs_sp", "sp@co.example", 4900, None, None, None, status="needs_manual_delivery")
    order = state.get_order("stripe", "cs_sp")
    state.set(rm.SPONSORS, [{"id": order["id"], "email": "sp@co.example", "paid_at": state.now(), "status": "pending"}])

    async def scenario(client):
        csrf = await test_gui.login(client)
        d = await (await client.get("/api/money")).json()
        assert d["sponsors"][0]["status"] == "pending" and "forecast" in d
        bad = await client.post("/api/sponsor/approve", json={"id": order["id"], "line": "x", "url": "http://no"},
                                headers={"X-CSRF-Token": csrf})
        assert bad.status == 400
        ok = await (await client.post("/api/sponsor/approve", json={"id": order["id"], "line": "Hire with Acme",
                                                                    "url": "https://acme.example"}, headers={"X-CSRF-Token": csrf})).json()
        assert ok["ok"] and rm.active_sponsor(state)["line"] == "Hire with Acme"

    test_gui.run(gui, scenario)


def test_the_panel_page_has_the_new_tabs(gui):
    async def scenario(client):
        await test_gui.login(client)
        html = await (await client.get("/")).text()
        for tab in ("products", "marketing", "money"):
            assert f'data-tab="{tab}"' in html and f'id="tab-{tab}"' in html
        js = await (await client.get("/static/app.js")).text()
        assert "loadProducts" in js and "innerHTML" not in js.split("\n", 2)[2]

    test_gui.run(gui, scenario)


def test_drafts_by_email(kit, state):
    assert execute(kit, "drafts") == "No drafts waiting. New ones arrive daily."
    pid = me.add_play(state, "linkedin_post", "draft", "queued")
    state._exec("UPDATE marketing_plays SET title = 'Hiring data', body = 'The text' WHERE id = ?", (pid,))
    reply = execute(kit, "drafts")
    assert f"#{pid} LinkedIn post: Hiring data" in reply and "The text" in reply
