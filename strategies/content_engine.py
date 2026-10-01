"""Content (Phases 180-184): the agent writes about its own data, every day, everywhere it's allowed.

* **Phase 180, blog** (auto play ``blog_post``, daily): a post on your own site
  (``blog/<slug>/``) about the technology with the freshest data not covered in 30 days: how many
  companies are hiring, who has the most roles, remote share, seniority mix, salaries when posted,
  with links to the hub and datasets. Every post also goes into the RSS feed.
* **Phase 181, weekly roundup** (auto play ``weekly_roundup``): "New hiring datasets this week",
  syndicated through the usual publishers (Dev.to, Hashnode, GitHub Discussions, RSS, Substack
  export) at their normal cadence, links tagged per platform.
* **Phase 182, weekly thread** (draft play ``weekly_thread``): an X / LinkedIn thread of the week's
  new products for you to post.
* **Phase 183, newsletter:** the weekly Tech Pulse to confirmed free-sample subscribers (they opted
  in) ends with "New this week": up to 5 new datasets, links tagged ``utm_source=newsletter``.
* **Phase 184, new-products feed:** ``feeds/products.xml`` (RSS) lists the newest 50 products; the
  home page advertises it.
"""

from __future__ import annotations

import html
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

BLOG = "blog_posts"
BLOG_DIR = "blog"
PRODUCTS_FEED = "feeds/products.xml"


def _site(cfg: Any) -> str:
    return str(cfg.pages_base_url or "").rstrip("/")


def _new_products(state: Any, days: int = 7, limit: int = 10) -> list[dict[str, Any]]:
    from strategies.revenue_models import live_products

    since = (state.clock() - timedelta(days=days)).isoformat(timespec="seconds")
    return [a for a in live_products(state, since=since) if a.get("checkout_url")][:limit]


# ------------------------------------------------------------------ Phase 180
def pick_topic(state: Any, cfg: Any) -> str | None:
    from tools.seo_scale import tech_counts

    covered = {p["tech"]: p["date"] for p in state.get(BLOG) or []}
    cutoff = (state.clock() - timedelta(days=30)).date().isoformat()
    counts = tech_counts(state, int(cfg.factory_max_age_days))
    for tech, c in sorted(counts.items(), key=lambda kv: (-kv[1]["companies"], kv[0])):
        if c["companies"] >= int(cfg.factory_min_companies) and covered.get(tech, "") < cutoff:
            return tech
    return None


def write_post(tools: Any) -> str:
    """Auto play: one blog post. Returns a summary, or "" when there's nothing new to write about."""
    from statistics import median

    from strategies.product_factory import facets, fresh_leads, label
    from strategies.product_types import salary_rows
    from tools.seo_scale import hub_path

    state, cfg = tools.state, tools.config
    tech = pick_topic(state, cfg)
    if not tech:
        return ""
    rows = [lead for lead in fresh_leads(state, int(cfg.factory_max_age_days)) if tech in facets(lead)["techs"]]
    name = label(tech)
    companies = Counter(" ".join(str(r.get("company") or "").split()) for r in rows if r.get("company"))
    remote = round(100 * sum(1 for r in rows if "remote" in facets(r)["regions"]) / len(rows)) if rows else 0
    levels = Counter(str(r.get("seniority") or "mid") for r in rows)
    pay = [r["salary_mid"] for r in salary_rows(rows)]
    date = state.clock().strftime("%B %d, %Y")
    title = f"Who's hiring {name} engineers: {len(companies)} companies, {len(rows)} open roles ({state.clock():%b %Y})"
    slug = re.sub(r"[^a-z0-9]+", "-", f"{tech}-hiring-{state.clock():%Y-%m-%d}".lower()).strip("-")
    hub = f"../../{hub_path(tech)}"
    body = [f"<h1>{html.escape(title)}</h1>", f"<p>As of {date}, {len(companies)} companies have {len(rows)} open roles that "
            f"mention {html.escape(name)} in public job postings. {remote}% of them are remote.</p>",
            "<h2>Most open roles</h2><ol>" + "".join(f"<li>{html.escape(c)} ({n})</li>" for c, n in companies.most_common(10)) + "</ol>",
            "<h2>Seniority</h2><ul>" + "".join(f"<li>{html.escape(k)}: {v}</li>" for k, v in levels.most_common()) + "</ul>"]
    if len(pay) >= 5:
        body.append(f"<h2>Salaries</h2><p>Median yearly salary in the {len(pay)} postings that state one: {median(pay):,.0f} "
                    "(as posted, currencies not converted).</p>")
    body.append(f'<p>Every company, role and link is in the <a href="{hub}">{html.escape(name)} hiring datasets</a>.</p>')
    posts = list(state.get(BLOG) or [])
    description = f"{len(companies)} companies are hiring {name} engineers right now: who has the most roles, remote share, seniority."
    posts.append({"slug": slug, "tech": tech, "title": title, "date": state.clock().date().isoformat(), "description": description,
                  "html": "".join(body)})
    state.set(BLOG, posts[-200:])
    _feed_item(state, cfg, title, description, f"{BLOG_DIR}/{slug}/", "".join(body), f"blog-{slug}")
    return f"blog post: {title}"


def _feed_item(state: Any, cfg: Any, title: str, description: str, path: str, body_html: str, guid: str) -> None:
    from tools.attribution import add_utm

    items = list(state.get("feed_items") or [])
    if any(i["guid"] == guid for i in items):
        return
    link = add_utm(f"{_site(cfg)}/{path}", "rss", "feed", guid) if _site(cfg) else path
    items.append({"guid": guid, "title": title, "description": description, "link": link, "published": state.now(),
                  "body_html": body_html})
    state.set("feed_items", items[-100:])


def blog_pages(state: Any, shell: Any) -> dict[str, str]:
    posts = list(reversed(state.get(BLOG) or []))
    if not posts:
        return {}
    out = {f"{BLOG_DIR}/{p['slug']}/index.html": shell(p["title"], p["html"] + '<p><a href="../">All posts</a></p>',
                                                       p["description"][:158], 2) for p in posts}
    items = "".join(f'<li><a href="{html.escape(p["slug"])}/">{html.escape(p["title"])}</a> ({p["date"]})</li>' for p in posts)
    out[f"{BLOG_DIR}/index.html"] = shell("Hiring data blog", f"<h1>Hiring data blog</h1><ul>{items}</ul>",
                                          "Data-driven posts on who is hiring which engineers right now, written from public "
                                          "job postings.", 1)
    return out


# ------------------------------------------------------------------ Phase 181
def weekly_roundup(tools: Any) -> str:
    from strategies.inbound_syndicator import InboundSyndicator
    from tools.syndication import LINK_TOKENS, Article, Syndicator

    state, cfg = tools.state, tools.config
    new = _new_products(state)
    if not new:
        return ""
    year, week, _ = state.clock().isocalendar()
    guid = f"roundup-{year}-w{week:02d}"
    if guid in {i["guid"] for i in state.get("feed_items") or []}:
        return ""
    lines = [f"New hiring datasets this week ({len(new)}), built from public job postings:", ""]
    lines += [f"- **{a['title']}**: {int(a.get('lead_count') or 0)} rows, ${int(a['price_cents']) / 100:.2f}" for a in new]
    lines += ["", f"Browse them all with free previews: {LINK_TOKENS['lander']}"]
    md = "\n".join(lines)
    html_body = "<p>" + "</p><p>".join(html.escape(line) for line in lines if line) + "</p>"
    html_body = html_body.replace(html.escape(LINK_TOKENS["lander"]), f'<a href="{LINK_TOKENS["lander"]}">{LINK_TOKENS["lander"]}</a>')
    site = _site(cfg) + "/" if _site(cfg) else ""
    article = Article(guid=guid, title=f"New hiring datasets this week: {new[0]['title']} and {len(new) - 1} more" if len(new) > 1
                      else f"New hiring dataset this week: {new[0]['title']}", description=f"{len(new)} new datasets of companies "
                      "hiring engineers, by technology, region and seniority.", markdown=md, html=html_body,
                      tags=["hiring", "data", "jobs", "career"], canonical_url=site, links={"lander": site})
    results = Syndicator(cfg, state, tools.files, InboundSyndicator().publishers(tools)).syndicate(article, state.clock())
    return f"weekly roundup: {', '.join(f'{k}: {v}' for k, v in results.items())}"[:300]


# ------------------------------------------------------------------ Phase 182
def weekly_thread(tools: Any, play_id: int) -> dict[str, Any] | None:
    from strategies.marketing_engine import product_url, tagged

    new = _new_products(tools.state, limit=6)
    if not new:
        return None
    first = tagged(product_url(tools.config, new[0]), "x", play_id)
    parts = [f"1/ {len(new)} new hiring datasets this week, each built from public job postings 🧵"]
    parts += [f"{i}/ {a['title']}: {int(a.get('lead_count') or 0)} rows" for i, a in enumerate(new, 2)]
    parts.append(f"{len(new) + 2}/ Free previews: {first}")
    return {"title": f"Thread: {len(new)} new datasets this week", "body": "\n\n".join(parts), "link": first}


# ------------------------------------------------------------------ Phase 183
def newsletter_section(state: Any, cfg: Any, campaign: str) -> tuple[list[str], str]:
    """("New this week" text lines, html) for the weekly Tech Pulse."""
    from strategies.marketing_engine import product_url
    from tools.attribution import add_utm

    new = _new_products(state, limit=5)
    if not new:
        return [], ""
    links = [(a, add_utm(product_url(cfg, a), "newsletter", "email", campaign)) for a in new]
    text = ["", "New this week:"] + [f"- {a['title']} (${int(a['price_cents']) / 100:.2f}): {u}" for a, u in links]
    items = "".join(f'<li><a href="{html.escape(u)}">{html.escape(a["title"])}</a> (${int(a["price_cents"]) / 100:.2f})</li>'
                    for a, u in links)
    return text, f"<h3>New this week</h3><ul>{items}</ul>"


# ------------------------------------------------------------------ Phase 184
def products_feed(state: Any, cfg: Any, site_title: str) -> str:
    import xml.etree.ElementTree as ET
    from email.utils import format_datetime

    from strategies.marketing_engine import product_url
    from strategies.revenue_models import live_products

    rss = ET.Element("rss", version="2.0")
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = f"{site_title}: new datasets"
    ET.SubElement(ch, "link").text = _site(cfg) + "/" if _site(cfg) else "https://example.invalid/"
    ET.SubElement(ch, "description").text = "Every new hiring dataset, as it's published."
    first: dict[str, str] = {}
    for a in state.list_assets():
        key = str(a.get("niche") or a["id"])
        first[key] = min(first.get(key, a["created_at"]), a["created_at"])
    products = sorted((a for a in live_products(state) if a.get("checkout_url")),
                      key=lambda a: first.get(str(a.get("niche") or a["id"]), ""), reverse=True)[:50]
    for a in products:
        el = ET.SubElement(ch, "item")
        ET.SubElement(el, "title").text = a["title"]
        ET.SubElement(el, "link").text = product_url(cfg, a)
        ET.SubElement(el, "guid", isPermaLink="false").text = f"product-{a.get('niche') or a['id']}"
        ET.SubElement(el, "description").text = f"{int(a.get('lead_count') or 0)} rows, ${int(a['price_cents']) / 100:.2f}"
        when = datetime.fromisoformat(first.get(str(a.get("niche") or a["id"]), a["created_at"]))
        ET.SubElement(el, "pubDate").text = format_datetime(when if when.tzinfo else when.replace(tzinfo=timezone.utc))
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(rss, encoding="unicode") + "\n"
