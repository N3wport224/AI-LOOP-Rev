"""Turn tech-radar data into syndicated articles and publish them.

Channels:

* **Dev.to**: ``POST https://dev.to/api/articles`` (``api-key`` header). Sets ``canonical_url``
  so search credit goes to your lander.
* **Hashnode**: GraphQL ``publishPost`` at ``https://gql.hashnode.com`` (``Authorization`` token),
  with ``originalArticleURL`` as the canonical.
* **GitHub Discussions**: GraphQL ``createDiscussion`` in ``github_discussions_repo``.
* **RSS**: every article becomes an item in ``feeds/radar.xml`` (built by ``page_builder``).
  Substack, Medium and newsletter tools can import or auto-post from it.
* **Substack**: no public posting API exists, so a paste-ready Markdown file is written to
  ``data/syndication/substack/``.

Guardrails: at most one post per platform every ``syndication_interval_days``; each
(niche, ISO week) article is published once; a minimum number of companies must back it;
no contact details are ever included.
"""

from __future__ import annotations

import html
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from tools.http_client import HttpClient
    from tools.storefront.github import GitHubClient

MIGRATION_SIGNALS = ("migration", "legacy_refactor")


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


def _label(niche_title: str) -> str:
    return niche_title.replace(" Remote", "")


def build_article(
    niche: str, niche_title: str, records: list[dict[str, Any]], now: datetime,
    lander_url: str = "", showcase_url: str = "", checkout_url: str = "", price_cents: int = 0, top_n: int = 10,
) -> Article:
    """A data-driven weekly summary. Headlines only claim what the data shows."""
    year, week, _ = now.isocalendar()
    migrating = [r for r in records if any(s in r.get("intent_signals", []) for s in MIGRATION_SIGNALS)]
    focus = migrating if len(migrating) >= 3 else records
    stack_counts = Counter(t for r in focus for t in r.get("stack", []))
    lead_tech = stack_counts.most_common(1)[0][0] if stack_counts else ""
    label = _label(niche_title)
    top = sorted(focus, key=lambda r: (-r.get("urgency_score", 0), r.get("company", "")))[:top_n]
    if focus is migrating:
        title = f"Weekly Tech Radar: Top {len(top)} {label} Companies Planning Migrations" + (
            f" ({lead_tech} Leads Their Stacks)" if lead_tech else ""
        )
    else:
        title = f"Weekly Tech Radar: {len(top)} {label} Companies Hiring Now, and What They Run"
    signals = Counter(s for r in records for s in r.get("intent_signals", []))
    all_stack = Counter(t for r in records for t in r.get("stack", []))
    n = len(records) or 1
    hot = sum(1 for r in records if r.get("urgency_score", 0) >= 60)
    description = (
        f"{len(records)} companies hiring for {label} roles this week: {hot} show high hiring urgency, "
        f"{len(migrating)} signal a migration or legacy refactor."
    )

    md = [
        f"*Week {week}, {year}.* {description}",
        "",
        f"## Top {len(top)} by hiring urgency",
        "",
        "| # | Company | Urgency | Signals | Stack |",
        "|---|---|---|---|---|",
    ]
    for i, r in enumerate(top, 1):
        md.append(
            f"| {i} | {r['company'].replace('|', '/')} | {r.get('urgency_score', 0)} | "
            f"{', '.join(s.replace('_', ' ') for s in r.get('intent_signals', [])) or '-'} | "
            f"{', '.join(r.get('stack', [])[:5]) or '-'} |"
        )
    md += ["", "## What the market is running", ""]
    md += [f"- **{tech}**: {c} companies ({c / n:.0%})" for tech, c in all_stack.most_common(8)]
    if signals:
        md += ["", "## Buying signals this week", ""]
        md += [f"- {s.replace('_', ' ')}: {c}" for s, c in signals.most_common()]
    md += [
        "",
        "## Method",
        "",
        "Built from public job-board APIs (Remote OK, Arbeitnow, Hacker News \"Who is hiring\"). Postings are "
        "grouped by company, fingerprinted for stack (languages, frameworks, databases, data platform, cloud, "
        "infrastructure) and scanned for intent triggers (migrations, legacy refactors, ERP work, new teams, "
        "urgent hires). Urgency blends those triggers with posting freshness and the number of open roles.",
        "",
        "---",
    ]
    footer = []
    if showcase_url:
        footer.append(f"Free 5-company preview: {showcase_url}")
    if checkout_url:
        footer.append(f"Full dataset ({len(records)} companies, CSV + JSON{f', ${price_cents / 100:.2f}' if price_cents else ''}): {checkout_url}")
    if lander_url:
        footer.append(f"Originally published at {lander_url}")
    md += [f"*{line}*" for line in footer]
    markdown = "\n".join(md) + "\n"

    rows = "".join(
        f"<tr><td>{i}</td><td>{html.escape(r['company'])}</td><td>{r.get('urgency_score', 0)}</td>"
        f"<td>{html.escape(', '.join(r.get('intent_signals', [])))}</td><td>{html.escape(', '.join(r.get('stack', [])[:5]))}</td></tr>"
        for i, r in enumerate(top, 1)
    )
    links = "".join(
        f'<li><a href="{html.escape(u)}">{html.escape(k)}</a></li>'
        for k, u in (("Free preview", showcase_url), ("Full dataset", checkout_url), ("Details", lander_url)) if u
    )
    body_html = (
        f"<p>{html.escape(description)}</p><table><tr><th>#</th><th>Company</th><th>Urgency</th><th>Signals</th>"
        f"<th>Stack</th></tr>{rows}</table><ul>{links}</ul>"
    )
    tags = [t for t in dict.fromkeys(["techradar", "hiring", *(re.sub(r"[^a-z0-9]", "", t.lower()) for t, _ in all_stack.most_common(4))]) if t][:4]
    return Article(
        guid=f"radar-{niche}-{year}-w{week:02d}",
        title=title,
        description=description,
        markdown=markdown,
        html=body_html,
        tags=tags,
        canonical_url=lander_url,
        links={"showcase": showcase_url, "checkout": checkout_url, "lander": lander_url},
    )


# ----------------------------------------------------------------------------- publishers
class DevToPublisher:
    name = "devto"
    URL = "https://dev.to/api/articles"

    def __init__(self, http: "HttpClient", api_key: str):
        self.http, self.api_key = http, api_key

    def configured(self) -> bool:
        return bool(self.api_key)

    @staticmethod
    def payload(article: Article, published: bool) -> dict[str, Any]:
        body: dict[str, Any] = {
            "title": article.title[:128],
            "body_markdown": article.markdown,
            "published": published,
            "tags": article.tags[:4],
            "description": article.description[:150],
        }
        if article.canonical_url.startswith("http"):
            body["canonical_url"] = article.canonical_url
        return {"article": body}

    def publish(self, article: Article, published: bool) -> str:
        resp = self.http.post(
            self.URL, json_body=self.payload(article, published),
            headers={"api-key": self.api_key, "Accept": "application/vnd.forem.api-v1+json"}, check_robots=False,
        )
        data = resp.json() or {}
        return str(data.get("url") or data.get("id") or "")


class HashnodePublisher:
    name = "hashnode"
    URL = "https://gql.hashnode.com"
    MUTATION = (
        "mutation PublishPost($input: PublishPostInput!) { publishPost(input: $input) { post { id url } } }"
    )

    def __init__(self, http: "HttpClient", token: str, publication_id: str):
        self.http, self.token, self.publication_id = http, token, publication_id

    def configured(self) -> bool:
        return bool(self.token and self.publication_id)

    def payload(self, article: Article) -> dict[str, Any]:
        inp: dict[str, Any] = {
            "title": article.title,
            "publicationId": self.publication_id,
            "contentMarkdown": article.markdown,
            "tags": [{"slug": t, "name": t} for t in article.tags],
        }
        if article.canonical_url.startswith("http"):
            inp["originalArticleURL"] = article.canonical_url
        return {"query": self.MUTATION, "variables": {"input": inp}}

    def publish(self, article: Article, published: bool) -> str:
        if not published:
            return ""  # Hashnode drafts are not exposed the same way; publish-only
        resp = self.http.post(self.URL, json_body=self.payload(article), headers={"Authorization": self.token}, check_robots=False)
        data = resp.json() or {}
        if data.get("errors"):
            raise RuntimeError(f"hashnode: {data['errors'][0].get('message', data['errors'])}")
        return str(((data.get("data") or {}).get("publishPost") or {}).get("post", {}).get("url", ""))


class GitHubDiscussionsPublisher:
    name = "github_discussions"
    IDS = (
        "query($owner: String!, $name: String!) { repository(owner: $owner, name: $name) { id "
        "discussionCategories(first: 25) { nodes { id name } } } }"
    )
    CREATE = (
        "mutation($input: CreateDiscussionInput!) { createDiscussion(input: $input) { discussion { url } } }"
    )

    def __init__(self, github: "GitHubClient", repo: str, category: str):
        self.github, self.repo, self.category = github, repo, category

    def configured(self) -> bool:
        return bool(self.github.configured() and self.repo)

    def publish(self, article: Article, published: bool) -> str:
        if not published:
            return ""
        owner, name = self.repo.split("/", 1)
        repo = self.github.graphql(self.IDS, {"owner": owner, "name": name})["repository"]
        cats = {c["name"].lower(): c["id"] for c in repo["discussionCategories"]["nodes"]}
        cat_id = cats.get(self.category.lower())
        if not cat_id:
            raise LookupError(f"discussion category {self.category!r} not found in {self.repo}")
        result = self.github.graphql(
            self.CREATE, {"input": {"repositoryId": repo["id"], "categoryId": cat_id, "title": article.title, "body": article.markdown}}
        )
        return result["createDiscussion"]["discussion"]["url"]


class Syndicator:
    def __init__(self, config, state, files, publishers: list[Any]):
        self.config = config
        self.state = state
        self.files = files
        self.publishers = publishers

    def due(self, platform: str, now: datetime) -> bool:
        last = self.state.get(f"syndicated:{platform}")
        return not last or now - datetime.fromisoformat(last) >= timedelta(days=self.config.syndication_interval_days)

    def add_feed_item(self, article: Article, now: datetime) -> bool:
        items = self.state.get("feed_items", []) or []
        if any(i["guid"] == article.guid for i in items):
            return False
        items.append({
            "guid": article.guid, "title": article.title, "description": article.description,
            "link": article.canonical_url or article.links.get("showcase", ""), "published": now.isoformat(timespec="seconds"),
            "body_html": article.html,
        })
        self.state.set("feed_items", items[-100:])
        return True

    def export_substack(self, article: Article, now: datetime) -> str:
        rel = f"syndication/substack/{now.date()}-{article.guid}.md"
        self.files.write_text(rel, f"# {article.title}\n\n{article.markdown}")
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
                url = pub.publish(article, self.config.syndication_publish)
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
