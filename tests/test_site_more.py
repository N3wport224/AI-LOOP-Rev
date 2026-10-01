"""Phases 120-124: compare page, complete sitemap, llms.txt, home JSON-LD, security.txt."""

import json
import re
import xml.etree.ElementTree as ET

from tools.page_builder import ProductPage, SiteBuilder
from tools.site_audit import audit_site
from tools.site_more import home_jsonld, security_txt


class Files:
    def write_text(self, *a):
        pass

    def write_bytes(self, *a):
        pass


def page(niche, price=1900, **kw):
    return ProductPage(niche=niche, title=f"{niche} Intel", summary="Companies hiring " + niche + " engineers right now.",
                       price_cents=price, currency="usd", checkout_url=f"https://buy.stripe.com/{niche}",
                       sample_columns=["company"], sample_rows=[{"company": "A"}], metrics={"companies": 40, "roles": 90},
                       updated_at="2026-09-29T10:00:00", versions=[{"version": 2, "date": "2026-09-29", "rows": 120}], **kw)


def build(config, clock, pages=None):
    config.pages_base_url, config.sender_email = "https://me.github.io/site", "hello@me.example"
    return SiteBuilder(config, Files()).build(pages or [page("python-remote"), page("ml-ai", 2900)], [], clock())


def test_compare_page_lists_every_dataset(config, clock):
    out = build(config, clock)
    html = out["compare/index.html"]
    assert html.index("ml-ai Intel") < html.index("python-remote Intel")
    assert "<td>120</td>" in html and "$29.00" in html and 'href="../python-remote/"' in html
    assert 'href="compare/"' in out["index.html"]


def test_sitemap_has_pricing_compare_changelog_and_legal_pages(config, clock):
    out = build(config, clock)
    locs = [e.text for e in ET.fromstring(out["sitemap.xml"]).iter("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")]
    for path in ("pricing/", "compare/", "python-remote/changelog/", "legal/refunds/", "contact/"):
        assert f"https://me.github.io/site/{path}" in locs
    assert not any("/thanks/" in loc for loc in locs) and len(locs) == len(set(locs))


def test_llms_txt_and_security_txt(config, clock):
    out = build(config, clock)
    assert "- [ml-ai Intel](https://me.github.io/site/ml-ai/): $29.00." in out["llms.txt"]
    sec = out[".well-known/security.txt"]
    assert sec.startswith("Contact: mailto:hello@me.example\nExpires: 2027-09-30T12:00:00Z")
    assert ".nojekyll" in out
    assert security_txt("", "", clock()) == ""


def test_home_has_organization_and_website_data(config, clock):
    out = build(config, clock)
    data = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', out["index.html"]).group(1))
    assert [g["@type"] for g in data["@graph"]] == ["Organization", "WebSite"]
    assert "</script>" not in home_jsonld("", "A </script> B")[40:-9]


def test_the_new_pages_pass_the_site_audit(config, clock):
    report = audit_site(build(config, clock), "https://me.github.io/site")
    assert report["broken_links"] == [] and report["a11y"] == []
    assert not [s for s in report["seo"] if s.startswith("compare/")]
