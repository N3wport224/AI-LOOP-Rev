import base64
import json
import xml.etree.ElementTree as ET

import pytest

from strategies.inbound_syndicator import site_pages
from tests.conftest import NOW
from tests.test_distribution import pipeline
from tests.test_page_builder import page, parse
from tools.http_client import Response
from tools.page_builder import SiteBuilder, render_product_page, render_sitemap
from tools.seo_assets import badge_for, render_badge_svg, render_og_png, render_og_svg

SVG = "{http://www.w3.org/2000/svg}"


def test_badge_is_valid_svg_with_metric():
    svg = render_badge_svg("cloud radar", "142 Active Cloud Hiring Signals Tracked")
    root = ET.fromstring(svg)
    assert root.tag == f"{SVG}svg" and root.get("height") == "20"
    texts = [t.text for t in root.iter(f"{SVG}text")]
    assert "142 Active Cloud Hiring Signals Tracked" in texts and "cloud radar" in texts
    assert int(root.get("width")) > 150
    assert root.get("aria-label") == "cloud radar: 142 Active Cloud Hiring Signals Tracked"


def test_badge_escapes_and_colours():
    ET.fromstring(render_badge_svg("a & <b>", "x\"y"))
    assert 'fill="#2e7d32"' in badge_for("x", {"roles": 142}) and "142 hiring signals" in badge_for("x", {"roles": 142})
    assert 'fill="#9e9e9e"' in badge_for("x", {"roles": 0})


def test_og_card_svg_and_png():
    metrics = {"companies": 42, "roles": 142, "high_urgency": 7, "signals": [("migration", 5)]}
    root = ET.fromstring(render_og_svg("Python Remote Tech Stack Intel", metrics, "$9.00"))
    assert (root.get("width"), root.get("height")) == ("1200", "630")
    assert any("142 hiring signals" in (t.text or "") for t in root.iter(f"{SVG}text"))
    pytest.importorskip("PIL")
    png = render_og_png("Python Remote Tech Stack Intel", metrics, "$9.00")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    from PIL import Image
    import io

    assert Image.open(io.BytesIO(png)).size == (1200, 630)


def test_og_png_is_optional(monkeypatch):
    import builtins

    real = builtins.__import__

    def no_pil(name, *a, **k):
        if name.startswith("PIL"):
            raise ImportError("no PIL")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_pil)
    assert render_og_png("t", {}, "$9") is None


def test_page_meta_offers_proof_and_attribution():
    p = page(subscription_url="https://buy.stripe.com/sub", subscription_price_cents=1000, subscription_interval="month",
             data_updated_at="2026-09-30T10:00:00+00:00", profiles_added_7d=14, verified_profiles=30, purchases_7d=0)
    text = render_product_page(p, "https://me.github.io")
    parsed = parse(text)
    assert parsed.meta["og:image"] == "https://me.github.io/python-remote/og.png"
    assert parsed.meta["twitter:card"] == "summary_large_image"
    data = json.loads(parsed.jsonld[0])
    one_off, sub = data["offers"]
    assert one_off["price"] == "9.00" and sub["price"] == "10.00"
    assert sub["priceSpecification"]["@type"] == "UnitPriceSpecification" and sub["priceSpecification"]["billingDuration"] == "P1M"
    assert data["image"] == "https://me.github.io/python-remote/og.png"
    assert '<time class="ago" datetime="2026-09-30T10:00:00+00:00">' in text
    assert "14 company profiles added this week" in text and "30 verified careers pages" in text
    assert "purchase" not in text.split('class="proof"')[1].split("</p>")[0]  # zero purchases: not shown
    assert text.count("data-checkout href") == 2 and "$10.00/month" in text
    assert "client_reference_id" in text and "time.ago" in text  # attribution + relative-time scripts
    assert '<img src="radar-badge.svg"' in text


def test_no_proof_line_without_data():
    assert 'class="proof"' not in render_product_page(page(), "")


def test_sitemap_robots_and_binary_assets_published_to_docs(config, toolkit, transport):
    pytest.importorskip("PIL")
    config.github_token, config.github_pages_repo, config.pages_base_url = "ghp", "me/site", "https://me.github.io/site"
    toolkit.github.token = "ghp"
    transport.add("https://api.github.com/repos/me/site/contents/", [Response(404, "u"), Response(201, "u", b"{}")])
    b = SiteBuilder(config, toolkit.files, toolkit.github)
    out = b.build([page()], [], NOW)
    assert isinstance(out["python-remote/og.png"], bytes)
    assert out["robots.txt"] == "User-agent: *\nAllow: /\nSitemap: https://me.github.io/site/sitemap.xml\n"
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = [e.text for e in ET.fromstring(out["sitemap.xml"]).findall("s:url/s:loc", ns)]
    assert locs[:2] == ["https://me.github.io/site/", "https://me.github.io/site/python-remote/"]
    assert "https://me.github.io/site/pricing/" in locs and len(locs) == len(set(locs))
    b.publish(out)
    puts = {c["url"].split("/contents/")[1]: json.loads(c["body"]) for c in transport.calls_to("https://api.github.com/repos/me/site/contents/", "PUT")}
    assert {"docs/robots.txt", "docs/sitemap.xml", "docs/python-remote/og.png", "docs/python-remote/radar-badge.svg"} <= set(puts)
    assert base64.b64decode(puts["docs/python-remote/og.png"]["content"]) == out["python-remote/og.png"]
    assert toolkit.files.read_bytes("site/python-remote/og.png")[:4] == b"\x89PNG"


def test_sitemap_lastmod_dates():
    root = ET.fromstring(render_sitemap([page()], "https://x"))
    lastmod = root.find("{http://www.sitemaps.org/schemas/sitemap/0.9}url/{http://www.sitemaps.org/schemas/sitemap/0.9}lastmod")
    assert lastmod is None or len(lastmod.text) == 10
    assert root.findall(".//{http://www.sitemaps.org/schemas/sitemap/0.9}lastmod")[0].text == "2026-09-30"


def test_site_pages_use_real_social_proof_and_attach_subscription(toolkit, state, make_hypothesis, clock):
    hyp = make_hypothesis()
    pipeline(toolkit, hyp)  # 4 companies, first seen now
    ds = state.latest_asset(hyp["id"], "lead_directory")
    state.update_asset(ds["id"], checkout_url="https://buy.stripe.com/one")
    sub = state.add_asset(hyp["id"], "subscription", "Sub", ds["path"], 1, 4, 1000, product_ref="plink_sub")
    state.update_asset(sub, niche="python-remote", checkout_url="https://buy.stripe.com/sub", kind_meta='{"interval": "month"}')
    state.record_order("stripe", "cs_1", "a@b.com", 900, "plink_1", ds["id"], hyp["id"], clock().isoformat())
    pages = site_pages(toolkit)
    assert [p.kind for p in pages] == ["dataset"]  # the subscription isn't a page of its own
    p = pages[0]
    assert (p.profiles_added_7d, p.purchases_7d, p.subscription_url, p.subscription_price_cents) == (4, 1, "https://buy.stripe.com/sub", 1000)
    assert p.data_updated_at.startswith("2026-09-30")
    clock.advance(days=8)
    assert site_pages(toolkit)[0].profiles_added_7d == 0 and site_pages(toolkit)[0].purchases_7d == 0
