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
    FeedItem, MatrixPage, ProductPage, SiteBuilder, changed_urls, compile_matrix_pages, indexnow_key, link_related,
    submit_indexnow,
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
        "high_intent": sum(1 for r in records if r.get("intent_level") == "High") if records else None,
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
    everything = compile_matrix_pages(datasets, offers, tools.state.clock(), cfg.seo_min_companies, cfg.seo_min_migrations,
                                      max_pages=10**6)
    shares = {n: s for n, s in ((tools.state.get("niche_allocation") or {}).get("shares") or {}).items() if n in offers}
    if len(shares) < 2:
        return link_related(everything[:cfg.seo_max_pages])
    # Satellites: split the page budget by revenue share (largest remainder), strongest pages
    # first within each niche; leftover budget goes to the strongest remaining pages.
    from strategies.satellite_orchestrator import split_quota

    quota = split_quota(cfg.seo_max_pages, {n: s / sum(shares.values()) for n, s in shares.items()})
    chosen, rest = [], []
    for page in everything:
        niche = page.offer.get("niche")
        if quota.get(niche, 0) > 0:
            quota[niche] -= 1
            chosen.append(page)
        else:
            rest.append(page)
    chosen += rest[: max(0, cfg.seo_max_pages - len(chosen))]
    order = {id(p): i for i, p in enumerate(everything)}
    return link_related(sorted(chosen, key=lambda p: order[id(p)]))


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
    from api.auth import api_asset
    from tools.copy_bandit import page_arms

    api = api_asset(tools.state)
    policy = tools.state.get("copy_policy") or {}
    bandit = cfg.copy_bandit_enabled and bool(cfg.lead_capture_base)
    from tools.site_extras import popular_niche

    popular = popular_niche(tools.state)
    for asset in tools.state.list_assets():  # newest first
        niche = asset.get("niche") or ""
        kind = {"lead_directory": "dataset"}.get(asset["kind"], asset["kind"])
        if not niche or kind in ("subscription", "team_license", "subscription_annual") or (niche, kind) in seen:
            continue
        if kind in ("custom_request", "pay_what_you_want", "sponsorship", "lifetime", "gift"):
            continue  # offers have their own page (more/), not a product page
        if kind == "micro" and (asset.get("status") != "published" or not asset.get("checkout_url")):
            continue  # factory products appear once they can be bought; retired ones disappear
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
        page = pages[-1]
        from strategies.testimonials import approved_for

        page.testimonials = approved_for(tools.state, niche) if kind == "dataset" else []
        page.refund_days = int(cfg.refund_policy_days or 0)
        page.popular = kind == "dataset" and niche == popular
        if kind == "dataset":
            from strategies.plans import ANNUAL_KIND, TEAM_KIND, plan_for

            page.versions = [{"version": a["version"], "date": str(a["created_at"])[:10], "rows": int(a["lead_count"] or 0)}
                             for a in tools.state.list_assets()
                             if a["kind"] == "lead_directory" and (a.get("niche") or "") == niche
                             and a.get("status") in ("published", "staged")][:30]
            team, annual = plan_for(tools.state, TEAM_KIND, niche), plan_for(tools.state, ANNUAL_KIND, niche)
            if team:
                page.team_url, page.team_price_cents, page.team_seats = team["checkout_url"], int(team["price_cents"]), \
                    int(cfg.team_license_seats)
            if annual:
                page.annual_url, page.annual_price_cents = annual["checkout_url"], int(annual["price_cents"])
            from datetime import datetime as _dt
            from datetime import timezone as _tz

            from strategies.launch_promos import active_code
            from strategies.seasonal_sale import active_sale
            from tools.site_extras import faq

            promo = active_code(tools.state, niche) or active_sale(tools.state)
            if promo:
                until = _dt.fromtimestamp(int(promo["expires_at"]), tz=_tz.utc).strftime("%b %d")
                page.banner = f"{promo['percent_off']}% off with code {promo['code']} until {until} (enter it at checkout)."
            page.faq = faq(cfg, page)
        if bandit and kind == "dataset":
            facts = {"label": niche_title(niche).replace(" Remote", ""), "companies": metrics.get("companies") or 0,
                     "hot": metrics.get("high_intent") or metrics.get("high_urgency") or 0,
                     "verified": metrics.get("verified_urls") or 0, "price": f"${asset['price_cents'] / 100:.2f}",
                     "api_price": f"${cfg.api_price_cents / 100:.2f}", "checkout_url": asset.get("checkout_url") or "",
                     "lead_form": cfg.lead_magnet_enabled, "api_url": (api or {}).get("checkout_url") or ""}
            page.copy_arms = page_arms(facts, policy)
            page.copy_version = int(policy.get("version", 0))
            page.copy_targets = {"free_sample": "#lead", "instant_feed": facts["checkout_url"], "developer_api": facts["api_url"]}
            page.telemetry_url = f"{cfg.lead_capture_base}/t/e"
    return pages


class InboundSyndicator(Strategy):
    name = "inbound_syndicator"
    tasks = ("syndicate", "build_site", "track_hn", "tune_copy")

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
        """Write about the niche furthest below its revenue share of recent articles (primary and
        satellites); with a single niche this is simply the active one."""
        tools, cfg = ctx.tools, ctx.tools.config
        shares = (tools.state.get("niche_allocation") or {}).get("shares") or {}
        if len(shares) > 1:
            from strategies.satellite_orchestrator import choose_by_deficit, niche_of, working_set

            hyps = {niche_of(h): h for h in working_set(tools.state)}
            counts = tools.state.get("syndication_niche_counts") or {}
            for niche in choose_by_deficit(shares, counts, eligible=[n for n in shares if n in hyps]):
                records, _ = intel_metrics(tools.files, niche, cfg.high_urgency_threshold)
                if len(records) >= cfg.syndication_min_companies:
                    counts[niche] = counts.get(niche, 0) + 1
                    tools.state.set("syndication_niche_counts", counts)
                    ctx = TaskContext(tools, hyps[niche], {})
                    break
        return self.syndicate_niche(ctx)

    def syndicate_niche(self, ctx: TaskContext) -> TaskResult:
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

    def tune_copy(self, ctx: TaskContext) -> TaskResult:
        """Recompute the copy bandit's allocation (and retire losing variants) before the site build."""
        from tools.copy_bandit import purge_seen, tune

        tools = ctx.tools
        if not tools.config.copy_bandit_enabled:
            return TaskResult(True, "copy bandit disabled", {})
        now = tools.state.clock()
        purge_seen(tools.state, now)
        policy = tune(tools.state, tools.config, now)
        alloc = "; ".join(f"{slot}: " + ", ".join(f"{v} {p:.0%}" for v, p in arms.items()) for slot, arms in policy["allocation"].items())
        return TaskResult(True, f"copy policy v{policy['version']}: {alloc}" + (f" ({'; '.join(policy['changes'])})" if policy["changes"] else ""),
                          {"version": policy["version"], "winners": policy["winners"], "changes": policy["changes"]})

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
        from strategies.revenue_models import more_page, sponsor_html
        from tools.offer_pages import _shell

        extra = {"more/index.html": more_page(tools.state, lambda title, body, desc: _shell(
            title, body, tools.config.site_title, description=desc))}
        out = builder.build(pages, items, tools.state.clock(), matrix=matrix, indexnow_key=key, extra=extra,
                            sponsor=sponsor_html(tools.state))
        from tools.site_audit import audit_site, record

        audit = audit_site(out, cfg.pages_base_url)
        record(tools.state, audit)
        changed = builder.publish(out, tools.state)
        # Tell search engines only once everything is live, so they never fetch a half-published site.
        submitted = self.submit_index(tools, out, key) if key and not builder.pending else 0
        return TaskResult(
            True, f"site: {len(pages)} product pages, {len(matrix)} matrix pages, {len(items)} feed items, "
                  f"{changed} files committed" + (f" ({builder.pending} left for next cycle)" if builder.pending else "")
                  + f", {submitted} URLs sent to IndexNow",
            {"pages": len(pages), "matrix_pages": len(matrix), "feed_items": len(items), "committed": changed,
             "pending": builder.pending, "indexnow": submitted},
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
