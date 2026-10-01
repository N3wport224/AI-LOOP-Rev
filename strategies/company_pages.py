"""Company pages (Phases 380-384): people search for "<company> hiring"; give them a useful page.

* **Phase 380, which companies:** employers with at least ``MIN_ROLES`` (3) current postings, the
  ``MAX_PAGES`` (300) with the most. Staffing agencies and companies that asked to be left out
  (Phase 287) never get a page. Built from the same current postings as the products.
* **Phase 381, the page:** ``company/<slug>/``: open roles, example titles, locations, remote share,
  the technologies they ask for, the newest posting date, links to up to ``MAX_LINKS`` of their public
  postings (the original job boards), and the datasets about their technologies.
* **Phase 382, the index:** ``company/`` lists them A to Z; the home page links it; the sitemap
  includes every page.
* **Phase 383, structured data:** each page carries ``Organization`` data with the company's name.
  (No ``JobPosting`` data: the postings belong to their boards.)
* **Phase 384, always current:** pages are rebuilt with the site; a company that stops hiring loses
  its page at the next build (Phase 210 removes it from the site).
"""

from __future__ import annotations

import hashlib
import html
from collections import Counter, defaultdict
from typing import Any

MIN_ROLES = 3
MAX_PAGES = 300
MAX_LINKS = 10
INDEX = "company/index.html"


def _slug(name: str) -> str:
    from strategies.product_factory import _slug as slugify

    s = slugify(name)[:60].strip("-")
    return s or "c-" + hashlib.sha256(name.encode()).hexdigest()[:8]


# ------------------------------------------------------------------ Phase 380
def companies(state: Any, cfg: Any) -> list[dict[str, Any]]:
    from strategies.posting_quality import company_key, is_agency
    from strategies.product_factory import facets, fresh_leads
    from strategies.product_types import _is_remote

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for lead in fresh_leads(state, int(cfg.factory_max_age_days)):  # opt-outs, hygiene and still-listed already applied
        name = " ".join(str(lead.get("company") or "").split())
        if name and not is_agency(name):
            groups[company_key(name)].append(lead)
    out = []
    for rows in groups.values():
        if len(rows) < MIN_ROLES:
            continue
        name = Counter(" ".join(str(r["company"]).split()) for r in rows).most_common(1)[0][0]
        techs = Counter(t for r in rows for t in facets(r)["techs"])
        out.append({"name": name, "roles": len(rows), "titles": [t for t, _ in Counter(str(r.get("title") or "") for r in rows).most_common(8) if t],
                    "locations": [loc for loc, _ in Counter(str(r.get("location") or "") for r in rows).most_common(5) if loc],
                    "remote_pct": round(100 * sum(_is_remote(r) for r in rows) / len(rows)),
                    "techs": [t for t, _ in sorted(techs.items(), key=lambda kv: (-kv[1], kv[0]))[:10]],
                    "newest": max(str(r.get("posted_at") or "")[:10] for r in rows),
                    "links": [(str(r.get("title") or "Posting"), str(r["url"])) for r in sorted(rows, key=lambda r: str(r.get("posted_at") or ""), reverse=True)
                              if str(r.get("url") or "").startswith(("https://", "http://"))][:MAX_LINKS]})
    out.sort(key=lambda c: (-c["roles"], c["name"].lower()))
    used: set[str] = set()
    for c in out[:MAX_PAGES]:
        slug = _slug(c["name"])
        while slug in used:
            slug += "-" + hashlib.sha256(c["name"].encode()).hexdigest()[:4]
        used.add(slug)
        c["slug"] = slug
    return out[:MAX_PAGES]


# ------------------------------------------------------------------ Phases 381, 383
def page(c: dict[str, Any], products: dict[str, list[tuple[str, str]]], shell_at: Any) -> str:
    from strategies.product_factory import label
    from tools.page_builder import _jsonld_script

    esc = html.escape
    links = "".join(f'<li><a href="{esc(url)}" rel="nofollow noopener">{esc(title)}</a></li>' for title, url in c["links"])
    datasets = []
    for t in c["techs"]:
        for title, href in products.get(t, [])[:2]:
            if (title, href) not in datasets:
                datasets.append((title, href))
    data_html = "".join(f'<li><a href="../../{esc(href)}">{esc(title)}</a></li>' for title, href in datasets[:6])
    ld = {"@context": "https://schema.org", "@type": "Organization", "name": c["name"]}
    body = (f"<h1>{esc(c['name'])} is hiring</h1>"
            f"<p>{c['roles']} open engineering roles in current public job postings (newest {esc(c['newest'])}); "
            f"{c['remote_pct']}% remote.</p>"
            + (f"<h2>Roles</h2><ul>{''.join(f'<li>{esc(t)}</li>' for t in c['titles'])}</ul>" if c["titles"] else "")
            + (f"<p><b>Locations:</b> {esc(', '.join(c['locations']))}</p>" if c["locations"] else "")
            + (f"<p><b>Technologies:</b> {esc(', '.join(label(t) for t in c['techs']))}</p>" if c["techs"] else "")
            + (f"<h2>Their postings</h2><ul>{links}</ul>" if links else "")
            + (f"<h2>Datasets with companies like {esc(c['name'])}</h2><ul>{data_html}</ul>" if data_html else "")
            + '<p class="muted"><a href="../">All companies</a> · <a href="../../remove/">Is this your company and you\'d '
              "rather not be listed?</a></p>"
            + f'<script type="application/ld+json">{_jsonld_script(ld)}</script>')
    return shell_at(f"{c['name']} is hiring: {c['roles']} open roles", body,
                    f"{c['name']} has {c['roles']} open engineering roles: titles, locations, remote share and the "
                    "technologies they hire for, from public job postings.", 2)


# ------------------------------------------------------------------ Phases 382, 384
def site_files(state: Any, cfg: Any, pages: list[Any], shell_at: Any) -> dict[str, str]:
    from strategies.site_discovery import factory_meta

    items = companies(state, cfg)
    if not items:
        return {}
    meta = factory_meta(state)
    on_sale = {p.slug: p.title for p in pages if getattr(p, "checkout_url", "")}
    products: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for slug, m in sorted(meta.items()):
        if slug in on_sale and m.get("tech"):
            products[m["tech"]].append((on_sale[slug], f"{slug}/"))
    out = {f"company/{c['slug']}/index.html": page(c, products, shell_at) for c in items}
    listing = "".join(f'<li><a href="{html.escape(c["slug"])}/">{html.escape(c["name"])}</a> ({c["roles"]})</li>'
                      for c in sorted(items, key=lambda c: c["name"].lower()))
    out[INDEX] = shell_at("Companies hiring engineers", f"<h1>Companies hiring engineers</h1><p>{len(items)} companies with "
                          f"{MIN_ROLES} or more open roles in current public job postings.</p><ul>{listing}</ul>",
                          "Companies hiring software engineers now, A to Z, with their open roles and technologies.", 1)
    return out
