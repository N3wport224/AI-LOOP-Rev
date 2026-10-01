"""Phases 80-84: legal pages, sale banner, FAQ, search verification, 404 page."""

import json
import re

from strategies.inbound_syndicator import InboundSyndicator
from tests import test_business_ops
from tests.test_business_ops import ctx, dataset
from tools.page_builder import ProductPage, render_product_page
from tools.site_extras import faq, faq_html

kit = test_business_ops.kit  # the same live-mode toolkit fixture


def build(kit, state, config):
    config.pages_base_url = "https://me.github.io/d"
    _, aid = dataset(kit, state, "python-remote")
    state.update_asset(aid, niche="python-remote")
    InboundSyndicator().run("build_site", ctx(kit))
    return {p.relative_to(kit.files.resolve("site")).as_posix(): p.read_text() for p in kit.files.resolve("site").rglob("*.html")}


def test_legal_pages_are_built_from_settings_and_linked_everywhere(kit, state, config):
    config.refund_policy_days = 30
    site = build(kit, state, config)
    for rel in ("legal/terms/index.html", "legal/privacy/index.html", "legal/refunds/index.html", "contact/index.html"):
        assert rel in site
    assert "<b>30 days</b>" in site["legal/refunds/index.html"] and "1 Main St" in site["contact/index.html"]
    assert "no cookies" in site["legal/privacy/index.html"]
    for rel in ("index.html", "python-remote/index.html", "pricing/index.html"):
        assert "Terms of sale" in site[rel] and "Refunds" in site[rel], rel
    assert state.get("site_audit")["broken_links"] == []  # every new link resolves


def test_sale_banner_and_faq_on_the_product_page(kit, state, config):
    from datetime import timedelta

    state.set("sale", {"code": "SALESEP26", "percent_off": 25, "expires_at": int((state.clock() + timedelta(days=2)).timestamp()),
                       "urls": []})
    site = build(kit, state, config)
    page = site["python-remote/index.html"]
    assert "25% off with code SALESEP26" in page and "25% off with code SALESEP26" in site["pricing/index.html"]
    assert "<h2>Questions</h2>" in page and "within 14 days" in page
    ld = [json.loads(m) for m in re.findall(r'<script type="application/ld\+json">(.*?)</script>', page, re.S)]
    assert any(d.get("@type") == "FAQPage" for d in ld)


def test_faq_mentions_only_what_exists():
    page = ProductPage(niche="py", title="Py", summary="s", price_cents=1900, currency="usd", checkout_url="https://x",
                       sample_columns=[], sample_rows=[], updated_at="2026-09-30T00:00:00")

    class Cfg:
        refund_policy_days = 14

    qs = [q for q, _ in faq(Cfg(), page)]
    assert "Can my team use it?" not in qs and "Can I get updates?" not in qs
    page.team_url, page.team_seats, page.subscription_url = "https://t", 10, "https://s"
    qs = [q for q, _ in faq(Cfg(), page)]
    assert "Can my team use it?" in qs and "Can I get updates?" in qs
    assert "</script" not in faq_html([("q", "a</script><script>x")]).split("<script", 1)[1].split("</script>")[0]


def test_search_verification_tags_and_404(kit, state, config):
    config.google_site_verification, config.bing_site_verification = "g-code", "b-code"
    site = build(kit, state, config)
    assert '<meta name="google-site-verification" content="g-code">' in site["index.html"]
    assert '<meta name="msvalidate.01" content="b-code">' in site["index.html"]
    notfound = site["404.html"]
    assert "https://me.github.io/d/python-remote/" in notfound and 'content="noindex"' in notfound


def test_banner_is_escaped():
    page = ProductPage(niche="py", title="Py", summary="s", price_cents=1900, currency="usd", checkout_url="https://x",
                       sample_columns=[], sample_rows=[], banner="<b>sale</b>")
    assert "&lt;b&gt;sale&lt;/b&gt;" in render_product_page(page)
