"""`automonetize` command-line interface."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Sequence

from rich.console import Console
from rich.live import Live
from rich.table import Table

from agent.config import Config
from agent.engine import CycleReport, Engine, ProcessLock
from agent.state import StateStore
from dashboard.render import render_dashboard
from dashboard.snapshot import collect_snapshot
from tools.revenue_tracker import RevenueTracker

EXAMPLE_CONFIG = """# AutoMonetize configuration. Environment variables AUTOMONETIZE_<KEY> override these.
# Secrets belong in the environment (.env), not here: STRIPE_SECRET_KEY, LEMONSQUEEZY_API_KEY,
# GITHUB_TOKEN, SENDGRID_API_KEY, POSTMARK_SERVER_TOKEN, SMTP_PASSWORD, IMAP_PASSWORD, GUMROAD_ACCESS_TOKEN,
# STRIPE_WEBHOOK_SECRET, DEVTO_API_KEY, HASHNODE_TOKEN.
[automonetize]
data_dir = "data"
interval_seconds = 3600          # one cycle per hour
pivot_after_iterations = 24      # cycles with zero verified revenue before a pivot...
min_hypothesis_days = 10         # ...and never before a niche is this old (hard evidence still pivots at once)
stale_revenue_days = 14          # a niche whose last sale is older than this is re-evaluated
signal_window_iterations = 12    # cycles with zero views AND zero sales before a pivot (needs GitHub traffic)
daily_target_cents = 1000        # $10.00/day
max_actions_per_cycle = 45
max_api_calls_per_cycle = 60
max_consecutive_errors = 5
http_rate_per_minute = 20
respect_robots_txt = true
min_leads_for_asset = 10
lead_sources = ["remoteok", "arbeitnow", "hn_hiring"]

# Storefront: "auto" = Stripe if configured, else Lemon Squeezy, else Gumroad staging.
storefront_provider = "auto"
price_tiers = [[0, 900], [25, 1400], [75, 1900]]  # starting one-off price: [companies >=, cents]
# lemonsqueezy_store_id = ""
# lemonsqueezy_variant_id = ""                     # shared "dataset" variant created once in the dashboard
# [automonetize.lemonsqueezy_variant_map]          # optional: one variant per niche (exact attribution)
# python-remote = "123456"
# [automonetize.stripe_payment_links]              # optional: pre-made links when you don't use a secret key
# python-remote = "https://buy.stripe.com/..."
allow_manual_fulfillment = false

# GitHub: showcase samples, Pages landers, traffic-based view tracking
# github_showcase_repo = "you/datasets"            # samples go to showcase/<niche>/README.md
# github_showcase_mode = "repo"                    # or "gist"
# github_pages_repo = "you/you.github.io"          # landers go to docs/<niche>/index.html
# pages_base_url = "https://you.github.io"

# Email. Nothing is sent until dry_run = false. Cold outreach additionally requires human approval.
dry_run = true
outreach_email_backend = "smtp"  # your own mailbox; see README before using sendgrid/postmark for outreach
email_backend = ""               # for order delivery; defaults to outreach_email_backend
smtp_host = ""
smtp_port = 587
smtp_username = ""
sender_name = ""
sender_email = ""
sender_postal_address = ""       # required by CAN-SPAM for live outreach
unsubscribe_email = ""           # defaults to sender_email
unsubscribe_url = ""             # https URL enables RFC 8058 one-click unsubscribe
warmup_start_per_day = 5
warmup_step_per_week = 5
dispatch_max_per_day = 30
imap_host = ""                   # poll replies and honour "unsubscribe" automatically
imap_username = ""

# Real-time fulfilment: Stripe webhooks (secret from `stripe listen` or the Dashboard endpoint)
webhook_host = "127.0.0.1"
webhook_port = 8443
webhook_path = "/webhook"
network_check_hosts = ["api.stripe.com:443", "api.github.com:443"]  # [] disables the offline check

# Pricing engine: price experiments across these tiers (cents)
price_matrix = [900, 1400, 1900]  # one-off tiers the pricing engine tests ($9 / $14 / $19)
pricing_min_views = 20           # views without a sale before stepping down...
pricing_window_hours = 48        # ...once an experiment is this old
demand_sales_threshold = 3       # sales in 24h that trigger deeper scraping + a premium add-on
premium_price_cents = 1900

# Inbound: landers, sitemap and RSS on GitHub Pages, weekly syndicated radars
site_title = "Tech Stack Intel"
# github_pages_branch = "gh-pages"  # default: github_branch
# github_pages_dir = ""             # default "docs"; "" = branch root
# hashnode_publication_id = ""
# github_discussions_repo = "you/datasets"
syndication_publish = true       # false = Dev.to drafts only
syndication_interval_days = 5   # never less than 5 days per platform
hn_tracker_enabled = true        # monthly HN "Who is hiring?" stack breakdown as a public gist
og_images = true                 # PNG OpenGraph cards (pip install '.[images]'); SVG badges always

# Recurring tier: weekly delta updates every Monday
subscription_price_cents = 1000  # $10/month; 0 disables
subscription_interval = "month"
subscription_delivery_weekday = 0   # Monday
subscription_delivery_hour = 8
subscription_timezone = "UTC"
syndication_min_companies = 10

sender_skills = ["python", "django", "aws"]
outreach_offer = "short-term contract help"
outreach_daily_cap = 20

[[automonetize.niches]]
name = "python-remote"
keywords = ["python", "django", "fastapi", "flask"]

[[automonetize.niches]]
name = "devops-sre"
keywords = ["devops", "sre", "kubernetes", "terraform"]
"""


def _cents(value: str) -> int:
    return round(float(value.replace("$", "").replace(",", "")) * 100)


def _open(args: argparse.Namespace) -> tuple[Config, StateStore]:
    config = Config.load(args.config)
    config.ensure_dirs()
    return config, StateStore(config.db_path)


def _setup_logging(config: Config, headless: bool, verbose: bool) -> None:
    handlers: list[logging.Handler] = [logging.FileHandler(config.data_dir / "agent.log")]
    if headless:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


# --------------------------------------------------------------------------- commands
def cmd_init(args: argparse.Namespace, console: Console) -> int:
    path = Path(args.config or "automonetize.toml")
    if path.exists() and not args.force:
        console.print(f"[yellow]{path} already exists (use --force to overwrite)[/]")
    else:
        path.write_text(EXAMPLE_CONFIG)
        console.print(f"[green]wrote {path}[/]")
    config = Config.load(path)
    config.ensure_dirs()
    StateStore(config.db_path).close()
    console.print(f"[green]state database ready at {config.db_path}[/]")
    return 0


def cmd_run(args: argparse.Namespace, console: Console) -> int:
    config = Config.load(args.config)
    config.ensure_dirs()
    _setup_logging(config, args.headless, args.verbose)
    lock = ProcessLock(config.data_dir / "agent.lock")
    if not lock.acquire():
        console.print(f"[red]another agent instance is already running ({config.data_dir / 'agent.lock'})[/]")
        return 2
    try:
        engine = Engine(config)
        stopped, why = engine.is_stopped()
        if stopped:
            console.print(f"[red]refusing to start: {why}. Run `automonetize resume` first.[/]")
            return 3
        max_cycles = 1 if args.once else args.cycles
        interval = args.interval if args.interval is not None else config.interval_seconds
        log = logging.getLogger("automonetize.cli")

        def on_cycle(report: CycleReport) -> None:
            summary = "; ".join(f"{a['task']}={a['status']}" for a in report.actions) or report.message
            log.info("cycle %s %s hypothesis=%s %s", report.cycle, report.status, report.hypothesis_key, summary)
            for pivot in report.pivots:
                log.warning("pivot: %s", pivot)
            if not args.headless:
                console.print(render_dashboard(collect_snapshot(engine.state, config)))

        ran = engine.run_forever(interval=interval, max_cycles=max_cycles, on_cycle=on_cycle)
        log.info("loop exited after %s cycles", ran)
        return 0
    finally:
        lock.release()


def cmd_dashboard(args: argparse.Namespace, console: Console) -> int:
    config, state = _open(args)
    if not args.watch:
        console.print(render_dashboard(collect_snapshot(state, config)))
        return 0
    try:
        with Live(render_dashboard(collect_snapshot(state, config)), console=console, refresh_per_second=1, screen=False) as live:
            while True:
                time.sleep(args.refresh)
                live.update(render_dashboard(collect_snapshot(state, config)))
    except KeyboardInterrupt:
        return 0


def cmd_status(args: argparse.Namespace, console: Console) -> int:
    config, state = _open(args)
    snap = collect_snapshot(state, config)
    if args.json:
        print(json.dumps(snap, indent=2, default=str))
    else:
        today = snap["revenue_today"]
        hyp = snap["hypothesis"]
        console.print(
            f"iteration {snap['iteration']} · breaker {snap['breaker']['status']} · "
            f"today ${today['net_cents'] / 100:.2f}/${today['target_cents'] / 100:.2f} · "
            f"hypothesis {hyp['key'] if hyp else 'none'}"
        )
    return 0


def cmd_stop(args: argparse.Namespace, console: Console) -> int:
    config = Config.load(args.config)
    Engine(config).emergency_stop(args.reason)
    console.print(f"[red]emergency stop engaged[/]: {args.reason}")
    return 0


def cmd_resume(args: argparse.Namespace, console: Console) -> int:
    config = Config.load(args.config)
    Engine(config).resume()
    console.print("[green]pause and emergency stop cleared; circuit breaker reset[/]")
    return 0


def cmd_pause(args: argparse.Namespace, console: Console) -> int:
    config = Config.load(args.config)
    Engine(config).pause(args.reason)
    console.print("[yellow]engine paused: cycles are skipped; the webhook and lead capture stay up. "
                  "`automonetize resume` to continue.[/]")
    return 0


def cmd_gui(args: argparse.Namespace, console: Console) -> int:
    from gui.server import check_loopback, run_gui

    workdir = Path(__file__).resolve().parents[1]
    env_file = Path(args.env_file or workdir / ".env").resolve()
    try:
        check_loopback(args.host)
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        return 2
    try:
        run_gui(args.config, env_file, workdir, host=args.host, port=args.port, open_browser=not args.no_browser)
    except OSError as exc:
        console.print(f"[red]could not start the control panel: {exc}[/]")
        return 1
    return 0


def cmd_api(args: argparse.Namespace, console: Console) -> int:
    from api.auth import ApiKeys, send_welcome

    config, state = _open(args)
    keys = ApiKeys(state, config)
    if args.api_cmd == "usage":
        now = state.clock()
        for row in keys.volume(args.days, now):
            console.print(f"{row['day']}  {row['requests']:>7} requests")
        console.print(f"today by endpoint: {keys.by_endpoint(now) or '{}'}")
        return 0
    if args.api_cmd == "reissue":
        sub = state.subscriber(args.subscriber_id)
        if not sub:
            console.print(f"[red]no subscriber {args.subscriber_id}[/]")
            return 1
        for row in keys.for_subscriber(sub["id"]):
            keys.set_status(row["id"], "rotated", "reissued by operator")
        status = "degraded" if sub.get("subscription_status") == "past_due" else "active"
        from tools import build_toolkit
        from tools.circuit_breaker import CircuitBreaker

        tools = build_toolkit(config, state, CircuitBreaker(max_actions_per_cycle=100, max_api_calls_per_cycle=100,
                                                            max_consecutive_errors=100))
        key, row = keys.issue(sub["id"], sub.get("email"), status)
        outcome = send_welcome(tools, sub, key, row, rotated=True)
        console.print(f"new key {row['prefix']}… for subscriber {sub['id']}: email {outcome}")
        return 0 if outcome in ("delivered", "dry_run") else 1
    if args.api_cmd == "revoke":
        keys.set_status(args.key_id, "revoked", "revoked by operator")
        console.print(f"key {args.key_id} revoked")
        return 0
    table = Table(title="API keys")
    for col in ("id", "prefix", "subscriber", "email", "status", "quota/day", "used today", "last used"):
        table.add_column(col)
    now = state.clock()
    for row in keys.list():
        table.add_row(str(row["id"]), row["prefix"] + "…", str(row["subscriber_id"] or ""), row["email"] or "",
                      row["status"], str(row["daily_quota"]), str(keys.used_today(row["id"], now)), row["last_used_at"] or "")
    console.print(table)
    return 0


def cmd_hypotheses(args: argparse.Namespace, console: Console) -> int:
    _, state = _open(args)
    t = Table("id", "key", "status", "iterations", "revenue", "reason")
    for h in state.list_hypotheses():
        t.add_row(
            str(h["id"]), h["key"], h["status"], str(h["iterations"]),
            f"${state.revenue_for_hypothesis(h['id']) / 100:.2f}", h["reason"] or "",
        )
    console.print(t)
    return 0


def cmd_revenue(args: argparse.Namespace, console: Console) -> int:
    config = Config.load(args.config)
    config.ensure_dirs()
    if args.revenue_cmd == "sync":
        engine = Engine(config)
        tracker = engine.tools.revenue
        reports = tracker.sync_storefronts(engine.tools.storefronts, days_back=args.days)
        if tracker.gumroad_enabled():
            reports.append(tracker.sync_gumroad(days_back=args.days))
        if not reports:
            console.print("[yellow]no storefront credentials (Stripe, Lemon Squeezy or Gumroad); nothing to sync[/]")
            return 1
        for rep in reports:
            console.print(f"{rep.source}: fetched {rep.fetched}, {rep.new} new, +${rep.net_cents_added / 100:.2f} net")
        return 0
    state = StateStore(config.db_path)
    tracker = RevenueTracker(
        state, config.daily_target_cents, fee_pct=config.platform_fee_pct, fee_fixed_cents=config.platform_fee_fixed_cents
    )
    if args.revenue_cmd == "add":
        inserted = tracker.record_manual(
            _cents(args.amount), verified=args.verified, note=args.note, hypothesis_id=args.hypothesis,
            source=args.source, external_id=args.external_id, fees_included=not args.gross,
        )
        console.print("[green]recorded[/]" if inserted else "[yellow]duplicate external id, ignored[/]")
        return 0 if inserted else 1
    if args.revenue_cmd == "report":
        t = Table("date", "verified net", "sales", "unverified", "target met")
        for row in tracker.history(args.days):
            t.add_row(
                row["date"], f"${row['net_cents'] / 100:.2f}", str(row["sales"]),
                f"${row['unverified_cents'] / 100:.2f}", "yes" if row["target_met"] else "no",
            )
        console.print(t)
        if args.ledger:
            lt = Table("occurred", "source", "external id", "gross", "fees", "net", "verified", "hypothesis")
            for r in state.list_revenue(50):
                lt.add_row(
                    r["occurred_at"], r["source"], r["external_id"][:16], f"${r['gross_cents'] / 100:.2f}",
                    f"${r['fee_cents'] / 100:.2f}", f"${r['net_cents'] / 100:.2f}",
                    "yes" if r["verified"] else "no", str(r["hypothesis_id"] or ""),
                )
            console.print(lt)
        return 0
    return 1


def cmd_outreach(args: argparse.Namespace, console: Console) -> int:
    config, state = _open(args)
    if args.outreach_cmd == "list":
        rows = state.list_outreach(args.status, limit=args.limit)
        for r in rows:
            console.rule(f"#{r['id']} [{r['status']}] {r['channel']} → {r['recipient']} (score {r['score']:.2f})")
            console.print(f"[bold]{r['subject']}[/]")
            if args.full:
                console.print(r["body"])
        if not rows:
            console.print("no drafts")
        return 0
    if args.outreach_cmd in ("approve", "reject", "mark-sent"):
        status = {"approve": "approved", "reject": "rejected", "mark-sent": "sent"}[args.outreach_cmd]
        for oid in args.ids:
            ok = state.set_outreach_status(oid, status)
            console.print(f"#{oid}: {status if ok else 'not found'}")
        return 0
    if args.outreach_cmd == "export":
        rows = state.list_outreach("approved", limit=1000)
        out = config.data_dir / "outbox" / f"approved-{datetime.now():%Y%m%d-%H%M%S}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            "\n\n---\n\n".join(
                f"## #{r['id']} {r['channel']} → {r['recipient']}\n\n**Subject:** {r['subject']}\n\n{r['body']}" for r in rows
            )
            + "\n"
        )
        console.print(f"exported {len(rows)} approved drafts to {out} (send them yourself, then `outreach mark-sent`)")
        return 0
    return 1


def cmd_assets(args: argparse.Namespace, console: Console) -> int:
    _, state = _open(args)
    if args.assets_cmd == "link":
        state.link_product(args.asset_id, args.product_id)
        console.print(f"asset #{args.asset_id} linked to product {args.product_id}; its sales now count toward its hypothesis")
        return 0
    t = Table("id", "hypothesis", "title", "version", "rows", "price", "product", "path")
    for a in state.list_assets():
        t.add_row(
            str(a["id"]), str(a["hypothesis_id"]), a["title"], str(a["version"]), str(a["lead_count"]),
            f"${a['price_cents'] / 100:.2f}", a["product_ref"] or "-", a["path"],
        )
    console.print(t)
    return 0


def cmd_orders(args: argparse.Namespace, console: Console) -> int:
    config, state = _open(args)
    if args.orders_cmd == "deliver":
        engine = Engine(config, state=state)
        order = next((o for o in state.list_orders(limit=10000) if o["id"] == args.order_id), None)
        asset = state.get_asset(args.asset_id)
        if order is None or asset is None:
            console.print("[red]unknown order or asset id[/]")
            return 1
        email = args.email or order["email"]
        if not email:
            console.print("[red]order has no buyer email; pass --email[/]")
            return 1
        outcome = engine.tools.dispatcher.deliver({**order, "email": email}, asset["title"], engine.tools.files.resolve(asset["path"]))
        if outcome == "delivered":
            state.set_order_status(order["id"], "delivered", asset_id=asset["id"])
        console.print(f"order #{order['id']}: {outcome}" + (" (dry run: see data/dispatched_audit.log)" if outcome == "dry_run" else ""))
        return 0
    t = Table("id", "provider", "order", "email", "gross", "asset", "status", "occurred")
    for o in state.list_orders(args.status, limit=args.limit):
        t.add_row(
            str(o["id"]), o["provider"], o["order_id"][:18], o["email"] or "-", f"${o['gross_cents'] / 100:.2f}",
            str(o["asset_id"] or "-"), o["status"], o["occurred_at"],
        )
    console.print(t)
    return 0


def cmd_dispatch(args: argparse.Namespace, console: Console) -> int:
    config = Config.load(args.config)
    engine = Engine(config)
    d = engine.tools.dispatcher
    problems = d.compliance_problems("outreach")
    console.print(f"mode: {'[green]LIVE[/]' if d.live else '[yellow]DRY RUN[/]'} · daily limit {d.daily_limit()}")
    for p in problems:
        console.print(f"[yellow]- {p}[/]")
    if args.check:
        return 0 if not problems else 1
    rep = d.dispatch_approved()
    console.print(
        f"sent {rep.sent} · audited {rep.dry_run} · blocked {rep.blocked} · failed {rep.failed} · deferred {rep.deferred}"
    )
    for r in rep.reasons[:20]:
        console.print(f"  {r}")
    return 0


def cmd_suppress(args: argparse.Namespace, console: Console) -> int:
    _, state = _open(args)
    if args.suppress_cmd == "add":
        for email in args.emails:
            console.print(f"{email}: {'suppressed' if state.suppress(email, args.reason) else 'already suppressed'}")
        return 0
    t = Table("email", "reason", "since")
    for r in state.list_suppressed():
        t.add_row(r["email"], r["reason"] or "", r["created_at"])
    console.print(t)
    return 0


def cmd_serve(args: argparse.Namespace, console: Console) -> int:
    from tools.storefront.stripe_pages_publisher import serve_directory

    config = Config.load(args.config)
    site = config.data_dir / "site"
    site.mkdir(parents=True, exist_ok=True)
    console.print(f"serving {site} at http://127.0.0.1:{args.port}/ (Ctrl+C to stop)")
    try:
        serve_directory(site, args.port)
    except KeyboardInterrupt:
        pass
    return 0


def cmd_supervise(args: argparse.Namespace, console: Console) -> int:
    from agent.supervisor import Supervisor

    config = Config.load(args.config)
    config.ensure_dirs()
    _setup_logging(config, args.headless, args.verbose)
    engine = Engine(config)
    log = logging.getLogger("automonetize.cli")
    stopped, why = engine.is_stopped()
    if stopped:
        # Don't exit: under launchd KeepAlive that's a restart loop every 30 s. Start anyway: the
        # engine skips cycles until resumed (or self-heals from a quarantine) and the webhook
        # listener keeps recording and fulfilling orders in the meantime.
        log.warning("starting paused: %s (webhook stays up; `automonetize resume` to continue)", why)
        console.print(f"[yellow]starting paused: {why}[/]")

    def on_cycle(report: CycleReport) -> None:
        summary = "; ".join(f"{a['task']}={a['status']}" for a in report.actions) or report.message
        log.info("cycle %s %s hypothesis=%s %s", report.cycle, report.status, report.hypothesis_key, summary)

    sup = Supervisor(config, engine=engine, webhook=not args.no_webhook, interval=args.interval,
                     max_cycles=args.cycles, on_cycle=on_cycle)
    try:
        reason = sup.run()
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/]")
        return 2
    log.info("supervisor exited: %s", reason)
    return 0


def cmd_webhook(args: argparse.Namespace, console: Console) -> int:
    import threading

    from tools.storefront.webhook_listener import WebhookProcessor, WebhookServer, sign_payload

    config = Config.load(args.config)
    config.ensure_dirs()
    engine = Engine(config)
    if not config.stripe_webhook_secret:
        console.print("[red]STRIPE_WEBHOOK_SECRET is not set (copy it from `stripe listen` or the Dashboard endpoint)[/]")
        return 1
    if args.selftest:
        payload = json.dumps({"id": "evt_selftest", "type": "customer.created", "data": {"object": {}}}).encode()
        out = WebhookProcessor(engine.tools).handle(payload, sign_payload(payload, config.stripe_webhook_secret))
        console.print(f"signature check: {'[green]ok[/]' if out.status == 200 else f'[red]{out.status} {out.body}[/]'}")
        return 0 if out.status == 200 else 1
    _setup_logging(config, True, args.verbose)
    server = WebhookServer(engine.tools, port=args.port)
    stop = threading.Event()
    console.print(f"listening on http://{server.host}:{server.port}{server.path} (Ctrl+C to stop)")
    try:
        server.run(stop)
    except KeyboardInterrupt:
        stop.set()
    return 0


def cmd_site(args: argparse.Namespace, console: Console) -> int:
    from strategies.inbound_syndicator import InboundSyndicator
    from strategies.base import TaskContext

    config, state = _open(args)
    engine = Engine(config, state=state)
    hyp = state.active_hypothesis() or {"id": 0, "params": {"niche": ""}, "iterations": 0}
    res = InboundSyndicator().build_site(TaskContext(engine.tools, hyp, {}))
    console.print(res.summary)
    return 0


def cmd_syndicate(args: argparse.Namespace, console: Console) -> int:
    from strategies.base import TaskContext
    from strategies.inbound_syndicator import InboundSyndicator

    config, state = _open(args)
    if args.drafts:
        config.syndication_publish = False
    engine = Engine(config, state=state)
    hyp = state.active_hypothesis()
    if hyp is None:
        console.print("[yellow]no active hypothesis yet[/]")
        return 1
    res = InboundSyndicator().syndicate(TaskContext(engine.tools, hyp, {}))
    console.print(res.summary)
    return 0


def cmd_pricing(args: argparse.Namespace, console: Console) -> int:
    config, state = _open(args)
    if args.pricing_cmd == "run":
        from agent.pricing_engine import PricingEngine

        hyp = state.active_hypothesis()
        if hyp is None:
            console.print("[yellow]no active hypothesis[/]")
            return 1
        report = PricingEngine(Engine(config, state=state).tools).run(hyp)
        for line in report["demand"] + report["decisions"]:
            console.print(line)
        return 0
    t = Table("exp", "asset", "price", "status", "views", "initiations", "started", "reason")
    for h in state.list_hypotheses():
        for e in state.experiments_for_hypothesis(h["id"]):
            t.add_row(str(e["id"]), str(e["asset_id"]), f"${e['price_cents'] / 100:.2f}", e["status"], str(e["views"]),
                      str(e["initiations"]), e["started_at"], e["reason"] or "")
    console.print(t)
    return 0


def cmd_analytics(args: argparse.Namespace, console: Console) -> int:
    from dashboard.analytics import compute, render

    config, state = _open(args)
    try:
        report = compute(state, config, args.period)
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        return 2
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        console.print(render(report))
    return 0


def cmd_subscriptions(args: argparse.Namespace, console: Console) -> int:
    config, state = _open(args)
    if args.subs_cmd == "deliver":
        from strategies.base import TaskContext
        from strategies.subscription_engine import SubscriptionEngine

        engine = Engine(config, state=state)
        hyp = state.active_hypothesis() or {"id": 0, "params": {"niche": ""}, "iterations": 0}
        res = SubscriptionEngine().deliver_subscriptions(TaskContext(engine.tools, hyp, {}))
        console.print(res.summary)
        return 0
    t = Table("id", "subscription", "email", "niche", "price", "status", "channel", "started", "last delivery")
    for s in state.list_subscribers(args.status):
        t.add_row(str(s["id"]), s["subscription_id"], s["email"] or "-", s["niche"] or "-",
                  f"${s['price_cents'] / 100:.2f}/{s['interval']}", s["subscription_status"], s["channel"] or "-",
                  s["started_at"][:10], (s["last_delivered_at"] or "-")[:16])
    console.print(t)
    return 0


# --------------------------------------------------------------------------- parser
def cmd_setup_autonomous(args: argparse.Namespace, console: Console) -> int:
    from agent.setup_autonomous import SetupOptions, load_env_into, run_setup, summarize
    from agent.connectivity import is_online
    from tools import build_toolkit
    from tools.circuit_breaker import CircuitBreaker

    workdir = Path(__file__).resolve().parents[1]
    env_file = Path(args.env_file or workdir / ".env").resolve()
    config = Config.load(args.config, env=load_env_into(env_file))
    config.ensure_dirs()
    state = StateStore(config.db_path)
    breaker = CircuitBreaker(max_actions_per_cycle=10_000, max_api_calls_per_cycle=10_000, max_consecutive_errors=10_000)
    tools = build_toolkit(config, state, breaker)
    opts = SetupOptions(env_file=env_file, workdir=workdir, require_live=args.live, skip_register=args.skip_register,
                        skip_launchd=args.skip_launchd, skip_handshake=args.skip_handshake, as_daemon=args.daemon)
    checks = run_setup(config, tools, opts, online_check=lambda: is_online(config.network_check_hosts))
    result = summarize(checks)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        table = Table(title="setup-autonomous")
        for col in ("step", "check", "", "detail"):
            table.add_column(col)
        icons = {"ok": "[green]ok[/]", "warn": "[yellow]warn[/]", "fail": "[red]FAIL[/]", "skip": "[dim]skip[/]"}
        for c in checks:
            table.add_row(c.step, c.name, icons.get(c.status, c.status), c.detail)
        console.print(table)
        console.print("[green]Unattended operation is set up.[/]" if result["ok"]
                      else "[red]Not ready: fix the FAIL rows and re-run (every step is idempotent).[/]")
    return 0 if result["ok"] else 1


def cmd_evolution(args: argparse.Namespace, console: Console) -> int:
    from agent.evolution.hot_reload import evolution_log, repo_root

    config, state = _open(args)
    elog = evolution_log(config, state.clock)
    sub = args.evolution_cmd
    if sub == "log":
        rows = elog.recent(args.limit)
        if args.json:
            print(json.dumps([{k: v for k, v in r.items() if k not in ("diff", "output")} for r in rows], indent=2))
            return 0
        t = Table(title="Evolution attempts (data/evolution_log.db)")
        for col in ("id", "when", "status", "kind", "title", "commit", "detail"):
            t.add_column(col, overflow="fold")
        colour = {"merged": "green", "failed": "red", "rejected": "red", "rolled_back": "yellow", "aborted": "dim"}
        for r in rows:
            t.add_row(str(r["id"]), r["created_at"][:16], f"[{colour.get(r['status'], 'white')}]{r['status']}[/]", r["kind"],
                      r["title"], (r["commit_sha"] or "")[:12], (r["detail"] or "")[:160])
        console.print(t)
        return 0
    if sub == "show":
        r = elog.get(args.id)
        if r is None:
            console.print(f"[red]no attempt {args.id}[/]")
            return 1
        console.print(f"[bold]#{r['id']} {r['title']}[/] · {r['status']} · {r['created_at']} → {r['finished_at'] or '…'}")
        console.print(f"branch {r['branch'] or '-'} · base {(r['base_sha'] or '')[:12]} · commit {(r['commit_sha'] or '-')[:12]} · "
                      f"fingerprint {r['fingerprint']}")
        console.print(f"{r['metric']}: {r['baseline']} → expected {r['expected']}\n{r['rationale']}\n\n{r['detail']}\n")
        console.print(r["diff"] or "(no diff)", markup=False, highlight=False)
        if args.output:
            console.print(r["output"] or "(no output)", markup=False, highlight=False)
        return 0
    if sub == "diagnose":
        from agent.evolution.diagnostics import diagnose

        findings = diagnose(state, config, repo_root(config), log=elog)
        if args.json:
            print(json.dumps([f.to_dict() for f in findings], indent=2, default=str))
            return 0
        if not findings:
            console.print("[green]no bottlenecks found[/]")
        for f in findings:
            console.print(f"[bold]{f.severity.upper()}[/] {f.kind}: {f.summary}")
            if f.hypothesis:
                h = f.hypothesis
                seen = " [dim](already tried)[/]" if elog.tried(h.fingerprint) else ""
                console.print(f"  patch: [cyan]{h.title}[/]{seen} · {h.metric}: {h.baseline:g} → {h.expected:g}")
                if args.diff:
                    console.print(h.diff(), markup=False, highlight=False)
        return 0
    if sub == "resume":
        elog.set_kv("halted", None)
        elog.set_kv("cooldown_until", None)
        console.print("[green]evolution un-halted and cooldown cleared[/]")
        return 0
    from agent.evolution.task import blocked

    summ = elog.summary()
    if args.json:
        print(json.dumps({**summ, "enabled": config.enable_autonomous_code_evolution, "blocked": blocked(config, elog, state)},
                         indent=2, default=str))
        return 0
    console.print(f"Autonomous code evolution: {'[green]ON[/]' if config.enable_autonomous_code_evolution else '[yellow]OFF[/]'}"
                  f" · repo {repo_root(config)}")
    console.print(f"attempted {summ['attempted']} · merged {summ['merged']} · rolled back {summ['rolled_back']} · failed "
                  f"{summ['failed']} · rejected {summ['rejected']} · blacklisted patches {summ['blacklisted']}")
    last = summ["last_commit"]
    console.print("last evolution: " + (f"{last['commit_sha'][:12]} {last['title']}" if last else "none"))
    canary = summ["canary"] or {}
    console.print(f"canary: {canary.get('status', 'none')}" + (f" ({canary.get('reason') or canary.get('until')})" if canary else ""))
    console.print("cooldown: " + (f"until {summ['cooldown_until']} ({summ['cooldown_reason']})" if summ["cooldown_until"] else "none"))
    if summ["halted"]:
        console.print(f"[red]HALTED[/]: {summ['halted']}")
    console.print(f"now: {blocked(config, elog, state) or 'ready to attempt at the next cycle'}")
    diag = summ["last_diagnosis"] or {}
    for f in diag.get("findings", [])[:10]:
        console.print(f"  • {f['severity']} {f['kind']}: {f['summary']}")
    return 0


def cmd_go_live(args: argparse.Namespace, console: Console) -> int:
    from cli.go_live import main as go_live

    return go_live(["--link"] if args.link else [])


def cmd_connect_marketing(args: argparse.Namespace, console: Console) -> int:
    from cli.connect_marketing import main as connect

    return connect([])


def cmd_doctor(args: argparse.Namespace, console: Console) -> int:
    from cli.doctor import main as doctor

    return doctor(["--fix"] if args.fix else [])


def cmd_autostart(args: argparse.Namespace, console: Console) -> int:
    from cli.doctor import main as doctor

    return doctor(["autostart"])


def cmd_backup(args: argparse.Namespace, console: Console) -> int:
    from cli.backups import main as backups

    return backups(["list"] if args.what == "list" else [])


def cmd_restore(args: argparse.Namespace, console: Console) -> int:
    from cli.backups import main as backups

    return backups(["restore", args.name] + (["--yes"] if args.yes else []))


def cmd_share(args: argparse.Namespace, console: Console) -> int:
    from cli.growth import share_main

    return share_main([])


def cmd_pace(args: argparse.Namespace, console: Console) -> int:
    from cli.growth import pace_main

    return pace_main([])


def cmd_heartbeat(args: argparse.Namespace, console: Console) -> int:
    from cli.growth import heartbeat_main

    return heartbeat_main([args.url] if args.url else [])


def cmd_todo(args: argparse.Namespace, console: Console) -> int:
    from cli.growth import todo_main

    return todo_main([])


def cmd_books(args: argparse.Namespace, console: Console) -> int:
    from cli.growth import books_main

    return books_main([args.month] if args.month else [])


def cmd_test_full_loop(args: argparse.Namespace, console: Console) -> int:
    from cli.test_loop import run

    return run(keep=args.keep, use_curl=not args.no_curl, color=False if args.no_color else None, json_out=args.json)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="automonetize", description="Autonomous zero-capital revenue agent")
    p.add_argument("--config", "-c", help="path to automonetize.toml")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="write an example config and create the state database")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("run", help="run the agent loop")
    s.add_argument("--once", action="store_true", help="run a single cycle and exit (for cron)")
    s.add_argument("--cycles", type=int, help="stop after N cycles")
    s.add_argument("--interval", type=float, help="seconds between cycles (overrides config)")
    s.add_argument("--headless", action="store_true", help="log only, no dashboard output")
    s.add_argument("--verbose", "-v", action="store_true")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("dashboard", help="show the terminal dashboard")
    s.add_argument("--watch", "-w", action="store_true", help="refresh continuously")
    s.add_argument("--refresh", type=float, default=2.0)
    s.set_defaults(func=cmd_dashboard)

    s = sub.add_parser("status", help="one-line status, or --json snapshot")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("stop", help="engage the emergency stop")
    s.add_argument("--reason", default="manual stop")
    s.set_defaults(func=cmd_stop)

    s = sub.add_parser("resume", help="clear a pause, the emergency stop or a quarantine, and reset the circuit breaker")
    s.set_defaults(func=cmd_resume)

    s = sub.add_parser("pause", help="skip engine cycles (webhook and lead capture stay up) until `resume`")
    s.add_argument("--reason", default="paused from the command line")
    s.set_defaults(func=cmd_pause)

    ap = sub.add_parser("api", help="Developer API keys and usage")
    apis = ap.add_subparsers(dest="api_cmd")
    apis.add_parser("keys", help="list keys (default)")
    au = apis.add_parser("usage", help="requests per day")
    au.add_argument("--days", type=int, default=14)
    ar = apis.add_parser("reissue", help="retire a subscriber's keys and email a new one (lost key)")
    ar.add_argument("subscriber_id", type=int)
    av = apis.add_parser("revoke", help="revoke one key")
    av.add_argument("key_id", type=int)
    ap.set_defaults(func=cmd_api, api_cmd="keys")

    g = sub.add_parser("gui", help="local control panel on http://127.0.0.1:8080 (opens your browser)")
    g.add_argument("--port", type=int, help="default: gui_port (8080)")
    g.add_argument("--host", default="127.0.0.1", help="loopback only: 127.0.0.1, localhost or ::1")
    g.add_argument("--env-file", help="default: <checkout>/.env")
    g.add_argument("--no-browser", action="store_true")
    g.set_defaults(func=cmd_gui)

    s = sub.add_parser("hypotheses", help="list hypotheses and their outcomes")
    s.set_defaults(func=cmd_hypotheses)

    rev = sub.add_parser("revenue", help="revenue ledger")
    rsub = rev.add_subparsers(dest="revenue_cmd", required=True)
    r = rsub.add_parser("add", help="record revenue received outside a synced storefront")
    r.add_argument("amount", help="dollars, e.g. 9.00")
    r.add_argument("--verified", action="store_true", help="you confirmed the funds arrived")
    r.add_argument("--gross", action="store_true", help="amount is gross; estimate platform fees")
    r.add_argument("--note", default="")
    r.add_argument("--source", default="manual")
    r.add_argument("--external-id", help="provider transaction id (makes the entry idempotent)")
    r.add_argument("--hypothesis", type=int, help="attribute to hypothesis id")
    r = rsub.add_parser("sync", help="pull sales from Gumroad")
    r.add_argument("--days", type=int, default=7)
    r = rsub.add_parser("report", help="daily totals vs target")
    r.add_argument("--days", type=int, default=7)
    r.add_argument("--ledger", action="store_true", help="also list individual entries")
    rev.set_defaults(func=cmd_revenue)

    out = sub.add_parser("outreach", help="review staged outreach drafts")
    osub = out.add_subparsers(dest="outreach_cmd", required=True)
    o = osub.add_parser("list")
    o.add_argument("--status", default="pending_review")
    o.add_argument("--limit", type=int, default=20)
    o.add_argument("--full", action="store_true", help="print message bodies")
    for name in ("approve", "reject", "mark-sent"):
        o = osub.add_parser(name)
        o.add_argument("ids", type=int, nargs="+")
    osub.add_parser("export", help="write approved drafts to data/outbox for manual sending")
    out.set_defaults(func=cmd_outreach)

    o = sub.add_parser("orders", help="storefront orders and fulfilment")
    osub2 = o.add_subparsers(dest="orders_cmd", required=True)
    ol = osub2.add_parser("list")
    ol.add_argument("--status")
    ol.add_argument("--limit", type=int, default=50)
    od = osub2.add_parser("deliver", help="deliver an order by hand (e.g. one flagged needs_manual_delivery)")
    od.add_argument("order_id", type=int)
    od.add_argument("asset_id", type=int)
    od.add_argument("--email")
    o.set_defaults(func=cmd_orders)

    d = sub.add_parser("dispatch", help="send approved outreach now (dry run unless dry_run=false)")
    d.add_argument("--check", action="store_true", help="only report mode, limit and compliance problems")
    d.set_defaults(func=cmd_dispatch)

    sp = sub.add_parser("suppress", help="do-not-contact list")
    ssub = sp.add_subparsers(dest="suppress_cmd", required=True)
    sa = ssub.add_parser("add")
    sa.add_argument("emails", nargs="+")
    sa.add_argument("--reason", default="manual")
    ssub.add_parser("list")
    sp.set_defaults(func=cmd_suppress)

    su = sub.add_parser("supervise", help="run engine + webhook daemon under supervision (for launchd/systemd)")
    su.add_argument("--no-webhook", action="store_true", help="engine only (polling sync)")
    su.add_argument("--interval", type=float)
    su.add_argument("--cycles", type=int)
    su.add_argument("--headless", action="store_true")
    su.add_argument("--verbose", "-v", action="store_true")
    su.set_defaults(func=cmd_supervise)

    sa = sub.add_parser("setup-autonomous", help="validate .env, preflight, register the Stripe webhook, load launchd, "
                        "verify the public URL end to end")
    sa.add_argument("--env-file", help="default: <checkout>/.env")
    sa.add_argument("--live", action="store_true", help="fail unless the Stripe key is a live-mode key")
    sa.add_argument("--daemon", action="store_true", help="install as LaunchDaemons (start at boot without login; uses sudo)")
    sa.add_argument("--skip-register", action="store_true")
    sa.add_argument("--skip-launchd", action="store_true")
    sa.add_argument("--skip-handshake", action="store_true")
    sa.add_argument("--json", action="store_true")
    sa.set_defaults(func=cmd_setup_autonomous)

    ev = sub.add_parser("evolution", help="autonomous code evolution: status, audit log, diagnosis")
    evs = ev.add_subparsers(dest="evolution_cmd")
    e = evs.add_parser("status", help="counts, last commit, canary, cooldown (default)")
    e.add_argument("--json", action="store_true")
    e = evs.add_parser("log", help="every attempt, newest first")
    e.add_argument("--limit", type=int, default=20)
    e.add_argument("--json", action="store_true")
    e = evs.add_parser("show", help="one attempt: rationale, diff, test output")
    e.add_argument("id", type=int)
    e.add_argument("--output", action="store_true", help="include the captured lint/pytest/simulator output")
    e = evs.add_parser("diagnose", help="run the diagnostics now (read-only)")
    e.add_argument("--diff", action="store_true")
    e.add_argument("--json", action="store_true")
    evs.add_parser("resume", help="clear a halt (after a failed rollback) and the cooldown")
    ev.set_defaults(func=cmd_evolution, evolution_cmd="status", json=False)

    gl = sub.add_parser("go-live", help="switch to real payments: checks the live Stripe key and email, saves, restarts, "
                        "prints your checkout link")
    gl.add_argument("--link", action="store_true", help="just print the live checkout link(s)")
    gl.set_defaults(func=cmd_go_live)

    cm = sub.add_parser("connect-marketing", help="paste a Dev.to key and a GitHub token: creates your public site, "
                        "switches on automatic articles and pages")
    cm.set_defaults(func=cmd_connect_marketing)

    dr = sub.add_parser("doctor", help="health check in plain words; --fix applies the safe fixes")
    dr.add_argument("--fix", action="store_true")
    dr.set_defaults(func=cmd_doctor)
    au = sub.add_parser("autostart", help="macOS: start the agent at login and restart it if it stops (launchd)")
    au.set_defaults(func=cmd_autostart)

    bk = sub.add_parser("backup", help="back up the agent's data now; `backup list` shows the backups")
    bk.add_argument("what", nargs="?", choices=["now", "list"], default="now")
    bk.set_defaults(func=cmd_backup)
    rs = sub.add_parser("restore", help="restore a backup (the current data is backed up first)")
    rs.add_argument("name")
    rs.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    rs.set_defaults(func=cmd_restore)

    sh = sub.add_parser("share", help="this week's ready-to-paste posts (LinkedIn, X, Reddit, DM) with tracked links")
    sh.set_defaults(func=cmd_share)
    pa = sub.add_parser("pace", help="7-day revenue pace vs the daily goal, and the one next step that would help most")
    pa.set_defaults(func=cmd_pace)
    hb = sub.add_parser("heartbeat", help="get an email if the agent stops: shows setup, or tests and saves a ping URL")
    hb.add_argument("url", nargs="?", help="ping URL, e.g. https://hc-ping.com/<id>")
    hb.set_defaults(func=cmd_heartbeat)
    td = sub.add_parser("todo", help="the few things only you can do, most valuable first, with the command for each")
    td.set_defaults(func=cmd_todo)
    bo = sub.add_parser("books", help="revenue spreadsheet for a month (default: last month) in data/exports/books")
    bo.add_argument("month", nargs="?", help="YYYY-MM")
    bo.set_defaults(func=cmd_books)

    tl = sub.add_parser("test-full-loop", help="end-to-end rehearsal in a sandbox: postings → intel → pages → four simulated "
                        "purchases → signed webhooks → deliveries → dashboard ≥ $10/day (never touches your data)")
    tl.add_argument("--keep", action="store_true", help="keep the sandbox directory for inspection")
    tl.add_argument("--no-curl", action="store_true", help="query the API with urllib instead of curl")
    tl.add_argument("--no-color", action="store_true")
    tl.add_argument("--json", action="store_true")
    tl.set_defaults(func=cmd_test_full_loop)

    wh = sub.add_parser("webhook", help="run only the Stripe webhook listener")
    wh.add_argument("--port", type=int)
    wh.add_argument("--selftest", action="store_true", help="verify the configured secret signs/verifies correctly")
    wh.add_argument("--verbose", "-v", action="store_true")
    wh.set_defaults(func=cmd_webhook)

    st = sub.add_parser("site", help="rebuild landers, sitemap and RSS feed (and push to GitHub Pages if configured)")
    st.set_defaults(func=cmd_site)

    sy = sub.add_parser("syndicate", help="publish this week's tech radar article")
    sy.add_argument("--drafts", action="store_true", help="create drafts only")
    sy.set_defaults(func=cmd_syndicate)

    pr = sub.add_parser("pricing", help="price experiments")
    psub = pr.add_subparsers(dest="pricing_cmd", required=True)
    psub.add_parser("status")
    psub.add_parser("run", help="evaluate pricing now")
    pr.set_defaults(func=cmd_pricing)

    an = sub.add_parser("analytics", help="revenue by niche, pricing tier and channel; MRR, churn, net/day vs goal")
    an.add_argument("--period", default="30d", help="7d, 30d, 12w, 3m, 1y or all (default 30d)")
    an.add_argument("--json", action="store_true")
    an.set_defaults(func=cmd_analytics)

    sb = sub.add_parser("subscriptions", help="recurring subscribers")
    sbs = sb.add_subparsers(dest="subs_cmd", required=True)
    sl = sbs.add_parser("list")
    sl.add_argument("--status", help="active, past_due, canceled...")
    sbs.add_parser("deliver", help="run this week's delivery now (respects dry_run; only if the Monday window has opened)")
    sb.set_defaults(func=cmd_subscriptions)

    sv = sub.add_parser("serve", help="serve data/site landers locally")
    sv.add_argument("--port", type=int, default=8000)
    sv.set_defaults(func=cmd_serve)

    a = sub.add_parser("assets", help="staged digital assets")
    asub = a.add_subparsers(dest="assets_cmd", required=True)
    asub.add_parser("list")
    al = asub.add_parser("link", help="link an asset to a storefront product id for revenue attribution")
    al.add_argument("asset_id", type=int)
    al.add_argument("product_id")
    a.set_defaults(func=cmd_assets)
    return p


def main(argv: Sequence[str] | None = None, console: Console | None = None) -> int:
    args = build_parser().parse_args(argv)
    console = console or Console()
    before = {id(s) for s in StateStore.open_stores()}
    try:
        return int(args.func(args, console) or 0)
    finally:
        # Close every database this command opened (directly or through an Engine), checkpointing
        # the WAL so a short-lived CLI call never leaves -wal/-shm growth or open handles behind.
        for store in StateStore.open_stores():
            if id(store) not in before:
                try:
                    store.checkpoint()
                finally:
                    store.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
