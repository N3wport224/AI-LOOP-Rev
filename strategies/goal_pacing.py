"""Goal pacing: how far the last 7 days are from $10/day, and the one step that would help most.

``pace_goal`` recomputes once a day (kv ``goal_pace``) from the same analytics as
``automonetize analytics``: 7-day net per day against ``daily_target_cents``, the projected month,
the best product and channel, how many people reached checkout and how many paid. Then it picks a
single next step from plain rules, most fundamental first:

1. nothing on sale → go live;
2. a product can't be bought (storefront health) → fix that;
3. goal met → keep going, share on one more channel;
4. nobody reached checkout → traffic: connect marketing, post the share kit;
5. people reached checkout but few paid → the price or the page (price tests run by themselves);
6. otherwise → more of what works (the best channel so far).

Shown in the daily report, the control panel (**Goal pace** card) and ``automonetize pace``.
"""

from __future__ import annotations

from typing import Any

from strategies.base import Strategy, TaskContext, TaskResult

KEY = "goal_pace"
LOW_CONVERSION = 0.3


def _best(buckets: dict[str, dict[str, Any]]) -> str:
    named = [(k, v) for k, v in buckets.items() if v.get("orders")]
    return max(named, key=lambda kv: kv[1].get("net_cents", 0))[0] if named else ""


def next_step(cfg: Any, products: int, broken: list[str], net_per_day: int, goal: int, started: int, completed: int,
              best_channel: str) -> str:
    if not products:
        return "Nothing is on sale yet: run `automonetize go-live`."
    if broken:
        return f"Fix the storefront first: {broken[0]} (`automonetize doctor` shows details)."
    if goal and net_per_day >= goal:
        return "You're on goal. Keep it there: post this week's share kit on one channel you haven't tried."
    if not started:
        if not (cfg.github_pages_repo and cfg.pages_base_url):
            return "Nobody reached checkout this week. Put your products on a public site: `automonetize connect-marketing`."
        return "Nobody reached checkout this week. Traffic is the bottleneck: post the share kit (control panel → Share)."
    if completed / started < LOW_CONVERSION:
        return (f"{started} people opened checkout but only {completed} paid. The price tests adjust prices by themselves; "
                "check that the product page explains what's inside.")
    if best_channel and best_channel not in ("direct", "manual"):
        return f"Sales are coming from {best_channel}: post there again this week (share kit has a ready post)."
    return "Sales are coming in. More visitors is the lever: post the share kit on one more channel."


def compute_pace(state: Any, cfg: Any) -> dict[str, Any]:
    from dashboard.analytics import compute
    from strategies.storefront_health import KEY as HEALTH
    from tools.catalog import live_products

    week = compute(state, cfg, "7d")
    by_channel = week.get("by_channel", {})
    started = sum(int(b.get("checkouts_started") or 0) for b in by_channel.values())
    completed = sum(int(b.get("checkouts_completed") or 0) for b in by_channel.values())
    broken = [f"{p['title']}: {', '.join(p['problems'])}" for p in (state.get(HEALTH) or {}).get("products", []) if not p["ok"]]
    net, goal = int(week["net_per_day_cents"]), int(week["goal_cents"])
    best_channel, best_niche = _best(by_channel), _best(week.get("by_niche", {}))
    return {
        "date": state.clock().date().isoformat(),
        "net_per_day_cents": net, "goal_cents": goal, "progress": round(net / goal, 4) if goal else None,
        "projected_month_cents": net * 30, "week_net_cents": int(week["totals"]["net_cents"]),
        "orders": int(week["totals"]["orders"]), "checkouts_started": started, "checkouts_completed": completed,
        "best_channel": best_channel, "best_niche": best_niche,
        "next_step": next_step(cfg, len(live_products(state)), broken, net, goal, started, completed, best_channel),
    }


def describe(pace: dict[str, Any] | None) -> str:
    if not pace:
        return "Pace: worked out after the next cycle."
    pct = f" ({pace['progress'] * 100:.0f}% of goal)" if pace.get("progress") is not None else ""
    best = ", ".join(x for x in (f"best channel {pace['best_channel']}" if pace.get("best_channel") else "",
                                 f"best niche {pace['best_niche']}" if pace.get("best_niche") else "") if x)
    return (f"Pace (7 days): ${pace['net_per_day_cents'] / 100:,.2f}/day of ${pace['goal_cents'] / 100:,.2f}{pct}, "
            f"about ${pace['projected_month_cents'] / 100:,.0f}/month"
            + (f"; {best}" if best else "") + f"\nNext step: {pace['next_step']}")


class GoalPacing(Strategy):
    name = "goal_pacing"
    tasks = ("pace_goal",)

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        state = ctx.tools.state
        current = state.get(KEY) or {}
        if current.get("date") == state.clock().date().isoformat():
            return TaskResult(True, "pace up to date", {})
        pace = compute_pace(state, ctx.tools.config)
        state.set(KEY, pace)
        return TaskResult(True, describe(pace).replace("\n", " · ")[:300], {"progress": pace["progress"]})
