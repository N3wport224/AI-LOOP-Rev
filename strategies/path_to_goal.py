"""Path to the daily goal (Phases 410-414): why the money isn't there yet, and the one next step.

A sale needs every link of a chain: real payments → something on sale → a place people can find
it → visitors → checkouts started → payments. ``diagnose`` checks each link from the agent's own
records and stops at the first broken one: that is the step that matters, everything after it
waits. It also does the arithmetic of the goal.

* **Phase 410, the chain:** live payments and real email; products on sale (live checkout links);
  a public site; ways in (Dev.to articles, posts from the share kit or marketing drafts you
  published in the last 14 days); checkouts started in the last 14 days; sales in the last 14 days.
* **Phase 411, the arithmetic:** at the average price on sale, after Stripe's fee, how many sales
  a day the goal takes, and roughly how many visitors a day that needs at a typical 1-2% visitor
  to buyer rate for low-priced data products. These are estimates and are labelled as such.
* **Phase 412, where it shows:** ``automonetize path``, the top of the control panel's status
  (``path`` in ``GET /api/status``), and a line in Monday's report.
* **Phase 413, what only you can do** is marked (``you``): the agent never creates accounts,
  never pays for anything and never posts under your name.
* **Phase 414, once on goal:** it says so and points at raising the goal.
"""

from __future__ import annotations

import math
from datetime import timedelta
from typing import Any

WINDOW_DAYS = 14
LOW_RATE, HIGH_RATE = 0.01, 0.02  # visitor → buyer, typical for $5-$30 digital products (an estimate)


def _since(state: Any, days: int) -> str:
    return (state.clock() - timedelta(days=days)).isoformat(timespec="seconds")


def _net_per_sale(cfg: Any, price_cents: int) -> int:
    pct = float(getattr(cfg, "stripe_fee_pct", 2.9) or 0)  # a percentage: 2.9 means 2.9%
    fee = round(price_cents * pct / 100) + int(getattr(cfg, "stripe_fee_fixed_cents", 30) or 0)
    return max(1, price_cents - fee)


def arithmetic(state: Any, cfg: Any) -> dict[str, Any]:
    """Phase 411: sales and visitors a day the goal takes, at today's prices."""
    from tools.catalog import live_products

    prices = [int(p["price_cents"]) for p in live_products(state) if int(p["price_cents"] or 0) > 0]
    avg = round(sum(prices) / len(prices)) if prices else int(getattr(cfg, "premium_price_cents", 1900) or 1900)
    goal = int(cfg.daily_target_cents)
    net = _net_per_sale(cfg, avg)
    sales = math.ceil(goal / net) if goal else 0
    return {"goal_cents": goal, "avg_price_cents": avg, "net_per_sale_cents": net, "sales_per_day": sales,
            "visitors_low": math.ceil(sales / HIGH_RATE), "visitors_high": math.ceil(sales / LOW_RATE),
            "priced_from_catalog": bool(prices)}


def diagnose(state: Any, cfg: Any) -> dict[str, Any]:
    """Phase 410: each link of the chain, the first broken one, and the arithmetic."""
    from tools.catalog import live_products

    since = _since(state, WINDOW_DAYS)
    steps: list[dict[str, Any]] = []

    def step(name: str, ok: bool, detail: str, fix: str = "", you: bool = False) -> None:
        steps.append({"name": name, "ok": bool(ok), "detail": detail, "fix": "" if ok else fix, "you": bool(you and not ok)})

    live_key = str(cfg.stripe_secret_key or "").startswith(("sk_live_", "rk_live_"))
    step("Real payments", live_key and not cfg.dry_run,
         "live Stripe key, real email" if live_key and not cfg.dry_run
         else ("test-mode Stripe key" if not live_key else "dry run: emails aren't sent, so files can't be delivered"),
         "roll your Stripe key, then automonetize go-live (10 minutes)", you=True)
    products = live_products(state)
    step("Products on sale", bool(products), f"{len(products)} with a live checkout link" if products
         else "no live checkout link yet", "it follows within a cycle of real payments; else automonetize doctor")
    site = bool(cfg.github_pages_repo and cfg.pages_base_url)
    step("A place to find them", site, f"public site {cfg.pages_base_url}" if site
         else "no public site: only people you send a checkout link can buy",
         "automonetize connect-marketing (free GitHub Pages site; 5 minutes)", you=True)
    from strategies.marketing_engine import ensure as ensure_plays

    ensure_plays(state)
    posted = int(state._one("SELECT COUNT(*) AS n FROM marketing_plays WHERE status = 'posted' AND done_at >= ?", (since,))["n"])
    articles = bool(cfg.devto_api_key)
    ways = []
    if articles:
        ways.append("Dev.to articles")
    if posted:
        ways.append(f"{posted} post(s) you published")
    step("Ways in (traffic)", bool(ways), ", ".join(ways) + f" in the last {WINDOW_DAYS} days" if ways
         else "nothing points people at the products yet",
         "post 2-3 of the ready-made posts where your buyers are (Reddit, LinkedIn, Hacker News, Indie Hackers): "
         "control panel → Marketing / Share, then mark them posted. Add a Dev.to key for automatic articles.", you=True)
    started = int(state._one("SELECT COUNT(*) AS n FROM checkout_sessions WHERE updated_at >= ?", (since,))["n"])
    views = int((state._one("SELECT COALESCE(SUM(count), 0) AS n FROM copy_events WHERE event = 'view' AND slot = 'headline' "
                            "AND day >= ?", (since[:10],)) or {}).get("n") or 0)
    seen = f"{views} page view(s) counted, " if views else ""
    step("Visitors start a checkout", started > 0, f"{seen}{started} checkout(s) started in {WINDOW_DAYS} days",
         "more visitors: keep posting (one channel at a time, every few days). Page views are counted once the "
         "tunnel is up.")
    sold = state.revenue_between(state.clock() - timedelta(days=WINDOW_DAYS), state.clock() + timedelta(seconds=1))
    step("Checkouts become sales", sold["n"] > 0, f"{sold['n']} sale(s), ${sold['net'] / 100:,.2f} net in {WINDOW_DAYS} days",
         "buyers start but don't pay: the Products tab shows those products; try a lower price "
         "(automonetize product <slug> price 900) or a clearer description")
    per_day = round(sold["net"] / WINDOW_DAYS)
    goal = int(cfg.daily_target_cents)
    step("On goal", goal > 0 and per_day >= goal, f"${per_day / 100:,.2f}/day average vs ${goal / 100:,.2f} goal",
         "more of what sells: the Products tab ranks products by revenue; the factory leans toward the best types "
         "(automonetize factory --plan)")
    blocker = next((s for s in steps if not s["ok"]), None)
    return {"steps": steps, "blocker": blocker, "math": arithmetic(state, cfg), "net_per_day_cents": per_day,
            "window_days": WINDOW_DAYS}


def describe(d: dict[str, Any]) -> list[str]:
    """Plain lines for the terminal and the report."""
    m = d["math"]
    lines = []
    for s in d["steps"]:
        mark = "✔" if s["ok"] else "✘"
        lines.append(f"{mark} {s['name']}: {s['detail']}")
        if s is d["blocker"]:
            lines.append(f"    NEXT{' (only you can do this)' if s['you'] else ''}: {s['fix']}")
            break  # the rest waits on this one
    lines.append(f"The goal: ${m['goal_cents'] / 100:,.2f}/day is about {m['sales_per_day']} sale(s) a day at "
                 f"${m['avg_price_cents'] / 100:,.2f} (${m['net_per_sale_cents'] / 100:,.2f} after Stripe's fee), which "
                 f"usually takes {m['visitors_low']:,}-{m['visitors_high']:,} visitors a day (estimate: 1-2% of visitors buy).")
    return lines


def headline(d: dict[str, Any]) -> str:
    b = d["blocker"]
    if b is None:
        return "On goal. Raise it with: automonetize goal <dollars>"
    return f"Next step to ${d['math']['goal_cents'] / 100:,.0f}/day: {b['name']} ({b['detail']}). {b['fix']}"
