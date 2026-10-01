"""Phases 180-184: blog, weekly roundup, weekly thread, newsletter section, new-products feed."""

import xml.etree.ElementTree as ET

from strategies import content_engine as ce
from strategies import marketing_engine as me
from strategies import product_factory as pf
from tests import test_business_ops
from tests.test_product_factory import postings, salaried, unique_links

kit = test_business_ops.kit  # the same live-mode toolkit fixture


def make(kit, state, clock, transport, n=2):
    unique_links(transport)
    salaried(state, clock, 40, ["rust"], "r")
    postings(state, clock, 30, ["kotlin"], prefix="k")
    return [pf.tick(kit, force=True)["made"] for _ in range(n)]


# ------------------------------------------------------------------ Phase 180
def test_a_daily_blog_post_from_the_data(kit, state, config, clock, transport):
    make(kit, state, clock, transport)
    summary = ce.write_post(kit)
    post = state.get(ce.BLOG)[-1]
    assert summary.startswith("blog post: Who's hiring") and post["tech"] in ("rust", "kotlin")
    assert "Most open roles" in post["html"] and f'href="../../hiring/{post["tech"]}/"' in post["html"]
    if post["tech"] == "rust":
        assert "Median yearly salary" in post["html"]
    assert any(i["guid"] == f"blog-{post['slug']}" for i in state.get("feed_items"))
    second = ce.write_post(kit)
    assert state.get(ce.BLOG)[-1]["tech"] != post["tech"] and second  # the other technology next
    assert ce.write_post(kit) == ""  # both covered this month
    pages = ce.blog_pages(state, lambda t, b, d, depth=1: b)
    assert f"blog/{post['slug']}/index.html" in pages and "blog/index.html" in pages


# ------------------------------------------------------------------ Phase 181
def test_the_weekly_roundup_is_syndicated_once(kit, state, config, clock, transport):
    config.pages_base_url = "https://me.github.io/site"
    made = make(kit, state, clock, transport)
    summary = ce.weekly_roundup(kit)
    assert summary.startswith("weekly roundup: rss: added")
    item = next(i for i in state.get("feed_items") if i["guid"].startswith("roundup-"))
    assert made[0]["title"] in item["body_html"] and "https://me.github.io/site/" in item["body_html"]
    assert ce.weekly_roundup(kit) == ""  # once a week


# ------------------------------------------------------------------ Phase 182
def test_the_weekly_thread_is_a_draft_with_a_tracked_link(kit, state, config, clock, transport):
    config.pages_base_url = "https://me.github.io/site"
    make(kit, state, clock, transport)
    pid = me.add_play(state, "weekly_thread", "draft", "queued")
    out = ce.weekly_thread(kit, pid)
    assert out["body"].startswith("1/ 2 new hiring datasets") and f"utm_campaign=mkt{pid}" in out["link"]
    assert "weekly_thread" in me.PLAYBOOK and me.PLAYBOOK["weekly_thread"]["mode"] == "draft"


# ------------------------------------------------------------------ Phase 183
def test_the_pulse_lists_new_datasets(kit, state, config, clock, transport):
    config.pages_base_url = "https://me.github.io/site"
    made = make(kit, state, clock, transport)
    text, html = ce.newsletter_section(state, config, "pulse_2026_w40")
    assert text[1] == "New this week:" and any(made[0]["title"] in line for line in text)
    assert "utm_source=newsletter" in html and "utm_campaign=pulse_2026_w40" in html


# ------------------------------------------------------------------ Phase 184
def test_new_products_feed(kit, state, config, clock, transport):
    config.pages_base_url = "https://me.github.io/site"
    made = make(kit, state, clock, transport)
    root = ET.fromstring(ce.products_feed(state, config, "Site"))
    titles = [i.findtext("title") for i in root.findall("./channel/item")]
    assert set(titles) == {m["title"] for m in made} and root.findall("./channel/item")[0].findtext("link").startswith("https://")


def test_unmeasurable_auto_plays_are_never_paused(kit, state):
    for _ in range(me.PAUSE_AFTER_PLAYS + 2):
        me.add_play(state, "blog_post", "auto", "done")
    assert "blog_post" not in me.pause_losers(state)
