"""Organic inbound distribution: SEO landers for every dataset plus syndicated weekly radars.

Tasks:

* ``syndicate``: builds this week's "Tech Radar" article from the active niche's intel and
  publishes it (RSS always; Dev.to / Hashnode / GitHub Discussions when configured and due;
  a Substack paste-ready file). Every successful post counts as an impression.
* ``build_site``: regenerates the whole static site (one product page per packaged dataset,
  bundle and premium add-on, plus index, sitemap and RSS feed) and commits changes to GitHub Pages.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from strategies.base import Strategy, TaskContext, TaskResult
from strategies.digital_asset_packager import ASSET_KIND, niche_title
from tools.page_builder import FeedItem, ProductPage, SiteBuilder
from tools.syndicator import DevToPublisher, GitHubDiscussionsPublisher, HashnodePublisher, Syndicator, build_article


def intel_metrics(files, niche: str, high_urgency: int = 60) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    path = f"exports/intel/{niche}/tech_radar.json"
    records = files.read_json(path) if files.exists(path) else []
    signals = Counter(s for r in records for s in r.get("intent_signals", []))
    return records, {
        "companies": len(records) or None,
        "high_urgency": sum(1 for r in records if r.get("urgency_score", 0) >= high_urgency) if records else None,
        "roles": sum(r.get("openings", 0) for r in records) or None,
        "verified_urls": sum(1 for r in records if r.get("careers_url_verified")) if records else None,
        "signals": [(s.replace("_", " "), n) for s, n in signals.most_common(5)],
    }


def site_pages(tools) -> list[ProductPage]:
    """One page per (niche, kind): the newest version of every packaged dataset, bundle and add-on."""
    cfg = tools.config
    seen: set[tuple[str, str]] = set()
    pages = []
    for asset in tools.state.list_assets():  # newest first
        niche = asset.get("niche") or ""
        kind = {"lead_directory": "dataset"}.get(asset["kind"], asset["kind"])
        if not niche or (niche, kind) in seen:
            continue
        seen.add((niche, kind))
        base = f"assets/{niche}/v{asset['version']}" if kind == "dataset" else f"assets/{niche}/{kind}-v{asset['version']}"
        sample = tools.files.read_json(f"{base}/sample.json") if tools.files.exists(f"{base}/sample.json") else {}
        listing = tools.files.read_json(f"{base}/listing.json") if tools.files.exists(f"{base}/listing.json") else {}
        _, metrics = intel_metrics(tools.files, niche, cfg.high_urgency_threshold)
        pages.append(
            ProductPage(
                niche=niche, title=asset["title"], summary=listing.get("summary", asset["title"]),
                price_cents=asset["price_cents"], currency=cfg.currency, checkout_url=asset.get("checkout_url") or "",
                sample_columns=sample.get("fields", []), sample_rows=sample.get("rows", []), metrics=metrics,
                updated_at=asset["created_at"], sku=f"asset-{asset['id']}", kind=kind,
            )
        )
    return pages


class InboundSyndicator(Strategy):
    name = "inbound_syndicator"
    tasks = ("syndicate", "build_site")

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        return getattr(self, task)(ctx)

    def publishers(self, tools) -> list[Any]:
        cfg = tools.config
        return [
            DevToPublisher(tools.http, cfg.devto_api_key),
            HashnodePublisher(tools.http, cfg.hashnode_token, cfg.hashnode_publication_id),
            GitHubDiscussionsPublisher(tools.github, cfg.github_discussions_repo, cfg.github_discussions_category),
        ]

    def syndicate(self, ctx: TaskContext) -> TaskResult:
        tools = ctx.tools
        cfg = tools.config
        records, _ = intel_metrics(tools.files, ctx.niche, cfg.high_urgency_threshold)
        if len(records) < cfg.syndication_min_companies:
            return TaskResult(True, f"not enough data to syndicate ({len(records)} < {cfg.syndication_min_companies} companies)",
                              {"syndicated": False})
        asset = tools.state.latest_asset(ctx.hypothesis["id"], ASSET_KIND) or {}
        lander = f"{cfg.pages_base_url.rstrip('/')}/{ctx.niche}/" if cfg.pages_base_url else (asset.get("lander_url") or "")
        now = tools.state.clock()
        article = build_article(
            ctx.niche, niche_title(ctx.niche), records, now, lander_url=lander,
            showcase_url=asset.get("showcase_url") or "", checkout_url=asset.get("checkout_url") or "",
            price_cents=asset.get("price_cents") or 0,
        )
        results = Syndicator(cfg, tools.state, tools.files, self.publishers(tools)).syndicate(article, now)
        posted = [p for p, r in results.items() if p not in ("rss", "substack") and r.startswith("http")]
        if posted:
            tools.state.add_metric(ctx.hypothesis["id"], "impressions", "syndication", len(posted))
        return TaskResult(
            True, f"'{article.title}': " + ", ".join(f"{k}={v}" for k, v in results.items()),
            {"syndicated": True, "guid": article.guid, "results": results, "posted": len(posted)},
        )

    def build_site(self, ctx: TaskContext) -> TaskResult:
        tools = ctx.tools
        pages = site_pages(tools)
        if not pages:
            return TaskResult(True, "no datasets to publish yet", {"pages": 0})
        items = [FeedItem(**{k: i[k] for k in ("title", "link", "description", "guid", "published", "body_html")})
                 for i in tools.state.get("feed_items", []) or []]
        builder = SiteBuilder(tools.config, tools.files, tools.github)
        out = builder.build(pages, items, tools.state.clock())
        changed = builder.publish(out)
        return TaskResult(
            True, f"site: {len(pages)} product pages, {len(items)} feed items, {changed} files committed",
            {"pages": len(pages), "feed_items": len(items), "committed": changed},
        )
