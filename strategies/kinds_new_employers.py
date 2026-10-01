"""New employers (Phases 265-269): companies that just started hiring.

A company's first posting in the agent's data marks when it started hiring. That only means
something once the data goes back far enough, so nothing is called new until the postings cover
``HISTORY_DAYS`` (60) days.

* **Phase 265, who's new:** companies whose first posting is within the last ``NEW_DAYS`` (30)
  days (``new_employers``), with their roles and technologies. Staffing agencies are left out.
* **Phase 266, new-employer lists** (``new-employers[-<tech>]``, $9): one row per new company,
  e.g. "Companies That Just Started Hiring Rust Engineers". Needs ``MIN_COMPANIES`` companies.
* **Phase 267, on the site:** ``new-employers/`` lists the companies that started hiring in the last
  30 days (public company names and counts, from public postings).
* **Phase 268, a feed:** ``feeds/new-employers.xml``, one item per new company.
* **Phase 269, in Monday's report:** how many companies started hiring in the past week, with a few
  names.
"""

from __future__ import annotations

import html
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any

from strategies.product_kinds import COMPANY_FILES, Kind, build_companies, company_candidate, register

HISTORY_DAYS = 60
NEW_DAYS = 30
MIN_COMPANIES = 8
PRICE = 900
PAGE = "new-employers/index.html"
FEED = "feeds/new-employers.xml"


# ------------------------------------------------------------------ Phase 265
def new_employers(leads: list[dict[str, Any]], now: datetime, days: int = NEW_DAYS) -> dict[str, dict[str, Any]]:
    """company key → {"name", "first", "postings": [...]}; empty while the data is too young."""
    from strategies.market_trends import _when
    from strategies.posting_quality import company_key, is_agency

    dated = [(lead, _when(lead)) for lead in leads if lead.get("company")]
    dated = [(lead, t) for lead, t in dated if t is not None]
    if not dated or now - min(t for _, t in dated) < timedelta(days=HISTORY_DAYS):
        return {}
    groups: dict[str, list[tuple[dict[str, Any], datetime]]] = defaultdict(list)
    for lead, t in dated:
        if not is_agency(lead["company"]):
            groups[company_key(lead["company"])].append((lead, t))
    since = now - timedelta(days=days)
    out = {}
    for key, items in groups.items():
        first = min(t for _, t in items)
        if since <= first <= now:
            out[key] = {"name": items[0][0]["company"], "first": first, "postings": [lead for lead, _ in items]}
    return out


# ------------------------------------------------------------------ Phase 266
def candidates(tagged: list[tuple[dict[str, Any], dict[str, Any]]], cfg: Any, state: Any) -> list[dict[str, Any]]:
    from strategies.product_factory import _slug, label

    from strategies.b2b_lead_aggregator import POOL_NICHE

    fresh = new_employers(state.leads_for_niche(POOL_NICHE), state.clock())  # the whole history, not just recent postings
    if not fresh:
        return []
    current = {str(lead.get("dedupe_key")) for lead, _ in tagged}
    rows = [p for e in fresh.values() for p in e["postings"] if str(p.get("dedupe_key")) in current]
    out = []
    cand = company_candidate("new_employers", "new-employers", "Companies That Just Started Hiring Engineers", rows,
                             f"that posted their first engineering role in the last {NEW_DAYS} days", PRICE,
                             min_companies=MIN_COMPANIES)
    if cand:
        out.append(cand)
    by_tech: dict[str, list[dict[str, Any]]] = defaultdict(list)
    from strategies.product_factory import facets

    for p in rows:
        for t in facets(p)["techs"]:
            by_tech[t].append(p)
    for tech, sub in by_tech.items():
        cand = company_candidate("new_employers", f"new-employers-{_slug(tech)}",
                                 f"Companies That Just Started Hiring {label(tech)} Engineers", sub,
                                 f"that started hiring {label(tech)} engineers in the last {NEW_DAYS} days", PRICE, {"tech": tech},
                                 min_companies=MIN_COMPANIES)
        if cand:
            cand["score"] -= 2
            out.append(cand)
    return out


# ------------------------------------------------------------------ Phases 267-268
def _rows(state: Any) -> list[dict[str, Any]]:
    from strategies.b2b_lead_aggregator import POOL_NICHE
    from strategies.product_factory import facets, label

    fresh = new_employers(state.leads_for_niche(POOL_NICHE), state.clock())
    out = []
    for e in fresh.values():
        techs = sorted({t for p in e["postings"] for t in facets(p)["techs"]})
        out.append({"name": e["name"], "first": e["first"], "roles": len(e["postings"]),
                    "techs": [label(t) for t in techs][:6]})
    return sorted(out, key=lambda r: (r["first"], r["name"]), reverse=True)


def site_files(state: Any, cfg: Any, shell: Any) -> dict[str, str]:
    rows = _rows(state)
    if not rows:
        return {}
    items = "".join(f"<li><b>{html.escape(r['name'])}</b>: {r['roles']} role(s) since {r['first']:%Y-%m-%d}"
                    + (f" ({html.escape(', '.join(r['techs']))})" if r["techs"] else "") + "</li>" for r in rows[:200])
    body = (f"<h1>Companies that just started hiring</h1><p>{len(rows)} companies posted their first engineering role in the last "
            f"{NEW_DAYS} days, newest first. From public job postings.</p><ul>{items}</ul>"
            '<p><a href="../feeds/new-employers.xml">Follow by RSS</a></p>')
    page = shell("Companies that just started hiring", body, "Companies that posted their first engineering role in the last "
                                                            "30 days, from public job postings.")
    return {PAGE: page, FEED: feed(rows, cfg, state.clock())}


def feed(rows: list[dict[str, Any]], cfg: Any, now: datetime) -> str:
    base = (cfg.pages_base_url or "https://example.invalid").rstrip("/")
    rss = ET.Element("rss", version="2.0")
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = f"{cfg.site_title}: companies that just started hiring"
    ET.SubElement(ch, "link").text = f"{base}/new-employers/"
    ET.SubElement(ch, "description").text = "Companies posting their first engineering roles."
    ET.SubElement(ch, "lastBuildDate").text = format_datetime(now.astimezone(timezone.utc))
    for r in rows[:50]:
        it = ET.SubElement(ch, "item")
        ET.SubElement(it, "title").text = f"{r['name']} started hiring" + (f" ({', '.join(r['techs'][:3])})" if r["techs"] else "")
        ET.SubElement(it, "link").text = f"{base}/new-employers/"
        ET.SubElement(it, "guid", isPermaLink="false").text = f"new-employer:{r['name'].lower()}:{r['first']:%Y-%m-%d}"
        ET.SubElement(it, "pubDate").text = format_datetime(r["first"].astimezone(timezone.utc))
        ET.SubElement(it, "description").text = f"{r['roles']} engineering role(s) posted since {r['first']:%Y-%m-%d}."
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(rss, encoding="unicode") + "\n"


# ------------------------------------------------------------------ Phase 269
def weekly_line(state: Any) -> str:
    week = [r for r in _rows(state) if state.clock() - r["first"] <= timedelta(days=7)]
    if not week:
        return ""
    names = ", ".join(r["name"] for r in week[:5])
    return f"{len(week)} compan{'y' if len(week) == 1 else 'ies'} started hiring this week, e.g. {names}."


register(Kind("new_employers", "New employers", candidates, lambda c, cfg, now: build_companies(c, cfg, now),
              files=COMPANY_FILES))
