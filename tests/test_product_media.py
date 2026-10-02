"""Phases 401-405: checkout descriptions and three preview images per product."""

import urllib.parse

from strategies import product_factory as pf
from strategies.inbound_syndicator import InboundSyndicator
from tests import test_business_ops
from tests.test_business_ops import STRIPE, ctx
from tests.test_catalog_hygiene import made_product
from tools import product_media as pm

kit = test_business_ops.kit  # the same live-mode toolkit fixture
INSIGHT = {"rows": 412, "companies": 188, "remote_pct": 64,
           "top_companies": [["Cloudflare", 14], ["Discord", 11], ["1Password", 9]], "top_locations": [["Remote", 260]]}
ROWS = [{"company": "Acme <b>", "title": "Senior Rust Engineer", "location": "Remote", "remote": True, "stack": ["Rust"]}] * 5


def test_the_checkout_description_states_the_facts_once_and_fits_stripe():
    f = pm.facts("Rust jobs", "412 current job postings from 188 companies: roles mentioning Rust. CSV, Excel, JSON and SQL, "
                 "delivered instantly.", INSIGHT, 412, 1900)
    text = pm.checkout_description(f, "2026-10-02T00:00:00", 14)
    assert len(text) <= 500 and text.count("412") == 1 and text.count("CSV") == 1
    assert "Hiring most: Cloudflare, Discord, 1Password." in text and "64% remote" in text and "14-day refund" in text
    bare = pm.checkout_description(pm.facts("X", "", {"rows": 30, "companies": 9}, 30, 900))
    assert bare.startswith("30 current job postings from 9 companies.") and "CSV, Excel, JSON and SQL" in bare
    assert len(pm.checkout_description(pm.facts("X", "word " * 400, INSIGHT, 412, 900))) <= 500


def test_three_previews_are_drawn_safely():
    out = pm.previews(pm.facts("A <very> long title " * 4, "", INSIGHT, 412, 1900), ["company", "title", "remote"], ROWS)
    assert {"preview-1.svg", "preview-2.svg", "preview-3.svg"} <= set(out)
    assert b"<very>" not in out["preview-1.svg"] and b"Acme &lt;b&gt;" in out["preview-2.svg"]  # escaped
    assert b"Yes" in out["preview-2.svg"] and b"Cloudflare" in out["preview-3.svg"]
    plain = pm.previews(pm.facts("Plain", "", {"rows": 5}, 5, 900), ["company", "title"], ROWS, png=False)
    assert set(plain) == {"preview-1.svg", "preview-2.svg", "preview-3.svg"} and b"every row" in plain["preview-3.svg"]


def test_product_pages_show_the_gallery_and_use_it_for_social_previews(kit, state, config, clock, transport):
    config.pages_base_url = "https://me.github.io/d"
    made = made_product(kit, state, clock, transport)
    InboundSyndicator().run("build_site", ctx(kit))
    page = kit.files.read_text(f"site/{made['slug']}/index.html")
    names = pm.gallery_names({p.name: b"" for p in kit.files.resolve(f"site/{made['slug']}").iterdir()})
    assert len(names) == 3 and 'class="gallery"' in page
    for n in names:
        assert f'src="{n}"' in page and kit.files.exists(f"site/{made['slug']}/{n}")
    if names[0].endswith(".png"):
        assert f'property="og:image" content="https://me.github.io/d/{made["slug"]}/preview-1.png"' in page
    base = f"assets/{made['slug']}/{pf.KIND}-v1"
    assert kit.files.exists(f"{base}/preview-1.svg")  # drawn once, next to the product's files


def test_previews_are_drawn_a_few_per_build(kit, state, config, clock, transport):
    budget = [1]
    f = pm.facts("T", "", INSIGHT, 412, 900)
    assert pm.ensure(kit.files, "assets/a/x-v1", f, ["company"], ROWS, png=False, budget=budget)
    assert pm.ensure(kit.files, "assets/b/x-v1", f, ["company"], ROWS, png=False, budget=budget) == {}  # next build
    assert pm.ensure(kit.files, "assets/a/x-v1", f, ["company"], ROWS, png=False, budget=budget)  # already drawn: free
    kit.files.write_json("assets/a/x-v1/preview.json", {"design": pm.DESIGN - 1})  # drawn with an older design
    old = pm.ensure(kit.files, "assets/a/x-v1", f, ["company"], ROWS, png=False, budget=[0])
    assert old  # out of budget: the old pictures stay up meanwhile
    pm.ensure(kit.files, "assets/a/x-v1", f, ["company"], ROWS, png=False, budget=[1])
    assert kit.files.read_json("assets/a/x-v1/preview.json") == {"design": pm.DESIGN}


def test_stripe_gets_the_images_once_they_are_online(kit, state, config, clock, transport):
    config.pages_base_url, config.github_pages_repo, config.github_pages_branch, config.github_pages_dir = \
        "https://me.github.io/d", "me/d", "gh-pages", ""
    made = made_product(kit, state, clock, transport)
    asset = state.get_asset(made["asset_id"])
    if asset["status"] != "published":
        pf.publish(kit, made["slug"])
        asset = state.get_asset(made["asset_id"])
    ref = asset["product_ref"]
    transport.add_json(f"{STRIPE}/payment_links/{ref}/line_items", {"data": [{"price": {"product": "prod_media"}}]})
    transport.add_json(f"{STRIPE}/products/prod_media", {"id": "prod_media"})
    InboundSyndicator().run("build_site", ctx(kit))
    assert pm.sync_stripe(kit) == 0  # not on the site yet: no broken images on the checkout
    names = [f"{made['slug']}/{n}.png" for n in pm.NAMES]
    state.set("site_manifest:me/d:gh-pages:", {n: "h" for n in names})
    base = f"assets/{made['slug']}/{pf.KIND}-v1"
    if not kit.files.exists(f"{base}/preview-1.png"):
        return  # Pillow isn't installed here: the site still has SVG previews; Stripe needs PNG
    assert pm.sync_stripe(kit) == 1
    call = transport.calls_to(f"{STRIPE}/products/prod_media", "POST")[-1]
    form = dict(urllib.parse.parse_qsl(call["body"].decode()))
    assert form["images[0]"] == f"https://me.github.io/d/{made['slug']}/preview-1.png" and "images[2]" in form
    assert len(form["description"]) <= 500 and "public posting" in form["description"]
    assert pm.sync_stripe(kit) == 0  # once


def test_new_checkouts_get_the_written_description(kit, state, clock, transport):
    made = made_product(kit, state, clock, transport)
    if state.get_asset(made["asset_id"])["status"] != "published":
        pf.publish(kit, made["slug"])
    call = transport.calls_to(f"{STRIPE}/products", "POST")[-1]
    desc = dict(urllib.parse.parse_qsl(call["body"].decode()))["description"]
    assert "public posting" in desc and len(desc) <= 500


# ------------------------------------------------------------------ Phases 406-409: made with pictures; the panel
from tests import test_gui  # noqa: E402

gui = test_gui.gui  # the control-panel fixture


def test_a_new_product_has_its_pictures_right_away(kit, state, clock, transport):
    made = made_product(kit, state, clock, transport)
    base = f"assets/{made['slug']}/{pf.KIND}-v1"
    assert all(kit.files.exists(f"{base}/{n}.svg") for n in pm.NAMES)
    assert b"POSTINGS BY TECHNOLOGY" in kit.files.read_bytes(f"{base}/preview-1.svg")  # what kind of product it is


def test_the_panel_lists_every_product_with_pictures_and_stripe_links(gui, kit, state, config, clock, transport):
    made = made_product(kit, state, clock, transport)
    if state.get_asset(made["asset_id"])["status"] != "published":
        pf.publish(kit, made["slug"])
    asset = state.get_asset(made["asset_id"])
    gui.env_file.write_text(gui.env_file.read_text() + "AUTOMONETIZE_PAGES_BASE_URL=https://me.github.io/d\n")

    async def scenario(client):
        csrf = await test_gui.login(client)
        d = await (await client.get("/api/products/all")).json()
        p = next(i for i in d["items"] if i["id"] == asset["id"])
        assert p["status"] == "live" and p["type"] == "Postings by technology" and p["previews"] == [1, 2, 3]
        assert p["checkout_url"] == asset["checkout_url"] and "public posting" in p["description"]
        # the panel's own settings hold a test key, so the link opens Stripe's test-mode dashboard
        assert p["stripe_url"] == f"https://dashboard.stripe.com/test/payment-links/{asset['product_ref']}"
        assert p["page_url"] == f"https://me.github.io/d/{made['slug']}/" and d["counts"]["live"] >= 1
        img = await client.get(f"/api/products/preview?id={asset['id']}&n=2")
        assert img.status == 200 and img.content_type in ("image/png", "image/svg+xml")
        assert (await client.get(f"/api/products/preview?id={asset['id']}&n=7")).status == 404
        assert (await client.get("/api/products/preview?id=x&n=1")).status == 404
        assert (await (await client.get("/api/products/all?q=zzz-nothing")).json())["total"] == 0
        r = await (await client.post("/api/products/redraw", json={"id": asset["id"]}, headers={"X-CSRF-Token": csrf})).json()
        assert r["ok"] and "Drew 3" in r["message"]
        bad = await client.post("/api/products/redraw", json={"id": 999999}, headers={"X-CSRF-Token": csrf})
        assert bad.status == 400
        html = await (await client.get("/")).text()
        assert 'id="gallery"' in html and 'id="gallery-q"' in html

    test_gui.run(gui, scenario)


def test_company_level_products_dont_claim_a_posting_per_row():
    f = pm.facts("AWS Stack Map", "10 companies hiring for AWS.", {"rows": 10, "companies": 10}, 10, 900,
                 label="Stack maps", postings=pm.per_posting("micro", "stack"))
    text = pm.checkout_description(f)
    assert "links to its public posting" not in text and "Built from public job postings" in text
    cover = pm.previews(f, ["company"], ROWS, png=False)["preview-1.svg"]
    assert b">rows<" in cover and b">postings<" not in cover and b"STACK MAPS" in cover
    assert pm.per_posting("micro", "slice") and pm.per_posting("lead_directory") and not pm.per_posting("micro", "top")
