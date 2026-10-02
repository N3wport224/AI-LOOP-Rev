"""Phases 410-416: the path to the daily goal, launch posts."""

from strategies import path_to_goal as ptg
from strategies import product_factory as pf
from tests import test_business_ops, test_gui
from tests.test_catalog_hygiene import made_product
from tools import product_media as pm

kit = test_business_ops.kit
gui = test_gui.gui


def test_a_fresh_install_stops_at_real_payments(config, state):
    config.stripe_secret_key, config.dry_run = "sk_test_x", True
    d = ptg.diagnose(state, config)
    assert d["blocker"]["name"] == "Real payments" and d["blocker"]["you"]
    lines = ptg.describe(d)
    assert lines[0].startswith("✘ Real payments") and "NEXT (only you can do this)" in lines[1] and len(lines) == 3
    assert "go-live" in ptg.headline(d)


def test_the_chain_moves_on_as_each_link_is_fixed(kit, state, config, clock, transport):
    made = made_product(kit, state, clock, transport)
    if state.get_asset(made["asset_id"])["status"] != "published":
        pf.publish(kit, made["slug"])
    d = ptg.diagnose(state, config)
    assert [s["ok"] for s in d["steps"][:2]] == [True, True] and d["blocker"]["name"] == "A place to find them"
    config.github_pages_repo, config.pages_base_url = "me/me.github.io", "https://me.github.io"
    assert ptg.diagnose(state, config)["blocker"]["name"] == "Ways in (traffic)"
    config.devto_api_key = "k"
    d = ptg.diagnose(state, config)
    assert d["blocker"]["name"] == "Visitors start a checkout" and not d["blocker"]["you"]
    state._exec("INSERT INTO checkout_sessions (session_id, product_ref, status, updated_at) VALUES ('cs1', 'p', 'open', ?)",
                (state.now(),))
    assert ptg.diagnose(state, config)["blocker"]["name"] == "Checkouts become sales"
    for i in range(30):
        state.record_revenue("stripe", f"s{i}", 1900, 85, 1815, True)
    d = ptg.diagnose(state, config)
    assert d["blocker"] is None and "On goal" in ptg.headline(d)


def test_the_arithmetic(config, state):
    config.daily_target_cents, config.stripe_fee_pct, config.stripe_fee_fixed_cents = 1000, 2.9, 30
    m = ptg.arithmetic(state, config)
    assert m["net_per_sale_cents"] == m["avg_price_cents"] - round(m["avg_price_cents"] * 0.029) - 30
    assert m["sales_per_day"] >= 1 and m["visitors_low"] == m["sales_per_day"] * 50 and m["visitors_high"] == m["sales_per_day"] * 100


def test_launch_posts_are_useful_and_have_one_link():
    f = pm.facts("Companies Hiring Rust Engineers", "", {"rows": 412, "companies": 188, "remote_pct": 64,
                 "top_companies": [["Cloudflare", 14], ["Discord", 11]], "top_locations": [["Berlin", 30]]}, 412, 1900)
    post = pm.launch_post(f, "https://me.github.io/rust/?utm_source=social")
    assert "412 current job postings from 188 companies" in post and "Cloudflare (14)" in post and "Berlin" in post
    assert post.count("https://") == 1 and post.rstrip().endswith("utm_source=social")


def test_the_panel_shows_the_path_and_launch_posts(gui, kit, state, clock, transport):
    made = made_product(kit, state, clock, transport)
    if state.get_asset(made["asset_id"])["status"] != "published":
        pf.publish(kit, made["slug"])
    asset = state.get_asset(made["asset_id"])

    async def scenario(client):
        csrf = await test_gui.login(client)
        status = await (await client.get("/api/status")).json()
        assert status["path"]["steps"][0]["name"] == "Real payments" and status["path"]["math"]["sales_per_day"] >= 1
        p = next(i for i in (await (await client.get("/api/products/all")).json())["items"] if i["id"] == asset["id"])
        assert "client_reference_id=am--social--" in p["post"] and "job postings" in p["post"]
        r = await (await client.post("/api/products/posted", json={"id": asset["id"]}, headers={"X-CSRF-Token": csrf})).json()
        assert r["ok"]
        html = await (await client.get("/")).text()
        assert 'id="path-card"' in html

    test_gui.run(gui, scenario)
    assert state._one("SELECT COUNT(*) AS n FROM marketing_plays WHERE strategy = 'launch_post' AND status = 'posted'")["n"] == 1
