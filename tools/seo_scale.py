"""SEO at scale (Phases 175-179): hundreds of product pages that each deserve to rank.

* **Phase 175, unique content per page:** every factory product page carries its own numbers
  (postings, companies, the companies with the most roles, top locations, remote share), stored
  with the product when it's built, so pages are never thin copies of each other.
* **Phase 176, technology hubs:** ``hiring/<tech>/`` lists every dataset about one technology with
  its size and price; ``hiring/`` lists the technologies. Buyers compare, search engines find the
  whole family through one page.
* **Phase 177, answer pages:** ``answers/how-many-companies-are-hiring-<tech>-engineers/`` answers
  the question people type, with the current numbers, the date, ``FAQPage`` data and links to the
  datasets.
* **Phase 178, breadcrumbs and links:** product pages get ``BreadcrumbList`` data (Home › tech hub
  › product) and a "More <tech> datasets" link; hubs link to the intel page for the same
  technology when there is one.
* **Phase 179, Google Dataset Search:** factory product pages also describe themselves as a
  ``Dataset`` (name, description, size, format, date), the markup Google Dataset Search reads.
"""

from __future__ import annotations

import html
import json
from collections import Counter
from typing import Any

HUB_DIR = "hiring"
ANSWER_DIR = "answers"


def tech_slug(tech: str) -> str:
    from strategies.product_factory import _slug

    return _slug(tech)


def hub_path(tech: str) -> str:
    return f"{HUB_DIR}/{tech_slug(tech)}/"


def answer_path(tech: str) -> str:
    return f"{ANSWER_DIR}/how-many-companies-are-hiring-{tech_slug(tech)}-engineers/"


# ------------------------------------------------------------------ Phase 175
def insight(rows: list[dict[str, Any]]) -> dict[str, Any]:
    companies = Counter(" ".join(str(r.get("company") or "").split()) for r in rows if r.get("company"))
    locations = Counter(str(r.get("location") or "").strip() for r in rows if str(r.get("location") or "").strip())
    remote = sum(1 for r in rows if str(r.get("remote")).lower() in ("true", "1", "yes") or "remote" in str(r.get("location") or "").lower())
    return {"rows": len(rows), "companies": len(companies), "top_companies": companies.most_common(5),
            "top_locations": locations.most_common(4), "remote_pct": round(remote * 100 / len(rows)) if rows else 0}


def insight_html(title: str, data: dict[str, Any], updated: str = "") -> str:
    if not data or not data.get("rows"):
        return ""
    tops = ", ".join(f"{html.escape(str(n))} ({c})" for n, c in data.get("top_companies", [])[:5])
    places = ", ".join(html.escape(str(p)) for p, _ in data.get("top_locations", [])[:4])
    text = (f"<p><b>In this dataset{f' (updated {html.escape(updated[:10])})' if updated else ''}:</b> "
            f"{int(data['rows']):,} job postings from {int(data['companies']):,} companies; {int(data.get('remote_pct', 0))}% "
            "remote.")
    if tops:
        text += f" Most open roles: {tops}."
    if places:
        text += f" Top locations: {places}."
    return text + "</p>"


# ------------------------------------------------------------------ Phase 178-179
def breadcrumbs_jsonld(base_url: str, tech_label: str, tech: str, title: str, slug: str) -> dict[str, Any]:
    base = base_url.rstrip("/")
    items = [("Home", f"{base}/"), (f"{tech_label} datasets", f"{base}/{hub_path(tech)}"), (title, f"{base}/{slug}/")]
    return {"@context": "https://schema.org", "@type": "BreadcrumbList",
            "itemListElement": [{"@type": "ListItem", "position": i, "name": n, "item": u} for i, (n, u) in enumerate(items, 1)]}


def dataset_jsonld(page: Any, url: str, brand: str, data: dict[str, Any]) -> dict[str, Any]:
    desc = page.summary if len(page.summary) >= 50 else f"{page.summary} Built from public job postings, refreshed weekly."
    out: dict[str, Any] = {
        "@context": "https://schema.org", "@type": "Dataset", "name": page.title, "description": desc[:5000],
        "creator": {"@type": "Organization", "name": brand}, "isAccessibleForFree": False,
        "keywords": ["hiring", "job postings", "tech stack", page.title],
        "distribution": [{"@type": "DataDownload", "encodingFormat": "text/csv", "contentUrl": url or page.checkout_url}],
        "variableMeasured": ["company", "title", "location", "remote", "seniority", "stack", "posted_at"],
    }
    if url.startswith("http"):
        out["url"] = url
    if page.updated_at:
        out["dateModified"] = page.updated_at[:10]
    if data.get("rows"):
        out["size"] = f"{int(data['rows'])} rows"
    return out


def script(data: dict[str, Any]) -> str:
    return '<script type="application/ld+json">' + json.dumps(data, ensure_ascii=False).replace("</", "<\\/") + "</script>"


# ------------------------------------------------------------------ Phase 176
def hub_pages(pages: list[Any], techs: dict[str, str], labels: dict[str, str], shell: Any,
              intel_paths: dict[str, str] | None = None) -> dict[str, str]:
    """``techs``: page slug → technology. Returns {path: html} for every hub and the hub index."""
    by_tech: dict[str, list[Any]] = {}
    for p in pages:
        tech = techs.get(p.slug)
        if tech and p.checkout_url:
            by_tech.setdefault(tech, []).append(p)
    out = {}
    index_items = []
    for tech, items in sorted(by_tech.items()):
        label = labels.get(tech, tech)
        rows = "".join(f'<tr><td><a href="../../{html.escape(p.slug)}/">{html.escape(p.title)}</a></td>'
                       f'<td>{html.escape(str((p.metrics or {}).get("rows") or ""))}</td><td>${p.price_cents / 100:.2f}</td></tr>'
                       for p in sorted(items, key=lambda p: p.title))
        intel = (intel_paths or {}).get(tech)
        body = (f"<h1>{html.escape(label)} hiring datasets</h1><p>Every dataset about companies hiring {html.escape(label)} "
                f"engineers: by region, seniority and remote, plus salary benchmarks and company shortlists.</p>"
                '<div class="wrap"><table><thead><tr><th>Dataset</th><th>Rows</th><th>Price</th></tr></thead><tbody>'
                + rows + "</tbody></table></div>"
                + f'<p><a href="../../{answer_path(tech)}">How many companies are hiring {html.escape(label)} engineers?</a></p>'
                + (f'<p><a href="../../{html.escape(intel)}">Hiring intel for {html.escape(label)}</a></p>' if intel else ""))
        out[f"{hub_path(tech)}index.html"] = shell(f"{label} hiring datasets", body,
                                                   f"Datasets of companies hiring {label} engineers: by region, seniority and "
                                                   "remote, with salaries and company shortlists. Updated weekly.", 2)
        index_items.append(f'<li><a href="{html.escape(tech_slug(tech))}/">{html.escape(label)}</a> ({len(items)})</li>')
    if index_items:
        out[f"{HUB_DIR}/index.html"] = shell("Hiring datasets by technology",
                                             "<h1>Hiring datasets by technology</h1><ul>" + "".join(index_items) + "</ul>",
                                             "Every hiring dataset grouped by technology: pick a stack, then a region, "
                                             "seniority or format.", 1)
    return out


# ------------------------------------------------------------------ Phase 177
def answer_pages(counts: dict[str, dict[str, int]], labels: dict[str, str], hubs: set[str], date: str, shell: Any) -> dict[str, str]:
    """``counts``: tech → {"companies", "roles"} from the current postings; only technologies with a hub."""
    out = {}
    for tech, c in sorted(counts.items()):
        if tech not in hubs or not c.get("companies"):
            continue
        label = labels.get(tech, tech)
        q = f"How many companies are hiring {label} engineers?"
        a = (f"As of {date}, {c['companies']:,} companies have {c['roles']:,} open roles mentioning {label} in public job "
             "postings we track, refreshed daily.")
        faq = {"@context": "https://schema.org", "@type": "FAQPage",
               "mainEntity": [{"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}}]}
        body = (f"<h1>{html.escape(q)}</h1><p><b>{html.escape(a)}</b></p><p>The numbers come from public job boards; a company "
                "counts once however many roles it posts.</p>"
                f'<p><a href="../../{hub_path(tech)}">All {html.escape(label)} hiring datasets</a> with the companies, roles and '
                "links.</p>" + script(faq))
        out[f"{answer_path(tech)}index.html"] = shell(q, body, a[:158], 2)
    return out


def tech_counts(state: Any, max_age_days: int) -> dict[str, dict[str, int]]:
    from strategies.product_factory import facets, fresh_leads

    roles: Counter = Counter()
    companies: dict[str, set[str]] = {}
    for lead in fresh_leads(state, max_age_days):
        for t in facets(lead)["techs"]:
            roles[t] += 1
            companies.setdefault(t, set()).add(str(lead.get("company") or "").strip().lower())
    return {t: {"roles": roles[t], "companies": len(companies[t])} for t in roles}
