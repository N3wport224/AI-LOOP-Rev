"""Phases 185-189: embeddable badges, HN answer drafts, directories, data story, hiring heatmap."""

import json

from strategies import distribution as dist
from strategies import marketing_engine as me
from strategies import product_factory as pf
from strategies.inbound_syndicator import InboundSyndicator, seo_extra, site_pages
from tests import test_business_ops
from tests.test_product_factory import postings, unique_links
from tools.http_client import Response
from tools.page_builder import SiteBuilder
from tools.site_audit import audit_site

kit = test_business_ops.kit  # the same live-mode toolkit fixture


class Files:
    def write_text(self, *a):
        pass

    def write_bytes(self, *a):
        pass


def catalog(kit, state, config, clock, transport):
    config.pages_base_url = "https://me.github.io/site"
    unique_links(transport)
    postings(state, clock, 30, ["rust"], location="Berlin, Germany")
    postings(state, clock, 30, ["kotlin"], location="Remote", remote=True, prefix="k")
    for _ in range(2):
        pf.tick(kit, force=True)


# ------------------------------------------------------------------ Phases 185 + 189 (site)
def test_badges_embed_page_and_heatmap_are_built_and_pass_the_audit(kit, state, config, clock, transport, monkeypatch):
    catalog(kit, state, config, clock, transport)
    captured = {}
    monkeypatch.setattr(SiteBuilder, "publish", lambda self, out, state=None: captured.update(out) or 0)
    InboundSyndicator().build_site(type("C", (), {"tools": kit})())
    assert "badges/rust.svg" in captured and "12 companies" in captured["badges/rust.svg"]
    embed = captured["embed/index.html"]
    assert "utm_source=embed" in embed and "&lt;img src=&quot;https://me.github.io/site/badges/rust.svg&quot;" in embed
    heat = captured["tools/hiring-heatmap/index.html"]
    assert "<th scope=\"row\">Rust</th>" in heat and "Free to cite with a link" in heat
    assert 'href="tools/hiring-heatmap/"' in captured["index.html"] and 'href="embed/"' in captured["index.html"]
    report = audit_site(captured, config.pages_base_url)
    assert report["broken_links"] == [] and report["a11y"] == []


# ------------------------------------------------------------------ Phase 186
def test_hn_answer_drafts_from_real_questions(kit, state, config, clock, transport):
    catalog(kit, state, config, clock, transport)
    hits = {"hits": [{"objectID": "111", "title": "Ask HN: which companies hiring rust devs remotely?"},
                     {"objectID": "222", "comment_text": "Nothing about our techs here"}]}
    transport.add("https://hn.algolia.com/api/v1/search_by_date", Response(200, "u", json.dumps(hits).encode(), {}))
    pid = me.add_play(state, "hn_answers", "draft", "queued")
    out = dist.hn_answers(kit, pid)
    assert out["title"].startswith("1 Hacker News thread") and "news.ycombinator.com/item?id=111" in out["body"]
    assert "12 companies have 30 open roles mentioning Rust" in out["body"] and f"utm_campaign=mkt{pid}" in out["body"]
    assert dist.hn_answers(kit, pid) is None  # the same thread isn't drafted twice


# ------------------------------------------------------------------ Phase 187
def test_directories_checklist(kit, state, config, clock, transport):
    catalog(kit, state, config, clock, transport)
    assert "2 datasets of companies hiring engineers" in dist.directory_blurb(state, config)
    assert not any(d["done"] for d in dist.directories(state))
    assert dist.mark_directory(state, "kaggle") and not dist.mark_directory(state, "nope")
    assert next(d for d in dist.directories(state) if d["key"] == "kaggle")["done"]


# ------------------------------------------------------------------ Phase 188
def test_monthly_data_story_draft(kit, state, config, clock, transport):
    catalog(kit, state, config, clock, transport)
    postings(state, clock, 25, ["golang"], location="Austin, TX, United States", prefix="g")
    pid = me.add_play(state, "data_story", "draft", "queued")
    out = dist.data_story(kit, pid)
    assert "Rust: 12 companies, 30 open roles" in out["body"] and "never as a bulk mail" in out["body"]
    assert "tools/hiring-heatmap/" in out["link"] and me.PLAYBOOK["data_story"]["every_days"] == 30


def test_seo_extra_still_builds_without_products(kit, state, config):
    assert seo_extra(kit, site_pages(kit), []) == {}
