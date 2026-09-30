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
[automonetize]
data_dir = "data"
interval_seconds = 3600          # one cycle per hour
pivot_after_iterations = 24      # cycles with zero verified revenue before a pivot
daily_target_cents = 1000        # $10.00/day
max_actions_per_cycle = 8
max_api_calls_per_cycle = 60
max_consecutive_errors = 5
http_rate_per_minute = 20
respect_robots_txt = true
min_leads_for_asset = 10
asset_price_cents = 900
storefront_url = ""              # e.g. your Gumroad product URL, used on the showcase page
lead_sources = ["remoteok", "arbeitnow", "hn_hiring"]

# Outreach drafts are staged for review and never sent automatically.
sender_name = ""
sender_email = ""
sender_skills = ["python", "django", "aws"]
outreach_offer = "short-term contract help"
outreach_daily_cap = 20

# gumroad_access_token = ""      # prefer the GUMROAD_ACCESS_TOKEN env var

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
    console.print("[green]emergency stop cleared; circuit breaker reset[/]")
    return 0


def cmd_hypotheses(args: argparse.Namespace, console: Console) -> int:
    config, state = _open(args)
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
        if not engine.tools.revenue.gumroad_enabled():
            console.print("[yellow]GUMROAD_ACCESS_TOKEN is not set; nothing to sync[/]")
            return 1
        rep = engine.tools.revenue.sync_gumroad(days_back=args.days)
        console.print(f"fetched {rep.fetched} sales, {rep.new} new, +${rep.net_cents_added / 100:.2f} net")
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
    config, state = _open(args)
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


# --------------------------------------------------------------------------- parser
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

    s = sub.add_parser("resume", help="clear the emergency stop and reset the circuit breaker")
    s.set_defaults(func=cmd_resume)

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
    return int(args.func(args, console) or 0)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
