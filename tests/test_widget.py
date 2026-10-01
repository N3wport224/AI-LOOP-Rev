"""Phases 300-304: embeddable product cards."""

import re

from strategies import widget
from strategies.inbound_syndicator import InboundSyndicator, site_pages
from strategies.sales_channels import affiliate_link
from tests import test_business_ops
from tests.test_business_ops import ctx
from tests.test_catalog_hygiene import made_product

kit = test_business_ops.kit  # the same live-mode toolkit fixture


def test_the_script_is_small_quiet_and_text_only():
    js = widget.WIDGET_JS
    assert len(js.encode()) < widget.MAX_BYTES
    assert "innerHTML" not in js and "cookie" not in js and "localStorage" not in js and "eval(" not in js
    assert re.findall(r"fetch\(([^)]*)\)", js) == ["base+'search.json'"]  # one request, to this site


def test_affiliate_tags_match_affiliate_links(config):
    tags = dict(re.findall(r"searchParams\.set\('(utm_\w+)','(\w+)'\)", widget.WIDGET_JS))
    assert tags["utm_source"] in ("aff", "widget")
    link = affiliate_link(config, "https://x.example/p/", "abc123")
    assert "utm_source=aff" in link and "utm_medium=affiliate" in link and "utm_campaign=abc123" in link
    assert "'utm_source','aff'" in widget.WIDGET_JS and "'utm_medium','affiliate'" in widget.WIDGET_JS


def test_snippet_escapes(config):
    config.pages_base_url = "https://me.github.io/d"
    s = widget.snippet(config, 'x"><script>', ref="r1")
    assert '<div data-am-product="x&quot;&gt;&lt;script&gt;" data-am-ref="r1"></div>' in s
    assert '<script src="https://me.github.io/d/widget.js" async></script>' in s


def test_the_site_has_the_script_and_the_embed_page(kit, state, config, clock, transport):
    config.pages_base_url = "https://me.github.io/d"
    made = made_product(kit, state, clock, transport)
    assert made["slug"] in {p.slug for p in site_pages(kit)}
    InboundSyndicator().run("build_site", ctx(kit))
    assert kit.files.read_text("site/widget.js") == widget.WIDGET_JS
    page = kit.files.read_text(f"site/{widget.PAGE}")
    assert f"data-am-product=&quot;{made['slug']}&quot;" in page and "data-am-ref" in page
    assert state.get("site_audit")["broken_links"] == []


def test_the_script_is_plain_ascii():
    assert widget.WIDGET_JS.isascii()  # renders the same whatever charset the embedding page assumes
