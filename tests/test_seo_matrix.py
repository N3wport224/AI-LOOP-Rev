"""Programmatic search-intent pages, sitemap coverage and IndexNow submission."""

import json
import re
import xml.etree.ElementTree as ET
from datetime import timedelta

from tests.conftest import NOW
from tools.page_builder import (
    MatrixPage, ProductPage, SiteBuilder, changed_urls, compile_matrix_pages, render_matrix_page,
    render_sitemap, submit_indexnow, tech_slug,
)
from tools.http_client import Response

BASE = "https://me.github.io/datasets"


def rec(i, stack, *, path="", category="", score=0, days=2, openings=1):
    return {
        "company": f"Co{i}", "domain": f"co{i}.example", "stack": stack, "openings": openings,
        "latest_posted_at": (NOW - timedelta(days=days)).isoformat(), "intent_score": score,
        "intent_level": "High" if score >= 60 else "Medium" if score >= 35 else ("Low" if score else ""),
        "intent_tag": "Urgency: High (Cloud Migration)" if score >= 60 else "", "intent_category": category,
        "commercial_signals": ["Cloud Migration"] if category else [], "migration_path": path,
        "contact_email": f"hr@co{i}.example", "intent_evidence": ["moving from our secret project"],
        "remote_friendly": i % 2 == 0,
    }


def datasets():
    py = [rec(i, ["Python", "Django", "AWS"], days=i) for i in range(8)]
    py += [rec(10 + i, ["Python", "PostgreSQL"], path="Oracle → PostgreSQL", category="migration", score=70) for i in range(3)]
    k8s = [rec(20 + i, ["Kubernetes", "Go"], path="VMware → Kubernetes", category="migration", score=65) for i in range(4)]
    k8s += [rec(30, ["Rust"])]
    return {"python-remote": py, "devops-sre": k8s}


OFFERS = {
    "python-remote": {"niche": "python-remote", "title": "Python Remote Tech Stack Intel", "price_cents": 900,
                      "currency": "usd", "checkout_url": "https://buy.stripe.com/py", "subscription_url": "https://buy.stripe.com/sub",
                      "subscription_price_cents": 1000, "subscription_interval": "month"},
    "devops-sre": {"niche": "devops-sre", "title": "DevOps SRE Tech Stack Intel", "price_cents": 1400, "currency": "usd",
                   "checkout_url": "https://buy.stripe.com/k8s"},
}


def test_slugs():
    assert tech_slug("C++") == "cpp" and tech_slug("C#") == "csharp" and tech_slug(".NET") == "dotnet"
    assert tech_slug("Next.js") == "nextjs" and tech_slug("Vector DB") == "vector-databases" and tech_slug("Python") == "python"


def test_matrix_compiles_only_well_supported_pages():
    pages = compile_matrix_pages(datasets(), OFFERS, NOW, min_companies=5, min_migrations=3)
    paths = {p.path for p in pages}
    assert "intel/companies-hiring-python-engineers.html" in paths          # 11 companies
    assert "intel/companies-hiring-aws-engineers.html" in paths             # 8
    assert "intel/companies-hiring-rust-engineers.html" not in paths        # 1: too thin to deserve a page
    assert "intel/companies-hiring-kubernetes-engineers.html" not in paths  # 4 < 5
    assert "intel/postgresql-infrastructure-migrations.html" in paths       # 3 migrating to it
    assert "intel/kubernetes-infrastructure-migrations.html" in paths
    py = next(p for p in pages if p.path.endswith("companies-hiring-python-engineers.html"))
    assert py.stats["companies"] == 11 and py.stats["companies_7d"] == 8 + 3  # days 0..7 plus the 3 migrators
    assert py.offer["checkout_url"] == "https://buy.stripe.com/py"
    assert py.rows[0]["company"].startswith("Co1")  # highest intent first
    assert all("contact_email" not in r and "intent_evidence" not in r for r in py.rows)
    k8s = next(p for p in pages if p.path.endswith("kubernetes-infrastructure-migrations.html"))
    assert k8s.stats["into"] == 4 and k8s.stats["paths"] == [("VMware → Kubernetes", 4)]
    assert k8s.offer["niche"] == "devops-sre"
    assert any(path.endswith("companies-hiring-python-engineers.html") for _, path in
               next(p for p in pages if p.tech == "PostgreSQL").related)  # internal links to the sibling page
    assert len(compile_matrix_pages(datasets(), OFFERS, NOW, max_pages=2)) == 2


def test_matrix_page_html_jsonld_cta_and_form():
    page = next(p for p in compile_matrix_pages(datasets(), OFFERS, NOW) if p.tech == "Python" and p.kind == "hiring")
    html = render_matrix_page(page, BASE, "Tech Stack Intel", "https://hooks.example.com/lead-magnet/capture")
    assert f'<link rel="canonical" href="{BASE}/intel/companies-hiring-python-engineers.html">' in html
    assert "<title>11 Companies Hiring Python Engineers (September 2026)</title>" in html
    ld = json.loads(re.search(r'<script type="application/ld\+json">\s*(.*?)\s*</script>', html, re.S).group(1))
    assert ld["@type"] == "Dataset" and ld["isAccessibleForFree"] is False and ld["url"].endswith(".html")
    assert ld["offers"]["price"] == "9.00" and ld["offers"]["url"] == "https://buy.stripe.com/py"
    assert 50 <= len(ld["description"]) <= 5000
    assert 'data-checkout href="https://buy.stripe.com/py"' in html and "https://buy.stripe.com/sub" in html
    assert 'action="https://hooks.example.com/lead-magnet/capture"' in html and 'name="website"' in html
    assert "hr@co" not in html and "secret project" not in html  # no contacts, no quoted posting text
    assert html.count("<tr>") == 1 + 5


def test_jsonld_escapes_script_breakout():
    page = MatrixPage("hiring", "X</script><script>alert(1)</script>", "intel/x.html", "t", "d" * 60, {"companies": 5},
                      ["company"], [], NOW.isoformat())
    html = render_matrix_page(page, BASE)
    block = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S).group(1)
    assert "</script>" not in block and json.loads(block)["keywords"][0].startswith("X")


def test_sitemap_lists_every_page_and_is_valid_xml():
    pages = compile_matrix_pages(datasets(), OFFERS, NOW)
    product = ProductPage("python-remote", "Py", "s", 900, "usd", "https://buy.stripe.com/py", [], [],
                          updated_at=NOW.isoformat())
    xml = render_sitemap([product], BASE, pages)
    root = ET.fromstring(xml.split("\n", 1)[1])
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = [u.find("s:loc", ns).text for u in root.findall("s:url", ns)]
    assert f"{BASE}/intel/" in locs and f"{BASE}/python-remote/" in locs
    assert all(f"{BASE}/{p.path}" in locs for p in pages)
    assert all(u.find("s:lastmod", ns) is None or re.fullmatch(r"\d{4}-\d\d-\d\d", u.find("s:lastmod", ns).text)
               for u in root.findall("s:url", ns))


def test_site_builder_writes_matrix_hub_key_file_and_forms(config, state, toolkit):
    config.pages_base_url = BASE
    config.public_webhook_url = "https://hooks.example.com/webhook"
    pages = compile_matrix_pages(datasets(), OFFERS, NOW)
    product = ProductPage("python-remote", "Py", "s", 900, "usd", "https://buy.stripe.com/py", [], [])
    out = SiteBuilder(config, toolkit.files, None).build([product], [], NOW, matrix=pages, indexnow_key="abc123def456")
    assert "intel/index.html" in out and out["abc123def456.txt"] == "abc123def456"
    assert all(p.path in out for p in pages)
    assert 'action="https://hooks.example.com/lead-magnet/capture"' in out["python-remote/index.html"]
    assert "companies-hiring-python-engineers.html" in out["intel/index.html"]
    assert toolkit.files.exists("site/intel/companies-hiring-python-engineers.html")
    assert "Sitemap: https://me.github.io/datasets/sitemap.xml" in out["robots.txt"]
    config.lead_magnet_enabled = False
    off = SiteBuilder(config, toolkit.files, None).build([ProductPage("python-remote", "Py", "s", 900, "usd", "", [], [])], [], NOW)
    assert "lead-magnet/capture" not in off["python-remote/index.html"]


def test_indexnow_submits_only_changed_pages(toolkit, transport):
    out = {"a/index.html": "<p>one</p><time class='ago'>2026-09-30T12:00:00</time>", "b.html": "<p>two</p>", "x.svg": "<svg/>"}
    urls, hashes = changed_urls(out, BASE, {})
    assert sorted(urls) == [f"{BASE}/a/", f"{BASE}/b.html"]
    out2 = dict(out, **{"a/index.html": "<p>one</p><time class='ago'>2026-10-01T09:00:00</time>"})  # only the timestamp moved
    assert changed_urls(out2, BASE, hashes)[0] == []
    out3 = dict(out, **{"b.html": "<p>two, updated</p>"})
    assert changed_urls(out3, BASE, hashes)[0] == [f"{BASE}/b.html"]

    transport.add("https://api.indexnow.org/indexnow", Response(202, "u", b""))
    assert submit_indexnow(toolkit.http, BASE, "k" * 32, urls) == 202
    body = json.loads(transport.calls_to("https://api.indexnow.org/indexnow")[0]["body"])
    assert body == {"host": "me.github.io", "key": "k" * 32, "keyLocation": f"{BASE}/{'k' * 32}.txt", "urlList": urls}
    assert submit_indexnow(toolkit.http, "http://insecure.example", "k", urls) == 0


def test_build_site_task_submits_to_indexnow_after_publishing(toolkit, transport, make_hypothesis, config, state):
    """End to end: packaged dataset → site with matrix pages → Pages commit → IndexNow, once."""
    from strategies.b2b_lead_aggregator import LeadAggregator
    from strategies.base import TaskContext
    from strategies.digital_asset_packager import DigitalAssetPackager
    from strategies.inbound_syndicator import InboundSyndicator
    from strategies.tech_stack_intel import TechStackIntel

    config.github_token, config.github_pages_repo, config.pages_base_url = "ghp_x", "me/datasets", BASE
    config.seo_min_companies = 2
    toolkit.github.token = "ghp_x"  # the fixture built the client before the token was set

    def contents(method, url, headers):
        if method == "GET":
            return Response(404, url, b"{}")
        return Response(201, url, json.dumps({"content": {"sha": "t", "html_url": url}}).encode())

    transport.add("https://api.github.com/repos/me/datasets/contents/", contents)
    transport.add("https://api.indexnow.org/indexnow", Response(200, "u", b""))
    hyp = make_hypothesis()
    ctx = TaskContext(toolkit, hyp, {})
    LeadAggregator().run("aggregate_leads", ctx)
    TechStackIntel().run("build_intel", ctx)
    DigitalAssetPackager().run("package_asset", ctx)
    res = InboundSyndicator().run("build_site", ctx)
    assert res.metrics["matrix_pages"] >= 1 and res.metrics["indexnow"] >= 1, res.summary
    assert state.get("indexnow_key") and len(state.get("indexnow_hashes")) >= 2
    again = InboundSyndicator().run("build_site", ctx)
    assert again.metrics["indexnow"] == 0  # nothing changed
    assert len(transport.calls_to("https://api.indexnow.org/indexnow")) == 1


def test_publish_resumes_across_cycles_when_the_api_budget_runs_out(config, state):
    """Regression: dozens of matrix pages × (GET + PUT) exceeded the 60-call cycle budget, failing
    build_site every cycle and never finishing. Now a manifest skips unchanged files and the rest
    goes out next cycle."""
    from tools.errors import CircuitOpenError

    class Budgeted:
        def __init__(self, budget):
            self.budget, self.puts = budget, []

        def configured(self):
            return True

        def put_file(self, repo, path, content, message, branch):
            if self.budget <= 0:
                raise CircuitOpenError("API budget exhausted")
            self.budget -= 1
            self.puts.append(path)
            return {"changed": True}

    config.github_pages_repo = "me/datasets"
    out = {f"intel/p{i:02d}.html": f"<p>{i}</p>" for i in range(25)}
    gh = Budgeted(10)
    builder = SiteBuilder(config, None, gh)
    assert builder.publish(out, state) == 10 and builder.pending == 15
    gh.budget = 10
    assert builder.publish(out, state) == 10 and builder.pending == 5
    gh.budget = 10
    assert builder.publish(out, state) == 5 and builder.pending == 0
    assert len(gh.puts) == len(set(gh.puts)) == 25  # nothing sent twice
    gh.budget = 10
    assert builder.publish(out, state) == 0 and gh.budget == 10  # unchanged: no API calls at all
    assert builder.publish(dict(out, **{"intel/p03.html": "<p>new</p>"}), state) == 1
