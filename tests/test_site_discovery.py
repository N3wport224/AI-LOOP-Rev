"""Phases 225-229: search index and page, full catalog, status page, what's new."""

import json

from strategies import product_factory as pf
from strategies import site_discovery as sd
from strategies.inbound_syndicator import site_pages
from strategies.money_insight import record_price_change
from tests import test_business_ops
from tests.test_product_factory import postings, unique_links

kit = test_business_ops.kit  # the same live-mode toolkit fixture
SHELL = lambda title, body, desc: body  # noqa: E731
SHELL_AT = lambda title, body, desc, depth: f"<!--{depth}-->" + body  # noqa: E731


def made(kit, state, clock, transport, n=1, techs=("rust",)):
    unique_links(transport)
    out = []
    for tech in techs:
        postings(state, clock, 30, [tech], prefix=tech)
    for _ in range(n):
        m = pf.tick(kit, force=True)["made"]
        if m:
            out.append(m)
    return out


# ------------------------------------------------------------------ Phases 225-226
def test_search_index_and_page(kit, state, clock, transport):
    m = made(kit, state, clock, transport)[0]
    index = json.loads(sd.search_index(site_pages(kit), state))
    entry = next(e for e in index if e["u"] == f"{m['slug']}/")
    assert entry == {"t": m["title"], "u": f"{m['slug']}/", "p": m["price_cents"], "k": "Rust", "y": "slice", "r": 30,
                     "d": (clock() - __import__("datetime").timedelta(days=3)).date().isoformat()}  # newest posting (Phase 378)
    page = sd.search_page(SHELL)
    assert 'id="q"' in page and "../search.json" in page and 'href="../catalog/"' in page
    assert "innerHTML" not in page and "textContent" in page


# ------------------------------------------------------------------ Phase 227
def test_catalog_groups_by_technology_and_paginates(kit, state, clock, transport, monkeypatch):
    made(kit, state, clock, transport, n=2, techs=("rust", "golang"))
    pages = site_pages(kit)
    files = sd.catalog_pages(pages, state, SHELL_AT)
    assert list(files) == ["catalog/index.html"]
    html = files["catalog/index.html"]
    assert "<h2>Rust (1)</h2>" in html and "<h2>Go (1)</h2>" in html
    monkeypatch.setattr(sd, "PER_PAGE", 1)
    files = sd.catalog_pages(pages, state, SHELL_AT)
    n = len(json.loads(sd.search_index(pages, state)))
    assert len(files) == n and "catalog/2/index.html" in files
    assert files["catalog/2/index.html"].startswith("<!--2-->") and 'href="../../catalog/">← Previous' in files["catalog/2/index.html"]
    assert 'href="../catalog/2/">Next →' in files["catalog/index.html"]
    assert sd.catalog_pages([], state, SHELL_AT) == {}


# ------------------------------------------------------------------ Phase 228
def test_status_page_shows_counts_and_board_names_only(state, config, clock):
    postings(state, clock, 12, ["rust"])
    config.lead_sources = ["remoteok", "remotive"]
    state.set("ops", {"sources": {"remoteok": {"ok": True}, "remotive": {"ok": False, "last_error": "secret detail"}}})
    page = sd.status_page(state, config, SHELL)
    assert "Job postings seen in the last 7 days: 12" in page and "Products on sale: 0" in page
    assert "<li>Remote OK: answering</li>" in page and "<li>Remotive: not answering lately</li>" in page
    assert "secret detail" not in page


# ------------------------------------------------------------------ Phase 229
def test_whats_new_lists_weeks_with_changes(kit, state, clock, transport, config):
    assert sd.changes_page(state, SHELL) == ""
    m = made(kit, state, clock, transport)[0]
    record_price_change(state, m["asset_id"], 500, 700)
    page = sd.changes_page(state, SHELL)
    assert f'<li><a href="../{m["slug"]}/">{m["title"]}</a></li>' in page and "New (1)" in page
    assert "Price changes" in page and f"{m['title']}: $5 → $7" in page
    clock.advance(days=3)
    state._exec("UPDATE factory_products SET status = 'retired', retired_at = ? WHERE slug = ?", (state.now(), m["slug"]))
    assert f"<li>{m['title']}</li>" in sd.changes_page(state, SHELL)  # its page is gone: no dead link
    clock.advance(days=config.factory_retire_days + 1)
    state._exec("UPDATE factory_products SET status = 'live', retired_at = NULL WHERE slug = ?", (m["slug"],))
    pf.retire_unsold(kit)
    page = sd.changes_page(state, SHELL)
    weeks = sd.changes(state, weeks=20)
    assert any(w["retired"] == [m["title"]] for w in weeks)
    assert any(w["added"] == [("", m["title"])] for w in weeks)  # retired: named, not linked
    assert f"Retired (1):</b> {m['title']}" in page


# ------------------------------------------------------------------ the site build
def test_the_home_page_links_the_discovery_pages():
    from tools.page_builder import render_index

    html = render_index([], "https://me.github.io", "Site", links=(("search/", "Search"), ("catalog/", "All products")))
    assert '<a href="search/">Search</a> ·' in html and '<a href="catalog/">All products</a> ·' in html


def test_a_full_site_build_has_the_pages_and_no_broken_links(kit, state, config, clock, transport):
    from strategies.inbound_syndicator import InboundSyndicator
    from tests.test_business_ops import ctx

    config.pages_base_url = "https://me.github.io/d"
    m = made(kit, state, clock, transport, n=2, techs=("rust", "python"))
    postings(state, clock, 6, ["rust"], prefix="old", days_ago=20)  # a rising technology for trends/
    clock.advance(days=3)
    state._exec("UPDATE factory_products SET status = 'retired', retired_at = ? WHERE slug = ?", (state.now(), m[1]["slug"]))
    InboundSyndicator().run("build_site", ctx(kit))
    for rel in ("search.json", "search/index.html", "catalog/index.html", "status/index.html", "changes/index.html",
                "trends/index.html"):
        assert kit.files.exists(f"site/{rel}"), rel
    home = kit.files.read_text("site/index.html")
    assert '<a href="search/">Search</a>' in home and '<a href="trends/">Hiring trends</a>' in home
    sitemap = kit.files.read_text("site/sitemap.xml")
    assert "/d/catalog/" in sitemap and "/d/trends/" in sitemap
    audit = state.get("site_audit")
    assert audit and audit["broken_links"] == [], audit["broken_links"]
