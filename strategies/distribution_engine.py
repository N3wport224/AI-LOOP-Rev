"""Distribution: publish checkouts and landers, showcase samples, send approved outreach, deliver orders,
and measure the funnel (impressions → views → purchases) that drives hypothesis scoring.

Tasks:

* ``publish_listing``: create or reuse a live checkout on the active storefront and deploy the lander.
  It refuses to open a checkout the agent could not fulfil (Stripe needs working email delivery,
  unless ``allow_manual_fulfillment``).
* ``publish_showcase``: a sanitized 5-record preview with the checkout link, written to
  ``data/showcase/<niche>/`` and to a GitHub repo ``showcase/`` directory or a public Gist.
* ``dispatch_outreach``: honour unsubscribe replies (IMAP), then send approved drafts under the
  dispatcher's dry-run, warm-up and CAN-SPAM rules.
* ``deliver_orders``: email each paid order its dataset.
* ``collect_metrics``: snapshot views (GitHub traffic), impressions and purchases per hypothesis.
"""

from __future__ import annotations

from typing import Any

from strategies.base import Strategy, TaskContext, TaskResult
from strategies.digital_asset_packager import ASSET_KIND
from tools.attribution import add_utm, checkout_link
from tools.storefront import Listing
from tools.storefront.stripe_pages_publisher import PagesDeployer, _cell


def _load_listing(ctx: TaskContext, asset: dict[str, Any]) -> Listing:
    base = f"assets/{ctx.niche}/v{asset['version']}"
    meta = ctx.tools.files.read_json(f"{base}/listing.json")
    sample = ctx.tools.files.read_json(f"{base}/sample.json") if ctx.tools.files.exists(f"{base}/sample.json") else {}
    return Listing(
        asset_id=asset["id"],
        hypothesis_id=ctx.hypothesis["id"],
        niche=ctx.niche,
        title=meta["name"],
        summary=meta["summary"],
        description_md=meta.get("description_markdown", ""),
        price_cents=int(meta["price_cents"]),
        zip_path=asset["path"],
        sample_rows=sample.get("rows", []),
        sample_columns=sample.get("fields", []),
    )


def render_showcase_md(listing: Listing, checkout_url: str, lander_url: str = "") -> str:
    cols = listing.sample_columns
    lines = [
        f"# {listing.title}: free preview",
        "",
        listing.summary,
        "",
        f"This preview shows {len(listing.sample_rows)} sanitized records. Contact details and full descriptions are omitted.",
        "",
        "| " + " | ".join(c.replace("_", " ") for c in cols) + " |",
        "|" + "---|" * len(cols),
    ]
    for r in listing.sample_rows:
        lines.append("| " + " | ".join(_cell(r.get(c)).replace("|", "\\|") for c in cols) + " |")
    lines.append("")
    if checkout_url:
        buy = checkout_link(checkout_url, "github", f"showcase_{listing.niche}")
        lines.append(f"**[Get the complete dataset (${listing.price_cents / 100:.2f})]({buy})**")
    else:
        lines.append("_Complete dataset coming soon._")
    if lander_url:
        lines += ["", f"More details: {add_utm(lander_url, 'github', 'showcase', listing.niche)}"]
    return "\n".join(lines) + "\n"


def fulfil_order(tools, order: dict[str, Any]) -> str:
    """Deliver one paid order. Shared by the webhook (instant) and ``deliver_orders`` (retry sweep).

    Returns ``delivered``, ``dry_run``, ``failed`` or ``manual``. Safe to call twice: an order that is
    no longer ``paid`` is left alone."""
    current = tools.state.get_order(order["provider"], order["order_id"]) or order
    if current["status"] != "paid":
        return {"delivered": "delivered", "delivering": "in_progress"}.get(current["status"], "manual")
    if not tools.state.claim_order_for_delivery(current["id"]):
        return "in_progress"  # another worker got there first
    asset = tools.state.get_asset(current["asset_id"]) if current["asset_id"] else None
    if asset is None or not current["email"]:
        tools.state.set_order_status(current["id"], "needs_manual_delivery")
        return "manual"
    try:
        outcome = tools.dispatcher.deliver(current, asset["title"], tools.files.resolve(asset["path"]))
    except Exception as exc:  # noqa: BLE001
        tools.state.release_order(current["id"])
        tools.state.record_delivery_failure(current["id"])
        tools.state.log_error("fulfilment", f"order {current['provider']}:{current['order_id']} delivery failed: {exc!r}")
        return "failed"
    if outcome == "delivered":
        tools.state.set_order_status(current["id"], "delivered")
    else:
        tools.state.release_order(current["id"])
    return outcome


class DistributionEngine(Strategy):
    name = "distribution_engine"
    tasks = ("publish_listing", "publish_showcase", "dispatch_outreach", "deliver_orders", "collect_metrics")

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        return getattr(self, task)(ctx)

    def _asset(self, ctx: TaskContext) -> dict[str, Any] | None:
        return ctx.tools.state.latest_asset(ctx.hypothesis["id"], ASSET_KIND)

    # ------------------------------------------------------------------ publish_listing
    def publish_listing(self, ctx: TaskContext) -> TaskResult:
        tools = ctx.tools
        asset = self._asset(ctx)
        if asset is None:
            return TaskResult(True, "nothing packaged yet", {"published": False})
        storefront = tools.storefront
        if asset.get("status") == "published" and asset.get("provider") == storefront.name:
            return TaskResult(True, f"v{asset['version']} already live on {storefront.name}", {"published": False})

        can_email = tools.dispatcher.can_deliver()
        if storefront.name == "stripe" and not (can_email or tools.config.allow_manual_fulfillment):
            return TaskResult(
                True,
                "not opening a Stripe checkout: the agent can't deliver the file yet "
                "(set dry_run=false and an email backend, or allow_manual_fulfillment=true)",
                {"published": False, "blocked": "fulfilment"},
            )
        listing = _load_listing(ctx, asset)
        # The asset's own checkout (if any) wins, then the newest earlier version that had one.
        previous = asset if asset.get("checkout_url") else next(
            (a for a in tools.state.list_assets(ctx.hypothesis["id"]) if a["id"] != asset["id"] and a.get("checkout_url")),
            None,
        )
        result = storefront.publish(listing, previous)
        if result.live and not (result.provider_delivers or can_email or tools.config.allow_manual_fulfillment):
            return TaskResult(
                True, f"{storefront.name} checkout created but withheld: nobody can deliver the file ({result.detail})",
                {"published": False, "blocked": "fulfilment"},
            )
        deployer = PagesDeployer(tools.config, tools.files, tools.github)
        lander_url = deployer.deploy(listing, result.checkout_url if result.live else "")
        fields: dict[str, Any] = {"provider": result.provider, "lander_url": lander_url or None}
        if result.live:
            fields.update(checkout_url=result.checkout_url, status="published")
        if result.product_ref:
            fields["product_ref"] = result.product_ref
        tools.state.update_asset(asset["id"], **fields)
        return TaskResult(
            True,
            f"{'live' if result.live else 'staged'} on {result.provider}: {result.checkout_url or 'no checkout yet'} ({result.detail})",
            {"published": result.live, "provider": result.provider, "checkout_url": result.checkout_url, "lander_url": lander_url},
        )

    # ------------------------------------------------------------------ publish_showcase
    def publish_showcase(self, ctx: TaskContext) -> TaskResult:
        tools = ctx.tools
        cfg = tools.config
        asset = self._asset(ctx)
        if asset is None:
            return TaskResult(True, "nothing packaged yet", {"showcased": False})
        listing = _load_listing(ctx, asset)
        if not listing.sample_rows:
            return TaskResult(True, "no sample records", {"showcased": False})
        md = render_showcase_md(listing, asset.get("checkout_url") or "", asset.get("lander_url") or "")
        tools.files.write_text(f"showcase/{ctx.niche}/README.md", md)
        url = ""
        if tools.github.configured():
            if cfg.github_showcase_mode == "gist":
                key = f"gist:{ctx.niche}"
                gist = tools.github.upsert_gist(tools.state.get(key), f"{listing.title}: free preview", {f"{ctx.niche}-preview.md": md})
                tools.state.set(key, gist["id"])
                url = gist["html_url"]
            elif cfg.github_showcase_repo:
                res = tools.github.put_file(
                    cfg.github_showcase_repo, f"showcase/{ctx.niche}/README.md", md,
                    f"Update {ctx.niche} showcase", cfg.github_branch,
                )
                url = res.get("html_url") or f"https://github.com/{cfg.github_showcase_repo}/tree/{cfg.github_branch}/showcase/{ctx.niche}"
        if url:
            tools.state.update_asset(asset["id"], showcase_url=url)
        return TaskResult(
            True, f"showcase {'published at ' + url if url else 'written locally (no GitHub token/repo)'}",
            {"showcased": True, "url": url},
        )

    # ------------------------------------------------------------------ dispatch_outreach
    def dispatch_outreach(self, ctx: TaskContext) -> TaskResult:
        tools = ctx.tools
        cfg = tools.config
        unsubscribed: list[str] = []
        if cfg.imap_host and cfg.imap_username:
            from tools.inbox import poll_unsubscribes

            unsubscribed = poll_unsubscribes(tools.state, cfg.imap_host, cfg.imap_username, cfg.imap_password)
        report = tools.dispatcher.dispatch_approved()
        if report.reasons and not (report.sent or report.dry_run) and tools.dispatcher.live:
            tools.state.log_error("dispatcher", "; ".join(report.reasons[:5]), kind="compliance")
        mode = "live" if tools.dispatcher.live else "dry-run"
        return TaskResult(
            True,
            f"{mode}: {report.sent} sent, {report.dry_run} audited, {report.blocked} blocked, {report.deferred} deferred "
            f"(limit {report.limit}/day), {len(unsubscribed)} unsubscribes honoured",
            {"sent": report.sent, "dry_run": report.dry_run, "blocked": report.blocked, "failed": report.failed,
             "deferred": report.deferred, "limit": report.limit, "unsubscribed": len(unsubscribed)},
        )

    # ------------------------------------------------------------------ deliver_orders
    def deliver_orders(self, ctx: TaskContext) -> TaskResult:
        tools = ctx.tools
        delivered = pending = failed = 0
        tools.state.reset_stale_deliveries()
        for order in tools.state.orders_to_deliver():
            outcome = fulfil_order(tools, order)
            delivered += outcome == "delivered"
            pending += outcome == "dry_run"
            failed += outcome == "failed"
        manual = tools.state.order_counts().get("needs_manual_delivery", 0)
        return TaskResult(
            True,
            f"{delivered} delivered, {pending} awaiting live email (dry run), {failed} failed, {manual} need manual delivery",
            {"delivered": delivered, "pending": pending, "failed": failed, "manual": manual},
        )

    # ------------------------------------------------------------------ collect_metrics
    def collect_metrics(self, ctx: TaskContext) -> TaskResult:
        tools = ctx.tools
        cfg = tools.config
        hid = ctx.hypothesis["id"]
        views = None
        if tools.github.configured() and cfg.github_showcase_repo and cfg.github_showcase_mode == "repo":
            try:
                paths = tools.github.popular_paths(cfg.github_showcase_repo)
            except Exception as exc:  # noqa: BLE001 - metrics are best effort
                tools.state.log_error("metrics", f"GitHub traffic unavailable: {exc!r}")
            else:
                needle = f"/showcase/{ctx.niche}"
                views = sum(int(p.get("count", 0)) for p in paths if needle in str(p.get("path", "")))
                tools.state.set_metric(hid, "views", "github_traffic", views)
                tools.state.set("view_tracking", True)
        assets = tools.state.list_assets(hid)
        surfaces = sum(1 for k in ("checkout_url", "showcase_url", "lander_url") if assets and assets[0].get(k))
        tools.state.set_metric(hid, "impressions", "outreach_sent", tools.state.outreach_sent_for(hid))
        tools.state.set_metric(hid, "impressions", "surfaces", surfaces)
        tools.state.set_metric(hid, "purchases", "orders", tools.state.purchases_for_hypothesis(hid))
        m = tools.state.metrics_for_hypothesis(hid)
        return TaskResult(
            True,
            f"impressions {m.get('impressions', 0)} · views {m.get('views', 0) if views is not None else 'n/a'} · purchases {m.get('purchases', 0)}",
            m,
        )
