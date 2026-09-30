"""Organic inbound distribution: SEO landers for every dataset plus syndicated weekly radars.

Tasks:

* ``syndicate``: builds this week's "Tech Radar" article from the active niche's intel and
  publishes it (RSS always; Dev.to / Hashnode / GitHub Discussions when configured and due;
  a Substack paste-ready file). Every successful post counts as an impression.
* ``build_site``: regenerates the whole static site (one product page per packaged dataset,
  bundle and premium add-on, the search-intent matrix pages under ``intel/``, plus index,
  sitemap and RSS feed), commits changes to GitHub Pages and submits changed URLs to IndexNow.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from strategies.base import Strategy, TaskContext, TaskResult
from strategies.digital_asset_packager import ASSET_KIND, niche_title
from tools.page_builder import (
    FeedItem, MatrixPage, ProductPage, SiteBuilder, changed_urls, compile_matrix_pages, indexnow_key, submit_indexnow,
)
from tools.syndication import DevToPublisher, GitHubDiscussionsPublisher, HashnodePublisher, Syndicator, build_article


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


def social_proof(tools, niche: str, hypothesis_id: int | None) -> dict[str, Any]:
    """Real, current numbers only: nothing here is estimated or padded."""
    from datetime import timedelta

    from strategies.tech_stack_intel import _norm_company

    now = tools.state.clock()
    week_ago = (now - timedelta(days=7)).isoformat(timespec="seconds")
    leads = tools.state.leads_for_niche(niche)
    first: dict[str, str] = {}
    for lead in leads:
        key = _norm_company(lead.get("company", ""))
        first[key] = min(first.get(key, lead["first_seen"]), lead["first_seen"])
    records, _ = intel_metrics(tools.files, niche)
    return {
        "data_updated_at": tools.state.niche_last_seen(niche) or "",
        "profiles_added_7d": sum(1 for v in first.values() if v >= week_ago),
        "verified_profiles": sum(1 for r in records if r.get("careers_url_verified")),
        "purchases_7d": tools.state.orders_since(hypothesis_id, now - timedelta(days=7)) if hypothesis_id else 0,
    }


def matrix_pages(tools, pages: list[ProductPage]) -> list[MatrixPage]:
    """Search-intent pages from every niche that has a tech radar, offering that niche's dataset."""
    cfg = tools.config
    datasets = {}
    for niche in {p.niche for p in pages}:
        path = f"exports/intel/{niche}/tech_radar.json"
        if tools.files.exists(path):
            datasets[niche] = tools.files.read_json(path)
    offers = {
        p.niche: {"niche": p.niche, "title": p.title, "price_cents": p.price_cents, "currency": p.currency,
                  "checkout_url": p.checkout_url, "subscription_url": p.subscription_url,
                  "subscription_price_cents": p.subscription_price_cents, "subscription_interval": p.subscription_interval}
        for p in pages if p.kind == "dataset"
    }
    return compile_matrix_pages(datasets, offers, tools.state.clock(), cfg.seo_min_companies, cfg.seo_min_migrations,
                                cfg.seo_max_pages)


def site_pages(tools) -> list[ProductPage]:
    """One page per (niche, kind): the newest version of every packaged dataset, bundle and add-on.
    The subscription tier isn't a page of its own: it's offered on its niche's dataset page."""
    import json as _json

    cfg = tools.config
    seen: set[tuple[str, str]] = set()
    pages = []
    subs = {}
    for a in tools.state.list_assets():
        if a["kind"] == "subscription" and a.get("checkout_url") and a.get("niche") not in subs:
            subs[a["niche"]] = a
    for asset in tools.state.list_assets():  # newest first
        niche = asset.get("niche") or ""
        kind = {"lead_directory": "dataset"}.get(asset["kind"], asset["kind"])
        if not niche or kind == "subscription" or (niche, kind) in seen:
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
                **social_proof(tools, niche, asset.get("hypothesis_id")),
                **({"subscription_url": subs[niche]["checkout_url"], "subscription_price_cents": subs[niche]["price_cents"],
                    "subscription_interval": _json.loads(subs[niche].get("kind_meta") or "{}").get("interval", "month")}
                   if kind == "dataset" and niche in subs else {}),
            )
        )
    return pages


class InboundSyndicator(Strategy):
    name = "inbound_syndicator"
    tasks = ("syndicate", "build_site", "track_hn")

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
        sample_path = f"assets/{ctx.niche}/v{asset['version']}/sample.json" if asset else ""
        sample = tools.files.read_json(sample_path) if sample_path and tools.files.exists(sample_path) else None
        article = build_article(
            ctx.niche, niche_title(ctx.niche), records, now, lander_url=lander,
            showcase_url=asset.get("showcase_url") or "", checkout_url=asset.get("checkout_url") or "",
            price_cents=asset.get("price_cents") or 0, sample=sample,
        )
        results = Syndicator(cfg, tools.state, tools.files, self.publishers(tools)).syndicate(article, now)
        posted = [p for p, r in results.items() if p not in ("rss", "substack") and r.startswith("http")]
        if posted:
            tools.state.add_metric(ctx.hypothesis["id"], "impressions", "syndication", len(posted))
        return TaskResult(
            True, f"'{article.title}': " + ", ".join(f"{k}={v}" for k, v in results.items()),
            {"syndicated": True, "guid": article.guid, "results": results, "posted": len(posted)},
        )

    def track_hn(self, ctx: TaskContext) -> TaskResult:
        if not ctx.tools.config.hn_tracker_enabled:
            return TaskResult(True, "HN tracker disabled", {})
        from agent.recovery import PlatformBackoff, is_transient
        from tools.syndication.hn_algolia_tracker import HNHiringTracker

        backoff = PlatformBackoff(ctx.tools.state)
        blocked = backoff.blocked_until("hn_algolia")
        if blocked is not None:
            return TaskResult(True, f"HN tracker backing off until {blocked.isoformat(timespec='seconds')}", {"status": "backoff"})
        try:
            res = HNHiringTracker(ctx.tools).run()
        except Exception as exc:  # noqa: BLE001
            if not is_transient(exc):
                raise
            # Algolia or GitHub is having a moment: reschedule instead of failing the task and
            # counting toward the circuit breaker.
            until = backoff.failure("hn_algolia", exc)
            ctx.tools.state.log_error("syndication:hn_algolia", f"transient failure {exc!r}; retry after {until.isoformat(timespec='seconds')}")
            return TaskResult(True, f"HN tracker deferred to {until.isoformat(timespec='seconds')}: {exc}", {"status": "backoff"})
        backoff.success("hn_algolia")
        if res.get("status") == "published":
            ctx.tools.state.add_metric(ctx.hypothesis["id"], "impressions", "hn_gist", 1)
        return TaskResult(True, "HN tracker: " + ", ".join(f"{k}={v}" for k, v in res.items()), res)

    def build_site(self, ctx: TaskContext) -> TaskResult:
        tools = ctx.tools
        cfg = tools.config
        pages = site_pages(tools)
        if not pages:
            return TaskResult(True, "no datasets to publish yet", {"pages": 0})
        items = [FeedItem(**{k: i[k] for k in ("title", "link", "description", "guid", "published", "body_html")})
                 for i in tools.state.get("feed_items", []) or []]
        matrix = matrix_pages(tools, pages) if cfg.seo_matrix_enabled else []
        live = bool(tools.github.configured() and cfg.github_pages_repo and cfg.pages_base_url)
        key = indexnow_key(cfg, tools.state) if cfg.indexnow_enabled and live else ""
        builder = SiteBuilder(cfg, tools.files, tools.github)
        out = builder.build(pages, items, tools.state.clock(), matrix=matrix, indexnow_key=key)
        changed = builder.publish(out)
        submitted = self.submit_index(tools, out, key) if key and changed else 0
        return TaskResult(
            True, f"site: {len(pages)} product pages, {len(matrix)} matrix pages, {len(items)} feed items, "
                  f"{changed} files committed, {submitted} URLs sent to IndexNow",
            {"pages": len(pages), "matrix_pages": len(matrix), "feed_items": len(items), "committed": changed,
             "indexnow": submitted},
        )

    @staticmethod
    def submit_index(tools, out: dict[str, Any], key: str) -> int:
        from agent.recovery import PlatformBackoff

        backoff = PlatformBackoff(tools.state)
        if backoff.blocked_until("indexnow"):
            return 0
        urls, hashes = changed_urls(out, tools.config.pages_base_url, tools.state.get("indexnow_hashes", {}) or {})
        if not urls:
            return 0
        try:
            status = submit_indexnow(tools.http, tools.config.pages_base_url, key, urls)
        except Exception as exc:  # noqa: BLE001 - indexing is best effort; retried after a cooldown
            backoff.failure("indexnow", exc)
            tools.state.log_error("seo:indexnow", repr(exc))
            return 0
        backoff.success("indexnow")
        tools.state.set("indexnow_hashes", hashes)
        tools.state.set("indexnow_last", {"at": tools.state.now(), "urls": len(urls), "status": status})
        return len(urls)
