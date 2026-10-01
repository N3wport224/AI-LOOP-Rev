"""Technology comparisons (Phases 330-334): "Rust vs Go: who's hiring" pages, from real numbers.

* **Phase 330, which pairs:** technologies people actually weigh against each other (``GROUPS``:
  languages, frontend frameworks, clouds, databases, mobile, infrastructure), when both have at
  least ``MIN_POSTINGS`` (30) postings in the last 30 days. At most ``MAX_PAGES`` pages, busiest first.
* **Phase 331, the page:** ``vs/<a>-vs-<b>/`` puts side by side: postings and companies in 30 days,
  remote share, senior share, median stated salary (when at least ``MIN_SALARIES`` postings state
  one), top countries, whether each is rising or cooling, and how many postings ask for both. Each
  side links to its datasets.
* **Phase 332, the index:** ``vs/`` lists every comparison; the home page links it and the sitemap
  includes them all.
* **Phase 333, week on week:** numbers are kept weekly (kv ``tech_compare``), so each page also shows
  the change in postings since the previous week.
* **Phase 334, answers search engines can read:** each page carries ``FAQPage`` data ("Which has
  more openings?", "Which is more often remote?", "Which pays more?"), answered from the same numbers.
"""

from __future__ import annotations

import html
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from itertools import combinations
from statistics import median
from typing import Any

GROUPS = [
    ("python", "golang", "rust", "java", "kotlin", "scala", "ruby", "php", "c#", "c++", "elixir", "typescript", "javascript"),
    ("react", "vue", "angular", "next.js"),
    ("aws", "gcp", "azure"),
    ("postgres", "mysql", "mongodb", "redis"),
    ("swift", "kotlin", "flutter", "ios", "android"),
    ("kubernetes", "docker", "terraform"),
    ("django", "flask", "fastapi", "rails", "laravel"),
    ("pytorch", "tensorflow"),
]
MIN_POSTINGS = 30
MIN_SALARIES = 10
MAX_PAGES = 40
KEY = "tech_compare"
DAYS = 30


def _recent(state: Any) -> list[tuple[dict[str, Any], set[str]]]:
    from strategies.market_trends import _when
    from strategies.pool_cache import pool
    from strategies.product_factory import facets

    since = state.clock() - timedelta(days=DAYS)
    out = []
    for lead in pool(state):
        when = _when(lead)
        if when is not None and when >= since:
            out.append((lead, facets(lead)["techs"]))
    return out


# ------------------------------------------------------------------ Phase 331 (numbers)
def stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    from strategies.kinds_countries import _plain, country_of
    from strategies.posting_quality import company_key
    from strategies.product_types import _is_remote, _yearly

    n = len(rows)
    mids = []
    for r in rows:
        lo, hi = _yearly(r.get("salary_min")), _yearly(r.get("salary_max"))
        if lo or hi:
            mids.append(((lo or hi) + (hi or lo)) / 2)
    countries = Counter(c for r in rows for c in country_of(r))
    return {"postings": n, "companies": len({company_key(r.get("company") or "") for r in rows if r.get("company")}),
            "remote_pct": round(100 * sum(_is_remote(r) for r in rows) / n) if n else 0,
            "senior_pct": round(100 * sum(str(r.get("seniority") or "") == "senior" for r in rows) / n) if n else 0,
            "median_salary": round(median(mids)) if len(mids) >= MIN_SALARIES else None,
            "countries": [_plain(c) for c, _ in countries.most_common(3)], "salaries": len(mids)}


# ------------------------------------------------------------------ Phase 330
def pairs(state: Any) -> list[dict[str, Any]]:
    recent = _recent(state)
    by_tech: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for lead, techs in recent:
        for t in techs:
            by_tech[t].append(lead)
    seen, out = set(), []
    for group in GROUPS:
        for a, b in combinations(group, 2):
            a, b = sorted((a, b))
            if (a, b) in seen or len(by_tech.get(a, [])) < MIN_POSTINGS or len(by_tech.get(b, [])) < MIN_POSTINGS:
                continue
            seen.add((a, b))
            both = sum(1 for _, techs in recent if a in techs and b in techs)
            out.append({"a": a, "b": b, "sa": stats(by_tech[a]), "sb": stats(by_tech[b]), "both": both})
    out.sort(key=lambda p: (-(p["sa"]["postings"] + p["sb"]["postings"]), p["a"], p["b"]))
    return out[:MAX_PAGES]


def path(a: str, b: str) -> str:
    from strategies.product_factory import _slug

    return f"vs/{_slug(a)}-vs-{_slug(b)}/index.html"


# ------------------------------------------------------------------ Phase 333
def week_on_week(state: Any, items: list[dict[str, Any]]) -> dict[str, int]:
    """Postings per technology now vs the snapshot from a week ago (snapshots taken weekly)."""
    rec = dict(state.get(KEY) or {})
    now = state.clock()
    current = {}
    for p in items:
        current[p["a"]] = p["sa"]["postings"]
        current[p["b"]] = p["sb"]["postings"]
    previous = rec.get("previous") or {}
    if not rec.get("at") or now - datetime.fromisoformat(rec["at"]) >= timedelta(days=7):
        state.set(KEY, {"at": state.now(), "previous": rec.get("current") or {}, "current": current})
        previous = rec.get("current") or {}
    return {t: n - previous[t] for t, n in current.items() if t in previous}


# ------------------------------------------------------------------ Phases 331, 334
def _faq(a: str, b: str, sa: dict[str, Any], sb: dict[str, Any]) -> list[tuple[str, str]]:
    def more(x: int, y: int, what: str) -> str:
        if x == y:
            return f"They're level: {x} {what} each."
        hi, lo = (a, b) if x > y else (b, a)
        return f"{hi}: {max(x, y)} {what} against {min(x, y)} for {lo}."

    qs = [(f"Which has more job openings, {a} or {b}?", more(sa["postings"], sb["postings"], f"postings in the last {DAYS} days")),
          (f"Which is more often remote, {a} or {b}?", f"{a}: {sa['remote_pct']}% remote; {b}: {sb['remote_pct']}% remote.")]
    if sa["median_salary"] and sb["median_salary"]:
        qs.append((f"Which pays more, {a} or {b}?", f"Median stated salary: {a} ${sa['median_salary']:,}; "
                                                    f"{b} ${sb['median_salary']:,} (postings that state one)."))
    return qs


def page(p: dict[str, Any], change: dict[str, int], links: dict[str, str], shell_at: Any, updated: str) -> str:
    from strategies.product_factory import label
    from tools.page_builder import _jsonld_script

    a, b, sa, sb = label(p["a"]), label(p["b"]), p["sa"], p["sb"]
    trend = p.get("trend") or {}

    def cell(v: Any) -> str:
        return html.escape(str(v)) if v not in (None, "", []) else "–"

    def ch(t: str) -> str:
        d = change.get(t)
        return "" if d is None else f" ({d:+d} vs last week)"

    rows = [("Postings, last 30 days", f"{sa['postings']}{ch(p['a'])}", f"{sb['postings']}{ch(p['b'])}"),
            ("Companies hiring", sa["companies"], sb["companies"]), ("Remote", f"{sa['remote_pct']}%", f"{sb['remote_pct']}%"),
            ("Senior roles", f"{sa['senior_pct']}%", f"{sb['senior_pct']}%"),
            ("Median stated salary", f"${sa['median_salary']:,}" if sa["median_salary"] else "", f"${sb['median_salary']:,}"
             if sb["median_salary"] else ""), ("Top countries", ", ".join(sa["countries"]), ", ".join(sb["countries"])),
            ("Trend", trend.get(p["a"], ""), trend.get(p["b"], ""))]
    table = "".join(f"<tr><th>{html.escape(k)}</th><td>{cell(x)}</td><td>{cell(y)}</td></tr>" for k, x, y in rows)
    faq = _faq(a, b, sa, sb)
    faq_html = "".join(f"<h3>{html.escape(q)}</h3><p>{html.escape(ans)}</p>" for q, ans in faq)
    ld = {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
        {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": ans}} for q, ans in faq]}
    more = "".join(f'<li><a href="../../{html.escape(href)}">{html.escape(text)}</a></li>' for text, href in links.items())
    body = (f"<h1>{html.escape(a)} vs {html.escape(b)}: who's hiring</h1><p>From public job postings in the last {DAYS} days, "
            f"updated {html.escape(updated)}. {p['both']} postings ask for both.</p><div style=\"overflow-x:auto\"><table><thead>"
            f"<tr><th></th><th>{html.escape(a)}</th><th>{html.escape(b)}</th></tr></thead><tbody>{table}</tbody></table></div>"
            f"<h2>Questions</h2>{faq_html}" + (f"<h2>The datasets</h2><ul>{more}</ul>" if more else "")
            + '<p><a href="../">All comparisons</a></p>'
            + f'<script type="application/ld+json">{_jsonld_script(ld)}</script>')
    return shell_at(f"{a} vs {b}: who's hiring", body, f"{a} vs {b} job market: openings, companies, remote share, salaries "
                                                       "and trends, from public job postings.", 2)


def site_files(state: Any, pages: list[Any], shell_at: Any) -> dict[str, str]:
    from strategies.market_trends import trends
    from strategies.product_factory import label
    from strategies.site_discovery import factory_meta

    items = pairs(state)
    if not items:
        return {}
    t = trends(state)
    trend = {r["tech"]: "rising" for r in t.get("rising") or []} | {r["tech"]: "cooling" for r in t.get("falling") or []}
    change = week_on_week(state, items)
    meta = factory_meta(state)
    on_sale = {p.slug: p.title for p in pages if getattr(p, "checkout_url", "")}
    by_tech: dict[str, dict[str, str]] = defaultdict(dict)
    for slug, m in meta.items():
        if slug in on_sale and m.get("tech") and len(by_tech[m["tech"]]) < 4:
            by_tech[m["tech"]][on_sale[slug]] = f"{slug}/"
    out = {}
    updated = state.now()[:10]
    for p in items:
        p["trend"] = trend
        out[path(p["a"], p["b"])] = page(p, change, {**by_tech.get(p["a"], {}), **by_tech.get(p["b"], {})}, shell_at, updated)
    listing = "".join(f'<li><a href="{html.escape(path(p["a"], p["b"])[3:-len("index.html")])}">{html.escape(label(p["a"]))} vs '
                      f'{html.escape(label(p["b"]))}</a> ({p["sa"]["postings"]} vs {p["sb"]["postings"]} postings)</li>' for p in items)
    out["vs/index.html"] = shell_at("Compare technologies", f"<h1>Compare technologies</h1><p>Side by side, from the job "
                                    f"postings of the last {DAYS} days.</p><ul>{listing}</ul>",
                                    "Technology job markets side by side: openings, remote share, salaries and trends.", 1)
    return out
