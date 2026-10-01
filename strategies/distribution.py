"""Distribution (Phases 185-189): get the data in front of people where they already are.

* **Phase 185, embeddable badges:** ``badges/<tech>.svg`` ("Rust hiring: 24 companies"), rebuilt
  with every site build, and ``embed/`` with copy-paste snippets. Bloggers and communities embed
  them; every embed links back with ``utm_source=embed``.
* **Phase 186, answer drafts** (draft play ``hn_answers``, every 2 days): recent Hacker News stories
  and comments asking about hiring for a technology the data covers (public Algolia API). The agent
  drafts a short, useful reply with the actual numbers and one link; you decide whether to post it.
* **Phase 187, directories:** ``DIRECTORIES`` lists places worth submitting the catalog to (data
  marketplaces, product directories, awesome-lists). Each gets a ready-made blurb;
  ``automonetize marketing directories`` shows them and ``... directories done <name>`` ticks one
  off. Until two are done, the to-do list reminds you.
* **Phase 188, data story** (draft play ``data_story``, monthly): a short press-style story with
  the month's numbers (most-hired technologies, remote share) to pitch to journalists or
  bloggers who cover tech hiring. Draft only: you choose who gets it.
* **Phase 189, hiring heatmap:** ``tools/hiring-heatmap/``: a free page with companies hiring per
  technology and region, updated every build. Useful on its own, so people link to it.
"""

from __future__ import annotations

import html
from collections import Counter, defaultdict
from datetime import timedelta
from typing import Any

BADGE_DIR = "badges"
EMBED = "embed/index.html"
HEATMAP = "tools/hiring-heatmap/index.html"
DIR_KEY = "directories_done"
DIRECTORIES = {
    "datarade": ("Datarade (data marketplace)", "https://datarade.ai"),
    "aws_data_exchange": ("AWS Data Exchange", "https://aws.amazon.com/data-exchange/"),
    "kaggle": ("Kaggle Datasets (free teaser)", "https://www.kaggle.com/datasets"),
    "huggingface": ("Hugging Face Datasets (free teaser)", "https://huggingface.co/datasets"),
    "data_world": ("data.world (free teaser)", "https://data.world"),
    "awesome_public_datasets": ("awesome-public-datasets (GitHub pull request)", "https://github.com/awesomedata/awesome-public-datasets"),
    "producthunt": ("Product Hunt", "https://www.producthunt.com/posts/new"),
    "indiehackers": ("Indie Hackers products", "https://www.indiehackers.com/products"),
    "saashub": ("SaaSHub", "https://www.saashub.com"),
    "alternativeto": ("AlternativeTo", "https://alternativeto.net"),
    "betalist": ("BetaList", "https://betalist.com"),
    "gumroad_discover": ("Gumroad Discover (with marketplace-export kits)", "https://gumroad.com"),
}
HN_QUERIES = ("companies hiring", "who is hiring", "job market for", "hard to find a job")


def _counts(state: Any, cfg: Any) -> dict[str, dict[str, int]]:
    from tools.seo_scale import tech_counts

    return tech_counts(state, int(cfg.factory_max_age_days))


# ------------------------------------------------------------------ Phase 185
def badge_techs(state: Any, cfg: Any) -> list[str]:
    return sorted(t for t, c in _counts(state, cfg).items() if c["companies"] >= int(cfg.factory_min_companies))


def badge_path(tech: str) -> str:
    from tools.seo_scale import tech_slug

    return f"{BADGE_DIR}/{tech_slug(tech)}.svg"


def badges(state: Any, cfg: Any) -> dict[str, str]:
    from strategies.product_factory import label
    from tools.seo_assets import render_badge_svg

    counts = _counts(state, cfg)
    return {badge_path(t): render_badge_svg(f"{label(t)} hiring", f"{counts[t]['companies']} companies") for t in badge_techs(state, cfg)}


def embed_page(cfg: Any, techs: list[str], shell: Any) -> str:
    from strategies.product_factory import label
    from tools.seo_scale import hub_path, tech_slug

    site = str(cfg.pages_base_url or "").rstrip("/")
    blocks = []
    for tech in techs:
        path = badge_path(tech)
        link = f"{site}/{hub_path(tech)}?utm_source=embed&utm_medium=badge&utm_campaign={tech_slug(tech)}"
        snippet = f'<a href="{link}"><img src="{site}/{path}" alt="{label(tech)} hiring: live count of companies"></a>'
        blocks.append(f'<h2>{html.escape(label(tech))}</h2><p><img src="../{html.escape(path)}" alt="{html.escape(label(tech))} '
                      f'hiring badge"></p><pre><code>{html.escape(snippet)}</code></pre>')
    body = ("<h1>Embed live hiring numbers</h1><p>Free badges with the current number of companies hiring for a technology, "
            "updated every day. Paste a snippet into your blog, README or community page.</p>" + "".join(blocks))
    return shell("Embed live hiring numbers", body, "Free embeddable badges showing how many companies are hiring for each "
                                                    "technology, updated daily.")


# ------------------------------------------------------------------ Phase 186
def find_questions(tools: Any, techs: list[str], days: int = 7, limit: int = 5) -> list[dict[str, Any]]:
    from tools.syndication.hn_algolia_tracker import ALGOLIA

    since = int((tools.state.clock() - timedelta(days=days)).timestamp())
    seen = set(tools.state.get("hn_answered") or [])
    found = []
    for query in HN_QUERIES:
        try:
            data = tools.http.get_json(f"{ALGOLIA}/search_by_date", params={"query": query, "tags": "(story,comment)",
                                                                            "numericFilters": f"created_at_i>{since}",
                                                                            "hitsPerPage": 20}, check_robots=False) or {}
        except Exception:  # noqa: BLE001 - a search failing just means fewer drafts today
            continue
        for hit in data.get("hits") or []:
            text = " ".join(str(hit.get(k) or "") for k in ("title", "story_title", "comment_text")).lower()
            tech = next((t for t in techs if f" {t} " in f" {text} "), "")
            oid = str(hit.get("objectID") or "")
            if tech and oid and oid not in seen:
                found.append({"id": oid, "tech": tech, "url": f"https://news.ycombinator.com/item?id={oid}",
                              "title": hit.get("title") or hit.get("story_title") or ""})
                seen.add(oid)
            if len(found) >= limit:
                return found
    return found


def hn_answers(tools: Any, play_id: int) -> dict[str, Any] | None:
    from strategies.marketing_engine import tagged
    from strategies.product_factory import label
    from tools.seo_scale import hub_path

    state, cfg = tools.state, tools.config
    counts = _counts(state, cfg)
    techs = sorted((t for t, c in counts.items() if c["companies"] >= int(cfg.factory_min_companies)), key=len, reverse=True)
    if not techs or not cfg.pages_base_url:
        return None
    questions = find_questions(tools, techs)
    if not questions:
        return None
    state.set("hn_answered", (list(state.get("hn_answered") or []) + [q["id"] for q in questions])[-500:])
    site = cfg.pages_base_url.rstrip("/")
    parts = []
    for q in questions:
        c = counts[q["tech"]]
        link = tagged(f"{site}/{hub_path(q['tech'])}", "hn", play_id)
        parts.append(f"{q['url']}  ({q['title'][:80]})\nDraft reply: From public postings this month, {c['companies']} companies "
                     f"have {c['roles']} open roles mentioning {label(q['tech'])}. Breakdown by region and seniority: {link}")
    return {"title": f"{len(questions)} Hacker News thread(s) the data can answer", "link": "",
            "body": "Only reply where it genuinely helps; HN dislikes promotion.\n\n" + "\n\n".join(parts)}


# ------------------------------------------------------------------ Phase 187
def directory_blurb(state: Any, cfg: Any) -> str:
    from strategies.revenue_models import live_products

    n = len([a for a in live_products(state) if a.get("checkout_url")])
    site = str(cfg.pages_base_url or "").rstrip("/") or "(your site)"
    return (f"{cfg.site_title}: {n} datasets of companies hiring engineers right now, by technology, region and seniority, "
            f"built from public job postings and refreshed weekly. CSV, Excel, JSON and SQL. {site}/?utm_source=directory")


def directories(state: Any) -> list[dict[str, Any]]:
    done = state.get(DIR_KEY) or {}
    return [{"key": k, "name": n, "url": u, "done": done.get(k, "")} for k, (n, u) in DIRECTORIES.items()]


def mark_directory(state: Any, key: str) -> bool:
    if key not in DIRECTORIES:
        return False
    done = dict(state.get(DIR_KEY) or {})
    done[key] = state.now()
    state.set(DIR_KEY, done)
    return True


# ------------------------------------------------------------------ Phase 188
def data_story(tools: Any, play_id: int) -> dict[str, Any] | None:
    from strategies.marketing_engine import tagged
    from strategies.product_factory import facets, fresh_leads, label

    state, cfg = tools.state, tools.config
    counts = _counts(state, cfg)
    if len(counts) < 3 or not cfg.pages_base_url:
        return None
    top = sorted(counts.items(), key=lambda kv: -kv[1]["companies"])[:5]
    leads = fresh_leads(state, 30)
    remote = round(100 * sum(1 for lead in leads if "remote" in facets(lead)["regions"]) / len(leads)) if leads else 0
    link = tagged(f"{cfg.pages_base_url.rstrip('/')}/tools/hiring-heatmap/", "press", play_id)
    lines = [f"{state.clock():%B %Y}: which engineers are companies hiring?", "",
             f"From {len(leads):,} public job postings in the last 30 days:"]
    lines += [f"- {label(t)}: {c['companies']} companies, {c['roles']} open roles" for t, c in top]
    lines += [f"- {remote}% of roles are remote", "", f"Full table by technology and region (free to cite with a link): {link}", "",
              "Pitch it to journalists or bloggers who cover tech hiring, one at a time and personally; never as a bulk mail."]
    return {"title": f"Data story for {state.clock():%B}: most-hired technologies", "body": "\n".join(lines), "link": link}


# ------------------------------------------------------------------ Phase 189
def heatmap_page(state: Any, cfg: Any, shell: Any) -> str:
    from strategies.product_factory import facets, fresh_leads, label

    leads = fresh_leads(state, int(cfg.factory_max_age_days))
    grid: dict[str, Counter] = defaultdict(Counter)
    for lead in leads:
        f = facets(lead)
        for t in f["techs"]:
            grid[t]["all"] += 1
            for region in f["regions"]:
                grid[t][region] += 1
    rows = sorted(grid.items(), key=lambda kv: -kv[1]["all"])[:40]
    if not rows:
        body = "<h1>Hiring heatmap</h1><p>The first numbers appear after the next data collection.</p>"
    else:
        peak = max(c["all"] for _, c in rows)

        def cell(n: int) -> str:
            shade = int(235 - 160 * (n / peak)) if peak else 235
            return f'<td style="background:rgb({shade},{shade + 10 if shade < 245 else 245},255)">{n}</td>'

        body_rows = "".join(f"<tr><th scope=\"row\">{html.escape(label(t))}</th>" + "".join(cell(c[k]) for k in ("all", "us", "europe", "remote"))
                            + "</tr>" for t, c in rows)
        body = (f"<h1>Hiring heatmap</h1><p>Open roles per technology and region in public job postings from the last "
                f"{int(cfg.factory_max_age_days)} days ({len(leads):,} postings), updated daily. Free to cite with a link.</p>"
                '<div class="wrap"><table><thead><tr><th>Technology</th><th>All</th><th>US</th><th>Europe</th><th>Remote</th>'
                "</tr></thead><tbody>" + body_rows + "</tbody></table></div>")
    return shell("Hiring heatmap", body, "Open roles per technology and region from current public job postings, updated daily: "
                                         "free to cite.")
