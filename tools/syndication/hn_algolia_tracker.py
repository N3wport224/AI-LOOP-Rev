"""Hacker News "Who is hiring?" tracker (Algolia HN API) → free monthly stack summary as a public Gist.

* Finds the newest monthly thread posted by ``whoishiring``
  (``/api/v1/search_by_date?tags=story,author_whoishiring``).
* Parses each top-level comment's header (``Company | Role | Location | …``) and fingerprints the
  whole comment for technologies and intent signals (``strategies.tech_stack_intel``).
* Writes an aggregate Markdown summary: stack share, remote share, intent signals, and a
  company → stack table. It holds **no contact details and no text copied from comments**, only
  derived facts. Dataset references at the bottom carry ``utm_source=github&utm_medium=gist``.
* Publishes/updates one public Gist per thread (at most every ``hn_gist_refresh_hours``, since the
  thread keeps growing for days), keeps a copy in ``data/syndication/hn/``, and adds one RSS item
  per thread.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from strategies.b2b_lead_aggregator import enrich_lead, parse_hn_comment
from strategies.tech_stack_intel import fingerprint, intent_signals
from tools.attribution import add_utm

ALGOLIA = "https://hn.algolia.com/api/v1"


class HNHiringTracker:
    def __init__(self, tools):
        self.tools = tools
        self.state = tools.state
        self.cfg = tools.config

    # -- data ------------------------------------------------------------------------
    def latest_thread(self) -> dict[str, Any] | None:
        search = self.tools.http.get_json(
            f"{ALGOLIA}/search_by_date",
            params={"tags": "story,author_whoishiring", "query": "who is hiring", "hitsPerPage": 5},
            check_robots=False,
        )
        hits = [h for h in (search or {}).get("hits", []) if "who is hiring" in (h.get("title") or "").lower()]
        if not hits:
            return None
        h = hits[0]
        return {"id": str(h["objectID"]), "title": h.get("title", ""), "created_at": h.get("created_at", "")}

    def companies(self, thread_id: str, max_comments: int = 1000) -> list[dict[str, Any]]:
        item = self.tools.http.get_json(f"{ALGOLIA}/items/{thread_id}", check_robots=False) or {}
        rows: dict[str, dict[str, Any]] = {}
        for child in (item.get("children") or [])[:max_comments]:
            lead = parse_hn_comment(child, thread_id)
            if lead is None:
                continue
            lead = enrich_lead(lead)
            text = " ".join([lead.title, lead.description])
            stack = sorted({t for names in fingerprint(text).values() for t in names})
            key = lead.company.lower()
            row = rows.setdefault(key, {"company": lead.company, "roles": [], "stack": set(), "remote": False, "signals": set()})
            row["roles"].append(lead.title[:80])
            row["stack"].update(stack)
            row["remote"] = row["remote"] or lead.remote
            row["signals"].update(intent_signals(text))
        out = []
        for r in rows.values():
            out.append({**r, "stack": sorted(r["stack"]), "signals": sorted(r["signals"])})
        return sorted(out, key=lambda r: r["company"].lower())

    # -- output ------------------------------------------------------------------------
    def dataset_links(self, campaign: str) -> list[tuple[str, str]]:
        base = self.cfg.pages_base_url.rstrip("/")
        links = []
        seen = set()
        for a in self.state.list_assets():
            niche = a.get("niche")
            if a["kind"] != "lead_directory" or not niche or niche in seen:
                continue
            seen.add(niche)
            url = f"{base}/{niche}/" if base else (a.get("lander_url") or a.get("showcase_url") or "")
            if url:
                links.append((a["title"], add_utm(url, "github", "gist", campaign)))
        return links[:6]

    def render(self, thread: dict[str, Any], companies: list[dict[str, Any]], now: datetime) -> str:
        n = len(companies) or 1
        stack = Counter(t for c in companies for t in c["stack"])
        signals = Counter(s for c in companies for s in c["signals"])
        remote = sum(1 for c in companies if c["remote"])
        campaign = f"hn_{thread['id']}"
        md = [
            f"# {thread['title']}: stack breakdown of {len(companies)} companies",
            "",
            f"_Aggregated from the public HN thread (https://news.ycombinator.com/item?id={thread['id']}), "
            f"updated {now.strftime('%Y-%m-%d %H:%M')} UTC. Derived facts only: no contact details, no copied text._",
            "",
            "## Headlines",
            "",
            f"- **{len(companies)} companies** posted; **{remote} ({remote / n:.0%})** mention remote work.",
        ]
        if stack:
            top3 = ", ".join(f"{t} ({c})" for t, c in stack.most_common(3))
            md.append(f"- Most requested: {top3}.")
        if signals:
            md.append(f"- {signals.get('migration', 0)} mention migrations, {signals.get('new_team', 0)} are building new teams, "
                      f"{signals.get('urgent_hire', 0)} say they're hiring urgently.")
        md += ["", "## Technology demand", "", "| Technology | Companies | Share |", "|---|---|---|"]
        md += [f"| {t} | {c} | {c / n:.0%} |" for t, c in stack.most_common(20)]
        if signals:
            md += ["", "## Intent signals", "", "| Signal | Companies |", "|---|---|"]
            md += [f"| {s.replace('_', ' ')} | {c} |" for s, c in signals.most_common()]
        md += ["", "## Companies and their stacks", "", "| Company | Roles | Remote | Stack |", "|---|---|---|---|"]
        for c in companies[:200]:
            md.append(f"| {c['company'].replace('|', '/')} | {len(c['roles'])} | {'yes' if c['remote'] else '-'} | "
                      f"{', '.join(c['stack'][:8]) or '-'} |")
        links = self.dataset_links(campaign)
        if links:
            md += ["", "---", "", "Company-level datasets with urgency scores and verified careers pages:", ""]
            md += [f"- [{title}]({url})" for title, url in links]
        return "\n".join(md) + "\n"

    # -- one pass --------------------------------------------------------------------------
    def run(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or self.state.clock()
        thread = self.latest_thread()
        if thread is None:
            return {"status": "no thread found"}
        key = f"hn_tracker:{thread['id']}"
        record = self.state.get(key) or {}
        last = record.get("updated_at")
        if last and now - datetime.fromisoformat(last) < timedelta(hours=self.cfg.hn_gist_refresh_hours):
            return {"status": "fresh", "thread": thread["id"], "gist": record.get("gist_url", "")}
        companies = self.companies(thread["id"])
        if not companies:
            return {"status": "no parsable posts yet", "thread": thread["id"]}
        md = self.render(thread, companies, now.astimezone(timezone.utc))
        self.tools.files.write_text(f"syndication/hn/{thread['id']}.md", md)
        gist_url = record.get("gist_url", "")
        if self.tools.github.configured():
            gist = self.tools.github.upsert_gist(
                record.get("gist_id"), f"{thread['title']}: stack breakdown ({len(companies)} companies)",
                {f"hn-who-is-hiring-{thread['id']}.md": md}, public=True,
            )
            record.update(gist_id=gist["id"], gist_url=gist["html_url"])
            gist_url = gist["html_url"]
        record.update(updated_at=now.isoformat(timespec="seconds"), companies=len(companies))
        self.state.set(key, record)
        items = self.state.get("feed_items", []) or []
        guid = f"hn-{thread['id']}"
        if gist_url and not any(i["guid"] == guid for i in items):
            items.append({"guid": guid, "title": f"{thread['title']}: stack breakdown of {len(companies)} companies",
                          "description": f"Technology demand across {len(companies)} companies hiring on Hacker News.",
                          "link": gist_url, "published": now.isoformat(timespec="seconds"), "body_html": ""})
            self.state.set("feed_items", items[-100:])
        return {"status": "published" if gist_url else "written locally", "thread": thread["id"],
                "companies": len(companies), "gist": gist_url}
