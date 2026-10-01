"""Phases 310-314: smaller pages, page budget, preconnect, split sitemaps, site size."""

import xml.etree.ElementTree as ET

from tools import site_perf as sp
from tests.test_ops_robustness import alerts


def test_minify_keeps_whitespace_where_it_matters():
    page = ("<html>\n  <body>\n\n    <p>Hello\n      world</p>\n    <pre>  a\n\n  b</pre>\n"
            "    <textarea>\n  x\n</textarea>\n<script>\n  if (a)\n\n    b();\n</script>\n</body>")
    out = sp.minify(page)
    assert out == ("<html>\n<body>\n<p>Hello\nworld</p>\n<pre>  a\n\n  b</pre>\n<textarea>\n  x\n</textarea>\n"
                   "<script>\n  if (a)\n\n    b();\n</script>\n</body>")
    assert len(out) < len(page)


def test_preconnect_only_with_a_checkout():
    page = '<html><head><title>x</title></head><body><a href="https://buy.stripe.com/abc">Buy</a></body></html>'
    once = sp.preconnect(page)
    assert once.startswith("<html><head>" + sp.PRECONNECT) and sp.preconnect(once) == once
    assert sp.preconnect("<html><head></head><body>no checkout</body></html>").count("preconnect") == 0


def sitemap(n):
    urls = "".join(f"<url><loc>https://me.github.io/p{i}/</loc></url>" for i in range(n))
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="{sp.NS}">{urls}</urlset>\n'


def test_big_sitemaps_are_split(monkeypatch):
    out = {"sitemap.xml": sitemap(10)}
    assert sp.split_sitemap(out, "https://me.github.io") == 1 and list(out) == ["sitemap.xml"]
    monkeypatch.setattr(sp, "MAX_URLS", 4)
    assert sp.split_sitemap(out, "https://me.github.io") == 3
    index = ET.fromstring(out["sitemap.xml"].split("\n", 1)[1])
    assert index.tag == f"{{{sp.NS}}}sitemapindex"
    assert [e.text for e in index.iter(f"{{{sp.NS}}}loc")] == [f"https://me.github.io/sitemap-{n}.xml" for n in (1, 2, 3)]
    third = ET.fromstring(out["sitemap-3.xml"].split("\n", 1)[1])
    assert [e.text for e in third.iter(f"{{{sp.NS}}}loc")] == ["https://me.github.io/p8/", "https://me.github.io/p9/"]


def test_budgets_and_alerts(state, monkeypatch):
    monkeypatch.setattr(sp, "PAGE_BUDGET_KB", 1)
    out = {"big/index.html": "<p>" + "x" * 3000 + "</p>", "index.html": "<p>ok</p>", "sitemap.xml": sitemap(2)}
    report = sp.optimise(out, "https://me.github.io")
    assert report["over_budget"] == [("big/index.html", 2)] and report["files"] == 3
    notes = sp.check(state, report)
    assert notes[0].startswith("1 page(s) over 1 KB, e.g. big/index.html")
    sp.check(state, report)
    assert sum("Website: 1 page(s) over" in a for a in alerts(state)) == 1  # once a day
    assert state.get("site_size")["files"] == 3


def test_the_built_site_is_minified_and_still_clean(kit_site):
    state, files = kit_site
    home = files.read_text("site/index.html")
    assert "\n  " not in home.split("<script")[0]  # no indentation outside scripts
    assert state.get("site_audit")["broken_links"] == [] and state.get("site_size")["saved_kb"] >= 0


import pytest  # noqa: E402

from tests import test_business_ops  # noqa: E402

kit = test_business_ops.kit


@pytest.fixture
def kit_site(kit, state, config, clock, transport):
    from strategies.inbound_syndicator import InboundSyndicator
    from tests.test_business_ops import ctx
    from tests.test_catalog_hygiene import made_product

    config.pages_base_url = "https://me.github.io/d"
    made_product(kit, state, clock, transport)
    InboundSyndicator().run("build_site", ctx(kit))
    return state, kit.files
