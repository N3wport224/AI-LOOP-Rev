import json
import re
import xml.etree.ElementTree as ET
from datetime import timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser


from strategies.base import TaskContext
from strategies.inbound_syndicator import InboundSyndicator
from tests.conftest import NOW
from tests.test_distribution import pipeline
from tools.http_client import Response
from tools.page_builder import (
    FeedItem, ProductPage, SiteBuilder, product_jsonld, render_product_page, render_rss, render_sitemap,
)
from tools.syndicator import (
    DevToPublisher, GitHubDiscussionsPublisher, HashnodePublisher, Syndicator, build_article,
)

RSS_NS = {"atom": "http://www.w3.org/2005/Atom", "content": "http://purl.org/rss/1.0/modules/content/"}


def page(**kw):
    base = dict(
        niche="python-remote", title="Python Remote Tech Stack Intel", summary="42 companies & counting",
        price_cents=900, currency="usd", checkout_url="https://buy.stripe.com/t",
        sample_columns=["company", "stack", "urgency_score"],
        sample_rows=[{"company": "Acme <Labs>", "stack": ["Python", "AWS"], "urgency_score": 80}],
        metrics={"companies": 42, "high_urgency": 7, "roles": 90, "verified_urls": 30, "signals": [("migration", 5)]},
        updated_at="2026-09-30T12:00:00+00:00", sku="asset-1",
    )
    base.update(kw)
    return ProductPage(**base)


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.jsonld, self._in = [], False
        self.meta, self.links = {}, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script" and a.get("type") == "application/ld+json":
            self._in = True
            self.jsonld.append("")
        if tag == "meta":
            self.meta[a.get("name") or a.get("property")] = a.get("content")
        if tag == "link":
            self.links.append(a)

    def handle_endtag(self, tag):
        if tag == "script":
            self._in = False

    def handle_data(self, data):
        if self._in:
            self.jsonld[-1] += data


def parse(html_text):
    p = Scripts()
    p.feed(html_text)
    return p


# ------------------------------------------------------------------ product pages
def test_product_page_jsonld_is_valid_schema_org_product():
    p = parse(render_product_page(page(), "https://me.github.io/intel"))
    assert len(p.jsonld) == 1
    data = json.loads(p.jsonld[0])
    assert data["@context"] == "https://schema.org" and data["@type"] == "Product"
    assert data["name"] == "Python Remote Tech Stack Intel" and data["sku"] == "asset-1"
    offer = data["offers"]
    assert offer["@type"] == "Offer" and offer["price"] == "9.00" and offer["priceCurrency"] == "USD"
    assert offer["availability"] == "https://schema.org/InStock" and offer["url"] == "https://buy.stripe.com/t"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", offer["priceValidUntil"])
    assert data["url"] == "https://me.github.io/intel/python-remote/"


def test_product_page_seo_tags_and_content():
    text = render_product_page(page(), "https://me.github.io/intel")
    p = parse(text)
    assert p.meta["description"].startswith("42 companies")
    assert p.meta["og:title"] == "Python Remote Tech Stack Intel" and p.meta["product:price:amount"] == "9.00"
    assert {"rel": "canonical", "href": "https://me.github.io/intel/python-remote/"} in p.links
    assert any(l.get("type") == "application/rss+xml" for l in p.links)
    assert "Acme &lt;Labs&gt;" in text and "<Labs>" not in text
    assert '<b>7</b>High-urgency' in text and "migration (5)" in text
    assert 'href="https://buy.stripe.com/t"' in text


def test_script_injection_cannot_break_out_of_jsonld():
    evil = page(title='x</script><script>alert(1)</script>', summary="</script>")
    text = render_product_page(evil, "")
    p = parse(text)
    assert len(p.jsonld) == 1
    assert json.loads(p.jsonld[0])["name"] == 'x</script><script>alert(1)</script>'
    assert "<script>alert(1)" not in text


def test_preorder_without_checkout():
    data = product_jsonld(page(checkout_url=""), "rel/", "Brand")
    assert data["offers"]["availability"] == "https://schema.org/PreOrder" and "url" not in data
    assert "Checkout opens soon" in render_product_page(page(checkout_url=""))


# ------------------------------------------------------------------ sitemap & feed
def test_sitemap_is_valid_xml():
    root = ET.fromstring(render_sitemap([page(), page(niche="ai-infra", kind="premium")], "https://me.github.io/intel"))
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = [e.text for e in root.findall("s:url/s:loc", ns)]
    assert locs == ["https://me.github.io/intel/", "https://me.github.io/intel/python-remote/", "https://me.github.io/intel/ai-infra-premium/"]
    assert root.find("s:url/s:lastmod", ns) is not None


def test_rss_feed_is_compliant_rss2():
    items = [
        FeedItem("Radar & <friends>", "https://x/a/", "desc < 1", "g1", "2026-09-23T12:00:00+00:00", "<p>body</p>"),
        FeedItem("Newer", "https://x/b/", "d", "g2", "2026-09-30T12:00:00+00:00"),
    ]
    xml = render_rss(items, "https://me.github.io/intel", "Tech Stack Intel", NOW)
    assert xml.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    root = ET.fromstring(xml)
    assert root.tag == "rss" and root.get("version") == "2.0"
    ch = root.find("channel")
    for req in ("title", "link", "description"):
        assert ch.find(req).text
    assert ch.find("atom:link", RSS_NS).get("rel") == "self"
    assert ch.find("atom:link", RSS_NS).get("href") == "https://me.github.io/intel/feeds/radar.xml"
    got = ch.findall("item")
    assert [i.find("guid").text for i in got] == ["g2", "g1"]  # newest first
    assert got[1].find("title").text == "Radar & <friends>"
    assert got[1].find("content:encoded", RSS_NS).text == "<p>body</p>"
    for i in got:
        assert parsedate_to_datetime(i.find("pubDate").text).tzinfo is not None
    parsedate_to_datetime(ch.find("lastBuildDate").text)


def test_site_builder_writes_and_publishes_to_pages_branch(config, toolkit, transport):
    config.github_token, config.github_pages_repo, config.pages_base_url = "ghp", "me/me.github.io", "https://me.github.io"
    config.github_pages_branch, config.github_pages_dir = "gh-pages", ""
    toolkit.github.token = "ghp"
    transport.add("https://api.github.com/repos/me/me.github.io/contents/", [Response(404, "u"), Response(201, "u", b"{}")])
    b = SiteBuilder(config, toolkit.files, toolkit.github)
    out = b.build([page()], [], NOW)
    config.og_images = False  # PNG cards are covered in test_seo_assets
    out = b.build([page()], [], NOW)
    assert set(out) == {"python-remote/index.html", "index.html", "thanks/index.html", "pricing/index.html", "sitemap.xml",
                        "robots.txt", "feeds/radar.xml", "intel/index.html", "404.html", "contact/index.html",
                        "legal/terms/index.html", "legal/privacy/index.html", "legal/refunds/index.html",
                        "python-remote/radar-badge.svg", "python-remote/og.svg", "radar-badge.svg", "compare/index.html",
                        "llms.txt", ".nojekyll", ".well-known/security.txt"}
    assert toolkit.files.exists("site/feeds/radar.xml")
    assert "Sitemap: https://me.github.io/sitemap.xml" in out["robots.txt"]
    assert b.publish(out) == 20
    puts = transport.calls_to("https://api.github.com/repos/me/me.github.io/contents/", "PUT")
    paths = sorted(c["url"].split("/contents/")[1] for c in puts)
    assert paths == [".nojekyll", ".well-known/security.txt", "404.html", "compare/index.html", "contact/index.html",
                     "feeds/radar.xml", "index.html", "intel/index.html", "legal/privacy/index.html",
                     "legal/refunds/index.html", "legal/terms/index.html", "llms.txt", "pricing/index.html", "python-remote/index.html",
                     "python-remote/og.svg", "python-remote/radar-badge.svg", "radar-badge.svg", "robots.txt", "sitemap.xml",
                     "thanks/index.html"]
    assert all(json.loads(c["body"])["branch"] == "gh-pages" for c in puts)


# ------------------------------------------------------------------ articles & syndication
def records(n=12):
    out = []
    for i in range(n):
        out.append({
            "company": f"Co{i}", "urgency_score": 90 - i, "openings": 2, "stack": ["PostgreSQL", "AWS", "Python"],
            "intent_signals": ["migration"] if i < 5 else [], "contact_email": "never@leak.example",
        })
    return out


def test_article_headline_follows_data_and_links_back():
    a = build_article("python-remote", "Python Remote", records(), NOW, lander_url="https://me.github.io/python-remote/",
                      showcase_url="https://github.com/me/showcase", checkout_url="https://buy.stripe.com/t", price_cents=900)
    # 5 companies migrate; all 5 list PostgreSQL or AWS: the headline number is computed, not invented
    assert a.title == "State of Python Migrations Q3 2026: 5 Companies Hiring for PostgreSQL & AWS"
    assert a.guid == "radar-python-remote-2026-w40"
    assert "never@leak.example" not in a.markdown and "@" not in a.markdown.replace("https://", "")
    dev = a.for_channel("devto")
    assert "Originally published at [https://me.github.io/python-remote/](https://me.github.io/python-remote/?utm_source=devto" in dev.markdown
    assert "https://buy.stripe.com/t?client_reference_id=am--devto--radar_python_remote_2026_w40" in dev.markdown
    assert "$9.00" in dev.markdown and "\u27e6" not in dev.markdown and "⟦" not in dev.markdown
    assert dev.canonical_url == "https://me.github.io/python-remote/"  # canonical stays clean
    assert len(a.tags) <= 4 and all(re.fullmatch(r"[a-z0-9]+", t) for t in a.tags)
    plain = build_article("x", "X", [dict(r, intent_signals=[]) for r in records()], NOW)
    assert plain.title == "State of X Hiring Q3 2026: 12 Companies Hiring for PostgreSQL & AWS"


def test_platform_payloads():
    a = build_article("python-remote", "Python Remote", records(), NOW, lander_url="https://me.github.io/p/")
    dv = DevToPublisher.payload(a, published=False)["article"]
    assert dv["published"] is False and dv["canonical_url"] == "https://me.github.io/p/" and len(dv["tags"]) <= 4
    hn = HashnodePublisher(None, "tok", "pub1").payload(a)
    assert "publishPost" in hn["query"]
    assert hn["variables"]["input"]["publicationId"] == "pub1"
    assert hn["variables"]["input"]["originalArticleURL"] == "https://me.github.io/p/"


class FakePublisher:
    def __init__(self, name, fail=False):
        self.name, self.fail, self.calls = name, fail, []

    def configured(self):
        return True

    def publish(self, article, published):
        self.calls.append(published)
        if self.fail:
            raise RuntimeError("down")
        return f"https://{self.name}.example/{article.guid}"


def test_syndicator_cadence_dedupe_and_isolation(config, state, toolkit, clock):
    good, bad = FakePublisher("devto"), FakePublisher("hashnode", fail=True)
    syn = Syndicator(config, state, toolkit.files, [good, bad])
    a = build_article("python-remote", "Python Remote", records(), clock())
    res = syn.syndicate(a, clock())
    assert res["devto"].startswith("https://") and res["hashnode"].startswith("failed") and res["rss"] == "added"
    assert toolkit.files.exists(res["substack"])
    assert syn.syndicate(a, clock())["devto"] == "already published"
    clock.advance(days=3)
    b = build_article("python-remote", "Python Remote", records(), clock() + timedelta(days=7))
    assert syn.syndicate(b, clock())["devto"] == "not due"
    clock.advance(days=5)
    assert syn.syndicate(b, clock())["devto"].startswith("https://")
    assert [i["guid"] for i in state.get("feed_items")] == [a.guid, b.guid]
    assert state.recent_errors(1)[0]["source"] == "syndication:hashnode"


def test_devto_over_http_respects_draft_mode(config, state, toolkit, transport):
    config.devto_api_key, config.syndication_publish = "dk", False
    transport.add_json("https://dev.to/api/articles", {"id": 1, "url": "https://dev.to/me/draft"}, 201)
    syn = Syndicator(config, state, toolkit.files, [DevToPublisher(toolkit.http, "dk")])
    res = syn.syndicate(build_article("p", "P", records(), NOW, lander_url="https://x/p/"), NOW)
    call = transport.calls_to("https://dev.to/api/articles", "POST")[0]
    assert call["headers"]["api-key"] == "dk"
    assert json.loads(call["body"])["article"]["published"] is False
    assert res["devto"] == "https://dev.to/me/draft"


def test_github_discussions_graphql_flow(config, toolkit, transport):
    toolkit.github.token = "ghp"
    transport.add("https://api.github.com/graphql", [
        Response(200, "u", json.dumps({"data": {"repository": {"id": "R1", "discussionCategories": {"nodes": [{"id": "C1", "name": "Announcements"}]}}}}).encode()),
        Response(200, "u", json.dumps({"data": {"createDiscussion": {"discussion": {"url": "https://github.com/me/r/discussions/1"}}}}).encode()),
    ])
    pub = GitHubDiscussionsPublisher(toolkit.github, "me/r", "announcements")
    url = pub.publish(build_article("p", "P", records(), NOW), True)
    assert url.endswith("/discussions/1")
    create = json.loads(transport.calls_to("https://api.github.com/graphql")[1]["body"])
    assert create["variables"]["input"]["repositoryId"] == "R1" and create["variables"]["input"]["categoryId"] == "C1"


# ------------------------------------------------------------------ strategy tasks
def test_inbound_tasks_end_to_end(toolkit, config, make_hypothesis, state):
    hyp = make_hypothesis()
    pipeline(toolkit, hyp)
    s = InboundSyndicator()
    assert s.syndicate(TaskContext(toolkit, hyp, {})).metrics["syndicated"] is False  # 4 companies < 10
    config.syndication_min_companies = 3
    res = s.syndicate(TaskContext(toolkit, hyp, {}))
    assert res.metrics["syndicated"] and res.metrics["results"]["rss"] == "added"
    site = s.build_site(TaskContext(toolkit, hyp, {}))
    assert site.metrics == {"pages": 1, "matrix_pages": 0, "feed_items": 1, "committed": 0, "pending": 0, "indexnow": 0}
    html_text = toolkit.files.read_text("site/python-remote/index.html")
    assert json.loads(parse(html_text).jsonld[0])["@type"] == "Product"
    feed = ET.fromstring(toolkit.files.read_text("site/feeds/radar.xml"))
    assert feed.find("channel/item/title").text.startswith("State of Python Hiring Q3 2026")
