"""Rich terminal dashboard."""

from __future__ import annotations

from typing import Any

from rich.console import Group
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

BREAKER_STYLE = {"OK": "green", "WARN": "yellow", "DEGRADED": "dark_orange", "TRIPPED": "bold red", "STOPPED": "bold red"}


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    return (f"{days}d " if days else "") + f"{hours:02d}:{minutes:02d}:{secs:02d}"


def dollars(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _objective_panel(snap: dict[str, Any]) -> Panel:
    hyp = snap["hypothesis"]
    t = Table.grid(padding=(0, 1))
    t.add_column(style="bold", no_wrap=True)
    t.add_column(ratio=1)
    t.add_row("Objective", snap["objective"])
    if hyp:
        t.add_row("Hypothesis", Text(f"#{hyp['id']} {hyp['key']}", style="cyan"))
        t.add_row("", hyp["description"])
        progress = Text(
            f"iterations {hyp['iterations']}/{hyp['pivot_after']} before zero-traction pivot · "
            f"attributed revenue {dollars(hyp['revenue_cents'])}"
        )
        if hyp.get("origin"):
            progress.append(f" · origin: {hyp['origin']}", style="dim")
        t.add_row("", progress)
    else:
        t.add_row("Hypothesis", Text("none active", style="yellow"))
    t.add_row(
        "Explored",
        Text(f"{snap['hypotheses']['total']} hypotheses ({snap['hypotheses']['deprecated']} deprecated)", style="dim"),
    )
    return Panel(t, title="Active Objective", border_style="cyan")


def _runtime_table(snap: dict[str, Any]) -> Table:
    t = Table.grid(padding=(0, 2))
    t.add_column(style="bold")
    t.add_column()
    t.add_row("Iteration", str(snap["iteration"]))
    t.add_row("Uptime", fmt_duration(snap["uptime_seconds"]))
    t.add_row("Last cycle", (snap["last_cycle_at"] or "never").replace("T", " ")[:19])
    t.add_row("Process", f"pid {snap['pid']}" if snap["pid"] else "not running")
    t.add_row("Actions", f"{snap['actions_total']} ({snap['actions_failed']} failed)")
    return t


def _pipeline_table(snap: dict[str, Any]) -> Table:
    t = Table.grid(padding=(0, 2))
    t.add_column(style="bold")
    t.add_column()
    t.add_row("Leads (all niches)", str(snap["leads_total"]))
    t.add_row("Leads (active niche)", str(snap["leads_active_niche"]))
    t.add_row("Assets staged", str(snap["assets_total"]))
    for asset in snap["assets"][:3]:
        link = f" → {asset['product_ref']}" if asset["product_ref"] else " (not listed)"
        t.add_row("", f"{asset['title']} v{asset['version']} · {asset['lead_count']} rows{link}")
    outreach = snap["outreach"]
    t.add_row(
        "Outreach",
        f"{outreach.get('pending_review', 0)} pending review · {outreach.get('approved', 0)} approved · "
        f"{outreach.get('sent', 0)} sent",
    )
    return t


def _revenue_panel(snap: dict[str, Any]) -> Panel:
    today = snap["revenue_today"]
    pct = min(1.0, today["progress"])
    style = "green" if today["target_met"] else ("yellow" if pct > 0 else "red")
    header = Text.assemble(
        ("Today ", "bold"),
        (dollars(today["net_cents"]), f"bold {style}"),
        (f" / {dollars(today['target_cents'])} target  ({today['progress']:.0%})", ""),
    )
    detail = Text(
        f"{today['sales']} verified sales · gross {dollars(today['gross_cents'])} · fees {dollars(today['fee_cents'])}"
        + (f" · {dollars(today['unverified_cents'])} unverified (excluded)" if today["unverified_cents"] else ""),
        style="dim",
    )
    hist = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
    for row in snap["revenue_history"]:
        hist.add_column(row["date"][5:], justify="right")
    hist.add_row(
        *[
            Text(dollars(r["net_cents"]), style="green" if r["target_met"] else ("yellow" if r["net_cents"] else "dim"))
            for r in snap["revenue_history"]
        ]
    )
    bar = ProgressBar(total=100, completed=pct * 100, width=50, complete_style=style)
    return Panel(Group(header, bar, detail, hist), title="Daily Revenue vs Target", border_style=style)


def _health_panel(snap: dict[str, Any]) -> Panel:
    br = snap["breaker"]
    status = br["status"]
    style = BREAKER_STYLE.get(status, "white")
    head = Text.assemble(
        ("Circuit breaker ", "bold"),
        (status, style),
        (f"  consecutive errors {br['consecutive_errors']}/{br['max_consecutive_errors']}", ""),
        (f"  operational failures {snap['operational_failures']}", "dim"),
    )
    parts: list[Any] = [head]
    if br["trip_reason"] or br["emergency_stop"]:
        parts.append(Text(f"STOP: {br['emergency_stop'] or br['trip_reason']}  (run `automonetize resume`)", style="bold red"))
    errors = Table(show_header=True, header_style="bold", box=None, padding=(0, 1), expand=True)
    errors.add_column("time", no_wrap=True, style="dim")
    errors.add_column("source", no_wrap=True)
    errors.add_column("kind", no_wrap=True)
    errors.add_column("message", overflow="ellipsis", no_wrap=True, ratio=1)
    for e in snap["recent_errors"]:
        errors.add_row(e["created_at"][11:19], e["source"], e["kind"], e["message"])
    if not snap["recent_errors"]:
        errors.add_row("", "", "", Text("no errors recorded", style="green"))
    parts.append(errors)
    return Panel(Group(*parts), title="Error Log & Circuit Health", border_style=style)


def _actions_panel(snap: dict[str, Any]) -> Panel:
    t = Table(show_header=True, header_style="bold", box=None, padding=(0, 1), expand=True)
    t.add_column("cycle", justify="right", style="dim")
    t.add_column("action", no_wrap=True)
    t.add_column("status", no_wrap=True)
    t.add_column("detail", overflow="ellipsis", no_wrap=True, ratio=1)
    for a in snap["recent_actions"]:
        colour = {"ok": "green", "failed": "red", "skipped": "yellow"}.get(a["status"], "white")
        t.add_row(str(a["cycle"]), a["name"], Text(a["status"], style=colour), a["detail"] or "")
    return Panel(t, title="Recent Actions", border_style="blue")


def render_dashboard(snap: dict[str, Any]) -> Group:
    runtime = Table.grid(expand=True, padding=(0, 2))
    runtime.add_column(ratio=1)
    runtime.add_column(ratio=2)
    runtime.add_row(
        Panel(_runtime_table(snap), title="Runtime", border_style="magenta"),
        Panel(_pipeline_table(snap), title="Staged Assets / Leads", border_style="magenta"),
    )
    return Group(
        Text("AutoMonetize", style="bold white on blue", justify="center"),
        _objective_panel(snap),
        runtime,
        _revenue_panel(snap),
        _health_panel(snap),
        _actions_panel(snap),
    )
