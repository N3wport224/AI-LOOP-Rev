"""Revenue velocity and acquisition attribution.

Everything is computed from verified data in the state DB:

* **Breakdowns** by niche, pricing tier and acquisition channel: verified revenue rows joined
  with their orders (the channel comes from the Stripe ``client_reference_id``, see
  ``tools/attribution.py``).
* **Channel conversion**: completed ÷ initiated Checkout Sessions per channel. Sessions carry the
  same ``client_reference_id`` as orders, so abandoned checkouts count against the channel that
  sent them. Page visits aren't observable on a static site, so this is checkout conversion.
* **MRR**: active and trialing subscribers normalised to a monthly amount (weekly × 52/12,
  yearly ÷ 12).
* **Churn**: subscribers cancelled in the period ÷ subscribers active at its start.
* **Net daily revenue** against ``daily_target_cents`` ($10.00): the period average, today, the
  number of days the goal was met, and how much of the goal MRR covers on its own.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from agent.config import Config
from agent.state import StateStore, iso

MONTHLY_FACTOR = {"day": 365 / 12, "week": 52 / 12, "month": 1.0, "year": 1 / 12}


def parse_period(text: str) -> timedelta | None:
    """``7d``, ``30d``, ``12w``, ``3m`` (30-day months), ``1y`` or ``all`` (None)."""
    text = (text or "").strip().lower()
    if text == "all":
        return None
    m = re.fullmatch(r"(\d+)\s*([dwmy])", text)
    if not m or int(m.group(1)) <= 0:
        raise ValueError(f"invalid period {text!r}: use e.g. 7d, 30d, 12w, 3m, 1y or all")
    n = int(m.group(1))
    return timedelta(days=n * {"d": 1, "w": 7, "m": 30, "y": 365}[m.group(2)])


def monthly_amount(price_cents: int, interval: str) -> float:
    return price_cents * MONTHLY_FACTOR.get(interval or "month", 1.0)


def _bucket() -> dict[str, int]:
    return {"orders": 0, "gross_cents": 0, "net_cents": 0}


def _sorted(d: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return dict(sorted(d.items(), key=lambda kv: (-kv[1]["net_cents"], kv[0])))


def compute(state: StateStore, config: Config, period: str = "30d", now: datetime | None = None) -> dict[str, Any]:
    now = (now or state.clock()).astimezone(timezone.utc)
    delta = parse_period(period)
    if delta is None:
        first = state.first_revenue_at()
        start = datetime.fromisoformat(first) if first else now - timedelta(days=1)
    else:
        start = now - delta
    days = max(1, round((now - start).total_seconds() / 86400))
    end = now + timedelta(seconds=1)  # include events stamped this very second

    assets: dict[int, dict[str, Any]] = {a["id"]: a for a in state.list_assets()}
    hyps = {h["id"]: h for h in state.list_hypotheses()}
    by_niche: dict[str, dict[str, Any]] = defaultdict(_bucket)
    by_tier: dict[str, dict[str, Any]] = defaultdict(_bucket)
    by_channel: dict[str, dict[str, Any]] = defaultdict(_bucket)
    totals = {"orders": 0, "gross_cents": 0, "fee_cents": 0, "net_cents": 0}
    for r in state.revenue_with_orders(start, end):
        asset = assets.get(r["asset_id"]) if r["asset_id"] else None
        niche = (asset or {}).get("niche") or (hyps.get(r["hypothesis_id"]) or {}).get("params", {}).get("niche") or "unattributed"
        if r["kind"] == "subscription":
            tier = f"subscription ${r['gross_cents'] / 100:.2f}"
        elif r["source"] == "manual":
            tier = "manual entry"
        else:
            tier = f"${r['gross_cents'] / 100:.2f} one-off"
        channel = r["channel"] or ("manual" if r["source"] == "manual" else "direct")
        for bucket in (by_niche[niche], by_tier[tier], by_channel[channel]):
            bucket["orders"] += 1
            bucket["gross_cents"] += r["gross_cents"]
            bucket["net_cents"] += r["net_cents"]
        totals["orders"] += 1
        for k in ("gross_cents", "fee_cents", "net_cents"):
            totals[k] += r[k]

    sessions = state.sessions_between(iso(start), iso(end))
    initiated: dict[str, int] = defaultdict(int)
    completed: dict[str, int] = defaultdict(int)
    for s in sessions:
        ch = s.get("channel") or "direct"
        initiated[ch] += 1
        completed[ch] += s["status"] == "complete"
    for ch in set(initiated) | set(by_channel):
        b = by_channel[ch]
        b["checkouts_started"] = initiated.get(ch, 0)
        b["checkouts_completed"] = completed.get(ch, 0)
        b["conversion"] = round(completed[ch] / initiated[ch], 4) if initiated.get(ch) else None

    subs = state.list_subscribers()
    live = [s for s in subs if s["subscription_status"] in ("active", "trialing")]
    mrr = round(sum(monthly_amount(s["price_cents"], s["interval"]) for s in live))
    start_iso, now_iso = iso(start), iso(end)
    active_at_start = [s for s in subs if s["started_at"] < start_iso and (not s["canceled_at"] or s["canceled_at"] >= start_iso)]
    canceled = [s for s in subs if s["canceled_at"] and start_iso <= s["canceled_at"] < now_iso]
    new_subs = [s for s in subs if start_iso <= s["started_at"] < now_iso]
    churned_base = [s for s in canceled if s["started_at"] < start_iso]
    churn = round(len(churned_base) / len(active_at_start), 4) if active_at_start else None

    goal = config.daily_target_cents
    daily = []
    for i in range(min(days, 366)):
        day = now - timedelta(days=i)
        daily.append(state.revenue_for_day(day)["net"])
    today = daily[0] if daily else 0
    net_per_day = round(totals["net_cents"] / days)
    mrr_daily = round(mrr * 12 / 365)
    return {
        "period": period, "start": start_iso, "end": now_iso, "days": days,
        "totals": {**totals, "avg_order_cents": round(totals["gross_cents"] / totals["orders"]) if totals["orders"] else 0},
        "net_per_day_cents": net_per_day,
        "today_net_cents": today,
        "goal_cents": goal,
        "goal_progress": round(net_per_day / goal, 4) if goal else None,
        "days_goal_met": sum(1 for d in daily if d >= goal),
        "by_niche": _sorted(dict(by_niche)),
        "by_tier": _sorted(dict(by_tier)),
        "by_channel": _sorted(dict(by_channel)),
        "subscriptions": {
            "active": len(live), "new": len(new_subs), "canceled": len(canceled),
            "active_at_start": len(active_at_start), "churn_rate": churn,
            "mrr_cents": mrr, "mrr_daily_cents": mrr_daily,
            "mrr_goal_coverage": round(mrr_daily / goal, 4) if goal else None,
        },
    }


def render(report: dict[str, Any]):
    """Rich renderables for ``automonetize analytics``."""
    from rich.console import Group
    from rich.table import Table
    from rich.text import Text

    def money(c: int | float) -> str:
        return f"${c / 100:,.2f}"

    t = report["totals"]
    s = report["subscriptions"]
    goal_pct = f"{report['goal_progress']:.0%}" if report["goal_progress"] is not None else "n/a"
    churn = "n/a" if s["churn_rate"] is None else f"{s['churn_rate']:.1%}"
    head = Text.assemble(
        (f"Last {report['period']} ({report['days']} days)  ", "bold"),
        (f"net {money(t['net_cents'])} · {t['orders']} payments · avg order {money(t['avg_order_cents'])}\n", ""),
        ("Net/day ", "bold"), (f"{money(report['net_per_day_cents'])} vs {money(report['goal_cents'])} goal ({goal_pct})", ""),
        (f" · today {money(report['today_net_cents'])} · goal met on {report['days_goal_met']} day(s)\n", ""),
        ("Recurring ", "bold"),
        (f"MRR {money(s['mrr_cents'])} ({money(s['mrr_daily_cents'])}/day, "
         f"{(s['mrr_goal_coverage'] or 0):.0%} of goal) · {s['active']} active · {s['new']} new · {s['canceled']} canceled · "
         f"churn {churn}", ""),
    )
    parts: list[Any] = [head]
    for key, title in (("by_channel", "Acquisition channel"), ("by_niche", "Niche"), ("by_tier", "Pricing tier")):
        tbl = Table(title=title, title_justify="left")
        tbl.add_column(title.split()[-1].lower())
        tbl.add_column("payments", justify="right")
        tbl.add_column("net", justify="right")
        tbl.add_column("gross", justify="right")
        if key == "by_channel":
            tbl.add_column("checkouts started", justify="right")
            tbl.add_column("conversion", justify="right")
        for name, b in report[key].items():
            row = [name, str(b["orders"]), money(b["net_cents"]), money(b["gross_cents"])]
            if key == "by_channel":
                row += [str(b.get("checkouts_started", 0)), "-" if b.get("conversion") is None else f"{b['conversion']:.0%}"]
            tbl.add_row(*row)
        if not report[key]:
            tbl.add_row("(no verified revenue in this period)", "", "", "", *([""] * (2 if key == "by_channel" else 0)))
        parts.append(tbl)
    return Group(*parts)
