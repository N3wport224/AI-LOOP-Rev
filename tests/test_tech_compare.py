"""Phases 330-334: technology comparison pages."""

import json
import re

from strategies import tech_compare as tc
from strategies.inbound_syndicator import InboundSyndicator
from tests import test_business_ops
from tests.test_business_ops import ctx
from tests.test_catalog_hygiene import made_product
from tests.test_product_factory import postings

kit = test_business_ops.kit  # the same live-mode toolkit fixture
SHELL_AT = lambda title, body, desc, depth: body  # noqa: E731


def market(state, clock):
    postings(state, clock, 40, ["rust"], prefix="r")
    postings(state, clock, 32, ["golang"], remote=True, location="Remote", prefix="g")
    postings(state, clock, 10, ["rust", "golang"], prefix="both")
    postings(state, clock, 50, ["php"], prefix="p", days_ago=45)  # too old to count
    postings(state, clock, 12, ["elixir"], prefix="e")             # too few


def test_pairs_and_numbers(state, clock):
    market(state, clock)
    items = tc.pairs(state)
    assert [(p["a"], p["b"]) for p in items] == [("golang", "rust")]
    p = items[0]
    assert p["sa"]["postings"] == 42 and p["sb"]["postings"] == 50 and p["both"] == 10
    assert p["sa"]["remote_pct"] == 76 and p["sb"]["countries"] == ["Germany"] and p["sb"]["median_salary"] is None


def test_page_faq_and_week_on_week(state, clock):
    market(state, clock)
    files = tc.site_files(state, [], SHELL_AT)
    page = files["vs/golang-vs-rust/index.html"]
    assert "<h1>Go vs Rust: who's hiring</h1>" in page and "10 postings ask for both" in page
    ld = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', page, re.S).group(1))
    assert ld["@type"] == "FAQPage" and ld["mainEntity"][0]["acceptedAnswer"]["text"] == \
        "Rust: 50 postings in the last 30 days against 42 for Go."
    assert 'href="golang-vs-rust/"' in files["vs/index.html"]
    clock.advance(days=7)
    postings(state, clock, 6, ["rust"], prefix="more")
    page = tc.site_files(state, [], SHELL_AT)["vs/golang-vs-rust/index.html"]
    assert "<td>56 (+6 vs last week)</td>" in page


def test_on_the_site(kit, state, config, clock, transport):
    config.pages_base_url = "https://me.github.io/d"
    made_product(kit, state, clock, transport)  # rust postings and a rust product
    postings(state, clock, 32, ["golang"], prefix="g")
    InboundSyndicator().run("build_site", ctx(kit))
    page = kit.files.read_text("site/vs/golang-vs-rust/index.html")
    assert 'href="../../hiring-rust/"' in page  # links the Rust dataset
    assert "/d/vs/golang-vs-rust/" in kit.files.read_text("site/sitemap.xml")
    assert '<a href="vs/">Compare technologies</a>' in kit.files.read_text("site/index.html")
    assert state.get("site_audit")["broken_links"] == []
