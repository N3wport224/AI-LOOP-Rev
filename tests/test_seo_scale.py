"""Phases 175-179: unique product content, technology hubs, answer pages, breadcrumbs, Dataset markup."""

import json
import re

from strategies import product_factory as pf
from strategies.inbound_syndicator import seo_extra, site_pages
from tests import test_business_ops
from tests.test_product_factory import postings, unique_links
from tools.page_builder import SiteBuilder
from tools.site_audit import audit_site

kit = test_business_ops.kit  # the same live-mode toolkit fixture


class Files:
    def write_text(self, *a):
        pass

    def write_bytes(self, *a):
        pass


def jsonld(html):
    return [json.loads(m) for m in re.findall(r'<script type="application/ld\+json">\s*(.*?)\s*</script>', html, re.S)]


def built(kit, state, config, clock, transport):
    config.pages_base_url = "https://me.github.io/site"
    unique_links(transport)
    postings(state, clock, 30, ["rust"], location="Berlin, Germany")
    postings(state, clock, 30, ["rust"], location="Austin, TX, United States", prefix="us")
    for _ in range(3):
        pf.tick(kit, force=True)
    pages = site_pages(kit)
    extra = seo_extra(kit, pages, [])
    out = SiteBuilder(config, Files()).build(pages, [], clock(), extra=extra)
    return pages, out


def test_product_pages_carry_their_own_numbers_and_markup(kit, state, config, clock, transport):
    pages, out = built(kit, state, config, clock, transport)
    micro = next(p for p in pages if p.slug == "hiring-rust")
    html = out["hiring-rust/index.html"]
    assert "In this dataset" in html and "60 job postings from 24 companies" in html and "Most open roles:" in html
    types = {d["@type"] for d in jsonld(html)}
    assert {"Product", "Dataset", "BreadcrumbList"} <= types
    crumbs = next(d for d in jsonld(html) if d["@type"] == "BreadcrumbList")
    assert [i["item"] for i in crumbs["itemListElement"]] == ["https://me.github.io/site/", "https://me.github.io/site/hiring/rust/",
                                                              "https://me.github.io/site/hiring-rust/"]
    assert 'href="../hiring/rust/">More Rust datasets</a>' in html and micro.insight_html


def test_hubs_and_answer_pages(kit, state, config, clock, transport):
    _, out = built(kit, state, config, clock, transport)
    hub = out["hiring/rust/index.html"]
    assert "Rust hiring datasets" in hub and hub.count('href="../../hiring-rust') >= 2
    assert 'href="rust/"' in out["hiring/index.html"] and 'href="hiring/">By technology</a>' in out["index.html"]
    answer = out["answers/how-many-companies-are-hiring-rust-engineers/index.html"]
    faq = next(d for d in jsonld(answer) if d["@type"] == "FAQPage")
    assert "24 companies have 60 open roles mentioning Rust" in faq["mainEntity"][0]["acceptedAnswer"]["text"]


def test_the_seo_pages_pass_the_site_audit_and_are_in_the_sitemap(kit, state, config, clock, transport):
    _, out = built(kit, state, config, clock, transport)
    report = audit_site(out, config.pages_base_url)
    assert report["broken_links"] == [] and report["a11y"] == []
    assert not [s for s in report["seo"] if s.startswith(("hiring/", "answers/"))]
    for path in ("hiring/", "hiring/rust/", "answers/how-many-companies-are-hiring-rust-engineers/"):
        assert f"https://me.github.io/site/{path}" in out["sitemap.xml"]
