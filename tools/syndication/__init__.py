"""Community syndication: value-first technical summaries, published without tripping spam filters.

Channels: Dev.to (``devto_publisher``), Hashnode (``hashnode_publisher``), GitHub Discussions
(``github_discussions``), the RSS feed, and a Substack paste-ready export. The HN "Who is hiring?"
tracker (``hn_algolia_tracker``) publishes free monthly gists.

Anti-spam guardrails:

* **Cadence**: at most one article per platform every 5 days (``MIN_INTERVAL_DAYS``), even if
  ``syndication_interval_days`` is set lower.
* **Once per article**: each (niche, ISO week) article goes to each platform once; Dev.to is also
  checked for an existing post with the same title.
* **Value first**: headlines only claim what the data shows; the body is an engineering breakdown
  (findings, stack adoption, intent signals, method) with the 5-record sanitized preview. There's
  exactly one purchase link, in the footer, next to the canonical link back to the lander.
* **Canonical URL**: set on every platform that supports it, so search credit goes to the lander
  and cross-posts aren't treated as duplicate content.
* **No personal data**: no emails, no contact details, no text copied from postings.

Links are rendered per channel: lander/showcase links get ``utm_source=<platform>`` and checkout
links get a ``client_reference_id``, so sales are attributed to the platform that drove them.
"""

from __future__ import annotations

import html
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from tools.attribution import add_utm, checkout_link

MIN_INTERVAL_DAYS = 5
MIGRATION_SIGNALS = ("migration", "legacy_refactor")
LINK_TOKENS = {"lander": "⟦lander⟧", "showcase": "⟦showcase⟧", "checkout": "⟦checkout⟧"}


@dataclass
class Article:
    guid: str
    title: str
    description: str
    markdown: str
    html: str
    tags: list[str]
    canonical_url: str
    links: dict[str, str] = field(default_factory=dict)
    channel: str = ""

    def for_channel(self, source: str, medium: str = "syndication") -> "Article":
        """Render with links tagged for ``source``. The canonical URL itself stays clean."""
        campaign = self.guid
        rendered = {
            "lander": add_utm(self.links.get("lander", ""), source, medium, campaign),
            "showcase": add_utm(self.links.get("showcase", ""), source, medium, campaign),
            "checkout": checkout_link(self.links.get("checkout", ""), source, campaign),
        }

        def fill(text: str, escape: bool) -> str:
            for key, token in LINK_TOKENS.items():
                value = rendered[key]
                text = text.replace(token, html.escape(value) if escape else value)
            return text

        return replace(self, markdown=fill(self.markdown, False), html=fill(self.html, True), channel=source)


def _label(niche_title: str) -> str:
    return niche_title.replace(" Remote", "")


def build_article(
    niche: str, niche_title: str, records: list[dict[str, Any]], now: datetime,
    lander_url: str = "", showcase_url: str = "", checkout_url: str = "", price_cents: int = 0,
    sample: dict[str, Any] | None = None, top_n: int = 10,
) -> Article:
    """A value-first quarterly-framed breakdown. Every number in the headline is computed from ``records``."""
    year, week, _ = now.isocalendar()
    quarter = (now.month - 1) // 3 + 1
    label = _label(niche_title)
    migrating = [r for r in records if any(s in r.get("intent_signals", []) for s in MIGRATION_SIGNALS)]
    focus = migrating if len(migrating) >= 3 else records
    theme = "Migrations" if focus is migrating else "Hiring"
    focus_stack = Counter(t for r in focus for t in r.get("stack", []))
    lead = [t for t, _ in focus_stack.most_common(2)]
    hiring_for = sum(1 for r in focus if set(lead) & set(r.get("stack", [])))
    if lead:
        title = f"State of {label} {theme} Q{quarter} {year}: {hiring_for} Companies Hiring for {' & '.join(lead)}"
    else:
        title = f"State of {label} {theme} Q{quarter} {year}: {len(focus)} Companies Hiring Now"

    n = len(records) or 1
    hot = sum(1 for r in records if r.get("urgency_score", 0) >= 60)
    signals = Counter(s for r in records for s in r.get("intent_signals", []))
    all_stack = Counter(t for r in records for t in r.get("stack", []))
    top = sorted(focus, key=lambda r: (-r.get("urgency_score", 0), r.get("company", "")))[:top_n]
    description = (f"{len(records)} companies hiring for {label} roles: {hot} with high hiring urgency, "
                   f"{len(migrating)} signalling a migration or legacy refactor.")

    md = [
        f"*{description}*",
        "",
        "## Key findings",
        "",
        f"- **{len(records)} companies** are actively hiring for {label} roles this week; **{hot}** show high urgency "
        "(multiple openings, fresh postings, explicit timelines).",
    ]
    if lead:
        md.append(f"- **{', '.join(lead)}** lead the stacks: {hiring_for} of the {len(focus)} "
                  f"{'companies planning migrations' if theme == 'Migrations' else 'companies analysed'} list at least one.")
    if migrating:
        md.append(f"- **{len(migrating)} companies** mention a migration or legacy refactor, which usually means "
                  "budget for tooling, contractors and data.")
    md += ["", "## Stack adoption", "", "| Technology | Companies | Share |", "|---|---|---|"]
    md += [f"| {tech} | {c} | {c / n:.0%} |" for tech, c in all_stack.most_common(10)]
    if signals:
        md += ["", "## Intent signals", "", "| Signal | Companies |", "|---|---|"]
        md += [f"| {s.replace('_', ' ')} | {c} |" for s, c in signals.most_common()]
    if sample and sample.get("rows"):
        cols = sample["fields"]
        md += ["", "## Sample: 5 companies from the dataset", "",
               "| " + " | ".join(c.replace("_", " ") for c in cols) + " |", "|" + "---|" * len(cols)]
        for row in sample["rows"][:5]:
            md.append("| " + " | ".join(
                (", ".join(map(str, v[:4])) if isinstance(v, list) else str(v if v is not None else "")).replace("|", "/")
                for v in (row.get(c) for c in cols)) + " |")
    md += ["", f"## Top {len(top)} by hiring urgency", "", "| # | Company | Urgency | Signals | Stack |", "|---|---|---|---|---|"]
    for i, r in enumerate(top, 1):
        md.append(f"| {i} | {r['company'].replace('|', '/')} | {r.get('urgency_score', 0)} | "
                  f"{', '.join(s.replace('_', ' ') for s in r.get('intent_signals', [])) or '-'} | "
                  f"{', '.join(r.get('stack', [])[:5]) or '-'} |")
    md += [
        "", "## Method", "",
        "Built from public job-board APIs (Remote OK, Arbeitnow, Hacker News \"Who is hiring\"). Postings are grouped "
        "by company, fingerprinted for stack (languages, frameworks, databases, data platform, cloud, infrastructure) "
        "and scanned for intent triggers (migrations, legacy refactors, ERP work, new teams, urgent hires). Urgency "
        "blends those triggers with posting freshness and the number of open roles.",
        "", "---",
    ]
    if showcase_url:
        md.append(f"*Free preview: [{len(records)}-company sample]({LINK_TOKENS['showcase']})*")
    if checkout_url:
        md.append(f"*Full dataset (CSV + JSON{f', ${price_cents / 100:.2f}' if price_cents else ''}): "
                  f"[get it here]({LINK_TOKENS['checkout']})*")
    if lander_url:
        md.append(f"*Originally published at [{lander_url}]({LINK_TOKENS['lander']})*")

    rows = "".join(
        f"<tr><td>{i}</td><td>{html.escape(r['company'])}</td><td>{r.get('urgency_score', 0)}</td>"
        f"<td>{html.escape(', '.join(r.get('intent_signals', [])))}</td><td>{html.escape(', '.join(r.get('stack', [])[:5]))}</td></tr>"
        for i, r in enumerate(top, 1)
    )
    link_items = "".join(
        f'<li><a href="{LINK_TOKENS[k]}">{label_}</a></li>'
        for k, label_, present in (("showcase", "Free preview", showcase_url), ("checkout", "Full dataset", checkout_url),
                                   ("lander", "Details", lander_url)) if present
    )
    body_html = (f"<p>{html.escape(description)}</p><table><tr><th>#</th><th>Company</th><th>Urgency</th><th>Signals</th>"
                 f"<th>Stack</th></tr>{rows}</table><ul>{link_items}</ul>")
    tags = [t for t in dict.fromkeys(["techradar", "hiring", *(re.sub(r"[^a-z0-9]", "", t.lower()) for t, _ in all_stack.most_common(4))]) if t][:4]
    return Article(
        guid=f"radar-{niche}-{year}-w{week:02d}", title=title, description=description,
        markdown="\n".join(md) + "\n", html=body_html, tags=tags, canonical_url=lander_url,
        links={"showcase": showcase_url, "checkout": checkout_url, "lander": lander_url},
    )


class Syndicator:
    def __init__(self, config, state, files, publishers: list[Any]):
        self.config = config
        self.state = state
        self.files = files
        self.publishers = publishers

    @property
    def interval(self) -> timedelta:
        return timedelta(days=max(MIN_INTERVAL_DAYS, self.config.syndication_interval_days))

    def due(self, platform: str, now: datetime) -> bool:
        last = self.state.get(f"syndicated:{platform}")
        return not last or now - datetime.fromisoformat(last) >= self.interval

    def add_feed_item(self, article: Article, now: datetime, guid: str | None = None) -> bool:
        items = self.state.get("feed_items", []) or []
        guid = guid or article.guid
        if any(i["guid"] == guid for i in items):
            return False
        rss = article.for_channel("rss", "feed") if not article.channel else article
        items.append({
            "guid": guid, "title": rss.title, "description": rss.description,
            "link": add_utm(article.canonical_url, "rss", "feed", guid) if article.canonical_url
            else rss.links.get("showcase", "") or article.links.get("showcase", ""),
            "published": now.isoformat(timespec="seconds"), "body_html": rss.html,
        })
        self.state.set("feed_items", items[-100:])
        return True

    def export_substack(self, article: Article, now: datetime) -> str:
        rel = f"syndication/substack/{now.date()}-{article.guid}.md"
        a = article.for_channel("substack", "newsletter")
        self.files.write_text(rel, f"# {a.title}\n\n{a.markdown}")
        return rel

    def syndicate(self, article: Article, now: datetime) -> dict[str, str]:
        """Publish ``article`` to every configured, due platform. Returns {platform: url or status}."""
        results: dict[str, str] = {}
        published_guids = set(self.state.get("syndicated_guids", []) or [])
        results["rss"] = "added" if self.add_feed_item(article, now) else "exists"
        results["substack"] = self.export_substack(article, now)
        for pub in self.publishers:
            key = f"{pub.name}:{article.guid}"
            if not pub.configured():
                continue
            if key in published_guids:
                results[pub.name] = "already published"
                continue
            if not self.due(pub.name, now):
                results[pub.name] = "not due"
                continue
            try:
                url = pub.publish(article.for_channel(getattr(pub, "channel", pub.name)), self.config.syndication_publish)
            except Exception as exc:  # noqa: BLE001 - one platform failing must not block the others
                self.state.log_error(f"syndication:{pub.name}", repr(exc))
                results[pub.name] = f"failed: {exc}"
                continue
            if not self.config.syndication_publish and not url:
                results[pub.name] = "skipped (syndication_publish = false)"
                continue
            published_guids.add(key)
            self.state.set(f"syndicated:{pub.name}", now.isoformat(timespec="seconds"))
            results[pub.name] = url or "published"
        self.state.set("syndicated_guids", sorted(published_guids)[-500:])
        return results


from tools.syndication.devto_publisher import DevToPublisher  # noqa: E402
from tools.syndication.github_discussions import GitHubDiscussionsPublisher  # noqa: E402
from tools.syndication.hashnode_publisher import HashnodePublisher  # noqa: E402

__all__ = [
    "Article", "DevToPublisher", "GitHubDiscussionsPublisher", "HashnodePublisher", "MIN_INTERVAL_DAYS",
    "Syndicator", "build_article",
]
