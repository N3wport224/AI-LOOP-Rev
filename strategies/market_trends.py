"""Market trends (Phases 220-224): which technologies are hiring more, and products that follow.

* **Phase 220, weekly counts:** new postings per technology per week for the last ``WEEKS`` weeks,
  by posting date (or the day the agent first saw the posting when a board gives no date). Worked
  out at most once every ``CACHE_HOURS`` and kept in kv ``tech_trends``.
* **Phase 221, rising and falling:** the last two weeks against the two before. A technology with
  at least ``MIN_RECENT`` recent postings that grew by ``RISE`` (25%) or more is rising; one that
  shrank by 25% or more is falling.
* **Phase 222, trends page:** ``trends/`` on the site: rising and falling technologies and an
  8-week table for the busiest ones, from the same public postings as the datasets.
* **Phase 223, the factory follows demand:** among products of the same type, ones about a rising
  technology are made first.
* **Phase 224, fastest-hiring companies** (``fast-hiring-<tech>``, $9): a new product type. Companies
  that posted ``FAST_MIN_ROLES`` (3) or more new roles for a technology in the last 14 days, ranked
  by new roles. Needs ``FAST_MIN_COMPANIES`` (8) such companies.
"""

from __future__ import annotations

import html
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

KEY = "tech_trends"
WEEKS = 8
CACHE_HOURS = 6
MIN_RECENT = 10
RISE = 0.25
FAST_DAYS = 14
FAST_MIN_ROLES = 3
FAST_MIN_COMPANIES = 8
FAST_PRICE = 900


def _when(lead: dict[str, Any]) -> datetime | None:
    for field in ("posted_at", "first_seen"):
        raw = str(lead.get(field) or "")
        if not raw:
            continue
        try:
            t = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    return None


# ------------------------------------------------------------------ Phase 220
def weekly_counts(leads: list[dict[str, Any]], now: datetime, weeks: int = WEEKS) -> dict[str, list[int]]:
    """technology → new postings per week, oldest first (the last entry is the 7 days up to now)."""
    from strategies.product_factory import facets

    counts: dict[str, list[int]] = defaultdict(lambda: [0] * weeks)
    for lead in leads:
        when = _when(lead)
        if when is None or when > now:
            continue
        age = int((now - when).total_seconds() // (7 * 86400))
        if age >= weeks:
            continue
        for tech in facets(lead)["techs"]:
            counts[tech][weeks - 1 - age] += 1
    return dict(counts)


# ------------------------------------------------------------------ Phase 221
def classify(counts: dict[str, list[int]]) -> dict[str, Any]:
    rising, falling = [], []
    for tech, weeks in counts.items():
        recent, before = sum(weeks[-2:]), sum(weeks[-4:-2])
        if recent < MIN_RECENT and before < MIN_RECENT:
            continue
        growth = (recent - before) / max(before, 1)
        row = {"tech": tech, "recent": recent, "before": before, "growth": round(growth, 2)}
        if recent >= MIN_RECENT and growth >= RISE and before > 0:
            rising.append(row)
        elif growth <= -RISE:
            falling.append(row)
    rising.sort(key=lambda r: (-r["growth"], r["tech"]))
    falling.sort(key=lambda r: (r["growth"], r["tech"]))
    return {"rising": rising, "falling": falling}


def trends(state: Any, force: bool = False) -> dict[str, Any]:
    """The cached weekly counts and their classification (worked out again every CACHE_HOURS)."""
    cached = state.get(KEY) or {}
    now = state.clock()
    if not force and cached.get("counts") and now - datetime.fromisoformat(cached["at"]) < timedelta(hours=CACHE_HOURS):
        return cached
    from strategies.b2b_lead_aggregator import POOL_NICHE

    counts = weekly_counts(state.leads_for_niche(POOL_NICHE), now)
    out = {"at": state.now(), "counts": counts, **classify(counts)}
    state.set(KEY, out)
    return out


def rising_techs(state: Any) -> set[str]:
    return {r["tech"] for r in trends(state).get("rising") or []}


# ------------------------------------------------------------------ Phase 222
def trends_page(state: Any, shell: Any) -> str:
    from strategies.product_factory import label

    t = trends(state)
    counts = t.get("counts") or {}
    if not counts:
        return ""

    def items(rows: list[dict[str, Any]], sign: str) -> str:
        if not rows:
            return "<p class=\"muted\">None this fortnight.</p>"
        return "<ul>" + "".join(f"<li><b>{html.escape(label(r['tech']))}</b>: {sign}{abs(round(r['growth'] * 100))}% "
                                f"({r['before']} → {r['recent']} new postings)</li>" for r in rows[:10]) + "</ul>"

    busiest = sorted(counts.items(), key=lambda kv: (-sum(kv[1]), kv[0]))[:20]
    head = "".join(f"<th>{'This week' if i == WEEKS - 1 else f'{WEEKS - 1 - i}w ago'}</th>" for i in range(WEEKS))
    rows = "".join(f"<tr><td>{html.escape(label(tech))}</td>" + "".join(f"<td>{n}</td>" for n in weeks) + "</tr>"
                   for tech, weeks in busiest)
    body = ("<h1>Hiring trends by technology</h1><p>New public job postings per technology, week by week. Rising: at least "
            f"{MIN_RECENT} postings in the last two weeks and up {round(RISE * 100)}% or more on the two weeks before.</p>"
            "<h2>Rising</h2>" + items(t.get("rising") or [], "+") + "<h2>Cooling</h2>" + items(t.get("falling") or [], "−")
            + "<h2>The busiest technologies</h2><div style=\"overflow-x:auto\"><table><thead><tr><th>Technology</th>" + head
            + "</tr></thead><tbody>" + rows + f"</tbody></table></div><p class=\"muted\">Updated {html.escape(t['at'][:10])}.</p>")
    return shell("Hiring trends by technology", body, "Which technologies companies are hiring for more, and less, week by week, "
                                                      "from public job postings.")


# ------------------------------------------------------------------ Phase 223
def favour_rising(cands: list[dict[str, Any]], rising: set[str]) -> list[dict[str, Any]]:
    """Stable: within a type, products about a rising technology move to the front."""
    if not rising:
        return cands
    return sorted(cands, key=lambda c: str((c.get("filters") or {}).get("tech") or "") not in rising)


# ------------------------------------------------------------------ Phase 224
def fast_hiring_candidates(tagged: list[tuple[dict[str, Any], dict[str, Any]]], now: datetime) -> list[dict[str, Any]]:
    from strategies.product_factory import label
    from strategies.product_types import _slug, companies

    since = now - timedelta(days=FAST_DAYS)
    recent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for lead, f in tagged:
        when = _when(lead)
        if when is None or when < since:
            continue
        for tech in f["techs"]:
            recent[tech].append(lead)
    out = []
    for tech, rows in recent.items():
        cos = [c for c in companies(rows) if c["open_roles"] >= FAST_MIN_ROLES]
        if len(cos) < FAST_MIN_COMPANIES:
            continue
        cos = cos[:50]
        out.append({"type": "fast_hiring", "slug": f"fast-hiring-{_slug(tech)}",
                    "title": f"Fastest-Hiring {label(tech)} Companies (Last {FAST_DAYS} Days)", "filters": {"tech": tech},
                    "rows": cos, "companies": len(cos), "keys": {f"fast:{tech}:{c['company'].lower()}" for c in cos},
                    "score": sum(c["open_roles"] for c in cos)})
    return sorted(out, key=lambda c: (-c["score"], c["slug"]))


def build_fast_hiring(cand: dict[str, Any], cfg: Any, now: datetime) -> dict[str, Any]:
    from strategies.product_types import build_company_list

    made = build_company_list({**cand, "type": "top"}, cfg, now)
    rows = cand["rows"]
    kind = f"companies with {FAST_MIN_ROLES}+ new roles in the last {FAST_DAYS} days"
    readme = (f"# {cand['title']}\n\n{len(rows)} {kind}, ranked by new roles, from current public job postings. Built "
              f"{now:%Y-%m-%d}. One row per company: new roles, example titles and locations, remote share, stack, latest "
              "posting and a link. Fast hiring usually means budget, urgency and a team that's growing.\n")
    made["files"]["README.md"] = readme.encode()
    made.update(readme=readme, price_cents=FAST_PRICE,
                summary=f"{len(rows)} {kind}: who is growing fastest right now. CSV, JSON and SQL.")
    return made
