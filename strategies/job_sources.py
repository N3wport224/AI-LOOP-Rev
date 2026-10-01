"""More public job sources (Phases 195-199): more postings in, more products out.

The product factory can only make as many distinct products as the data allows, so these widen
the intake. All are public feeds meant for reuse; each is fetched at most every
``SOURCE_MIN_HOURS`` (Remotive asks for no more than about four requests a day), robots.txt is
respected as for every source, and every row keeps its ``source`` and a link to the original.

* **Phase 195, Remotive** (``remotive``): the public remote-jobs API.
* **Phase 196, Jobicy** (``jobicy``): the public remote-jobs API (with yearly salary fields).
* **Phase 197, Himalayas** (``himalayas``): the public jobs API.
* **Phase 198, We Work Remotely** (``weworkremotely``): the programming-jobs RSS feed.
* **Phase 199, credit where it's due:** ``sources/`` on the site names every board the data comes
  from, with a link (the attribution these boards ask for), and the per-source cadence is kept in
  kv ``source_fetched_at``.

Turn one off by removing it from ``lead_sources``.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

from strategies.b2b_lead_aggregator import Lead, _clean, _note_shape, _ts

SOURCE_MIN_HOURS = {"remotive": 6, "jobicy": 6, "himalayas": 6, "weworkremotely": 6}
FETCHED = "source_fetched_at"
CREDITS = {
    "remoteok": ("Remote OK", "https://remoteok.com"),
    "arbeitnow": ("Arbeitnow", "https://www.arbeitnow.com"),
    "hn_hiring": ("Hacker News \"Who is hiring?\"", "https://news.ycombinator.com/submitted?id=whoishiring"),
    "remotive": ("Remotive", "https://remotive.com"),
    "jobicy": ("Jobicy", "https://jobicy.com"),
    "himalayas": ("Himalayas", "https://himalayas.app"),
    "weworkremotely": ("We Work Remotely", "https://weworkremotely.com"),
}


def _money(text: Any) -> list[int]:
    """"$80k - $100k" / "80,000-100,000 USD" → [80000, 100000]."""
    out = []
    for num, k in re.findall(r"(\d[\d,.]*)\s*([kK])?", str(text or "")):
        try:
            value = float(num.replace(",", ""))
        except ValueError:
            continue
        out.append(int(value * 1000) if k else int(value))
    return [v for v in out if v >= 1000][:2]


def _int(v: Any) -> int | None:
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return None
    return n or None


# ------------------------------------------------------------------ Phase 195: Remotive
def parse_remotive(payload: Any) -> list[Lead]:
    items = payload.get("jobs", []) if isinstance(payload, dict) else []
    leads = []
    for j in items:
        if not isinstance(j, dict) or not j.get("title") or not j.get("company_name"):
            continue
        pay = _money(j.get("salary"))
        leads.append(Lead(source="remotive", source_id=str(j.get("id") or ""), company=_clean(j.get("company_name")),
                          title=_clean(j.get("title")), url=str(j.get("url") or ""),
                          location=_clean(j.get("candidate_required_location")) or "Remote", remote=True,
                          tags=[str(t).lower() for t in (j.get("tags") or [])] + [str(j.get("category") or "").lower()],
                          description=_clean(j.get("description")), posted_at=_ts(j.get("publication_date")),
                          salary_min=pay[0] if pay else None, salary_max=pay[1] if len(pay) > 1 else None))
    _note_shape("remotive", items if isinstance(items, list) else [], len(leads))
    return leads


def fetch_remotive(http, depth: int = 1) -> list[Lead]:
    return parse_remotive(http.get_json("https://remotive.com/api/remote-jobs", params={"category": "software-dev"}))


# ------------------------------------------------------------------ Phase 196: Jobicy
def parse_jobicy(payload: Any) -> list[Lead]:
    items = payload.get("jobs", []) if isinstance(payload, dict) else []
    leads = []
    for j in items:
        if not isinstance(j, dict) or not j.get("jobTitle") or not j.get("companyName"):
            continue
        tags = [str(t).lower() for key in ("jobIndustry", "jobType") for t in (j.get(key) or []) if isinstance(t, str)]
        leads.append(Lead(source="jobicy", source_id=str(j.get("id") or ""), company=_clean(j.get("companyName")),
                          title=_clean(j.get("jobTitle")), url=str(j.get("url") or ""), location=_clean(j.get("jobGeo")) or "Remote",
                          remote=True, tags=tags, description=_clean(j.get("jobDescription") or j.get("jobExcerpt")),
                          posted_at=_ts(j.get("pubDate")), salary_min=_int(j.get("annualSalaryMin")),
                          salary_max=_int(j.get("annualSalaryMax"))))
    _note_shape("jobicy", items if isinstance(items, list) else [], len(leads))
    return leads


def fetch_jobicy(http, depth: int = 1) -> list[Lead]:
    return parse_jobicy(http.get_json("https://jobicy.com/api/v2/remote-jobs", params={"count": 50, "industry": "dev"}))


# ------------------------------------------------------------------ Phase 197: Himalayas
def parse_himalayas(payload: Any) -> list[Lead]:
    items = payload.get("jobs", []) if isinstance(payload, dict) else []
    leads = []
    for j in items:
        if not isinstance(j, dict) or not j.get("title") or not j.get("companyName"):
            continue
        where = j.get("locationRestrictions") or []
        when = j.get("pubDate")
        if isinstance(when, (int, float)):
            when = datetime.fromtimestamp(when, tz=timezone.utc).isoformat()
        level = j.get("seniority") or []
        leads.append(Lead(source="himalayas", source_id=str(j.get("guid") or j.get("applicationLink") or ""),
                          company=_clean(j.get("companyName")), title=_clean(j.get("title")),
                          url=str(j.get("applicationLink") or j.get("guid") or ""),
                          location=", ".join(map(str, where))[:120] if where else "Remote", remote=True,
                          tags=[str(t).lower() for t in (j.get("categories") or []) + (level if isinstance(level, list) else [])],
                          description=_clean(j.get("description") or j.get("excerpt")), posted_at=_ts(when),
                          salary_min=_int(j.get("minSalary")), salary_max=_int(j.get("maxSalary"))))
    _note_shape("himalayas", items if isinstance(items, list) else [], len(leads))
    return leads


def fetch_himalayas(http, depth: int = 1) -> list[Lead]:
    return parse_himalayas(http.get_json("https://himalayas.app/jobs/api", params={"limit": 100}))


# ------------------------------------------------------------------ Phase 198: We Work Remotely
def parse_wwr(xml_text: str) -> list[Lead]:
    if "<!DOCTYPE" in xml_text[:2048].upper() or "<!ENTITY" in xml_text.upper():
        raise ValueError("rss: DOCTYPE/ENTITY declarations are refused")
    root = ET.fromstring(xml_text)
    items = root.findall("./channel/item")
    leads = []
    for it in items:
        title = it.findtext("title") or ""
        if ":" not in title:
            continue
        company, role = (s.strip() for s in title.split(":", 1))
        when = it.findtext("pubDate") or ""
        try:
            from email.utils import parsedate_to_datetime

            when = parsedate_to_datetime(when).isoformat()
        except (TypeError, ValueError):
            when = ""
        leads.append(Lead(source="weworkremotely", source_id=it.findtext("guid") or it.findtext("link") or "",
                          company=_clean(company), title=_clean(role), url=it.findtext("link") or "",
                          location=_clean(it.findtext("region")) or "Remote", remote=True,
                          tags=[str(it.findtext("category") or "").lower()],
                          description=_clean(html.unescape(it.findtext("description") or "")), posted_at=_ts(when)))
    _note_shape("weworkremotely", [{"title": i.findtext("title")} for i in items], len(leads))
    return leads


def fetch_weworkremotely(http, depth: int = 1) -> list[Lead]:
    return parse_wwr(http.get("https://weworkremotely.com/categories/remote-programming-jobs.rss").text)


EXTRA_FETCHERS = {"remotive": fetch_remotive, "jobicy": fetch_jobicy, "himalayas": fetch_himalayas,
                  "weworkremotely": fetch_weworkremotely}


# ------------------------------------------------------------------ Phase 199: cadence and credits
def due(state: Any, source: str) -> bool:
    """The board's cadence (with its fixed offset, Phase 298) and any back-off after failures (Phase 297)."""
    from strategies.source_efficiency import due as efficient_due

    last = (state.get(FETCHED) or {}).get(source)
    return efficient_due(state, source, float(SOURCE_MIN_HOURS.get(source) or 0), last)


def fetched(state: Any, source: str) -> None:
    """Every board's last attempt is kept: cadence and back-off both use it."""
    stamps = dict(state.get(FETCHED) or {})
    stamps[source] = state.now()
    state.set(FETCHED, stamps)


def sources_page(cfg: Any, shell: Any) -> str:
    rows = "".join(f'<li><a href="{html.escape(url)}" rel="noopener">{html.escape(name)}</a></li>'
                   for key, (name, url) in CREDITS.items() if key in cfg.lead_sources)
    body = ("<h1>Where the data comes from</h1><p>Every dataset is built from public job postings on these boards. Every row "
            "keeps its source and a link to the original posting. Thank you to them.</p><ul>" + rows + "</ul>")
    return shell("Data sources", body, "The public job boards our hiring datasets are built from, with links: every row "
                                       "keeps its source.")
