"""The catalog plan (Phases 325-329): what the factory's products earn, and how many it takes.

Goal pacing (``automonetize pace``) says how far the last week is from your daily target and the
next step. This looks at the catalog itself:

* **Phase 325, what a product earns:** net revenue per product per day over the last 30 days, from
  factory products at least ``MIN_AGE_DAYS`` (7) old (newer ones haven't had their chance yet).
* **Phase 326, where it's heading:** at the factory's pace over the last 7 days (products made minus
  retired), the catalog and its daily revenue in 30 and 90 days, capped at ``factory_max_live``.
* **Phase 327, what it takes:** how many products the daily target (``daily_target_cents``) needs at
  today's earnings per product, and how many days that is at the current pace. Says so plainly when
  the cap or a zero pace makes it unreachable, and points at the other lever (earning more per
  product: traffic, pricing).
* **Phase 328, where to focus:** product types that earn at least ``FOCUS_RATIO`` times the average per
  product (with at least ``MIN_TYPE_PRODUCTS`` products) are named, and so are types that sold
  nothing from ``MIN_TYPE_PRODUCTS`` products over 30 days, with the ``factory_types`` line to apply it.
  Advice only: nothing changes unless you change the setting.
* **Phase 329, where you see it:** ``automonetize factory --plan``, Monday's report (one line) and the
  control panel's Products tab (``plan`` in ``GET /api/products``).
"""

from __future__ import annotations

import math
from datetime import timedelta
from typing import Any

MIN_AGE_DAYS = 7
DAYS = 30
FOCUS_RATIO = 2.0
MIN_TYPE_PRODUCTS = 5


# ------------------------------------------------------------------ Phase 325
def per_product_day(state: Any) -> dict[str, Any]:
    from strategies.product_factory import ensure

    ensure(state)
    now = state.clock()
    old = (now - timedelta(days=MIN_AGE_DAYS)).isoformat(timespec="seconds")
    since = (now - timedelta(days=DAYS)).isoformat(timespec="seconds")
    live = state._all("SELECT slug, published_at FROM factory_products WHERE status = 'live' AND published_at <= ?", (old,))
    if not live:
        return {"products": 0, "cents_per_product_day": 0.0, "net_cents": 0}
    slugs = [r["slug"] for r in live]
    marks = ",".join("?" for _ in slugs)
    row = state._one(f"SELECT COALESCE(SUM(o.gross_cents), 0) AS gross FROM orders o JOIN assets a ON a.id = o.asset_id "
                     f"WHERE a.niche IN ({marks}) AND o.occurred_at >= ? AND o.status NOT IN ('refunded', 'disputed')",
                     (*slugs, since))
    product_days = sum(min(DAYS, max(1, (now - _dt(r["published_at"])).days)) for r in live)
    gross = int(row["gross"])
    return {"products": len(live), "cents_per_product_day": round(gross / product_days, 2), "net_cents": gross}


def _dt(iso: str) -> Any:
    from datetime import datetime

    return datetime.fromisoformat(iso)


# ------------------------------------------------------------------ Phase 326
def pace(state: Any) -> float:
    since = (state.clock() - timedelta(days=7)).isoformat(timespec="seconds")
    made = int(state._one("SELECT COUNT(*) AS n FROM factory_products WHERE published_at >= ?", (since,))["n"])
    retired = int(state._one("SELECT COUNT(*) AS n FROM factory_products WHERE retired_at >= ?", (since,))["n"])
    return (made - retired) / 7


def project(state: Any, cfg: Any) -> dict[str, Any]:
    from strategies.product_factory import catalog

    live = catalog(state).get("live", 0)
    earn = per_product_day(state)["cents_per_product_day"]
    per_day = pace(state)
    cap = int(cfg.factory_max_live)
    out = {"live": live, "pace_per_day": round(per_day, 2), "cents_per_product_day": earn}
    for days in (30, 90):
        n = max(0, min(cap, round(live + per_day * days)))
        out[f"products_{days}"] = n
        out[f"cents_per_day_{days}"] = round(n * earn)
    return out


# ------------------------------------------------------------------ Phase 327
def what_it_takes(state: Any, cfg: Any) -> dict[str, Any]:
    p = project(state, cfg)
    goal = int(getattr(cfg, "daily_target_cents", 0) or 0)
    earn = p["cents_per_product_day"]
    if not goal:
        return {**p, "goal_cents": 0, "needed": None, "days": None, "note": "No daily target set."}
    if earn <= 0:
        return {**p, "goal_cents": goal, "needed": None, "days": None,
                "note": "No factory product has sold in the last 30 days yet, so there's nothing to scale from: "
                        "traffic comes first (automonetize pace shows the next step)."}
    needed = math.ceil(goal / earn)
    if needed > int(cfg.factory_max_live):
        return {**p, "goal_cents": goal, "needed": needed, "days": None,
                "note": f"The target needs about {needed:,} products at today's earnings, more than factory_max_live "
                        f"({cfg.factory_max_live}). Raise the cap, or earn more per product (traffic, pricing)."}
    if needed <= p["live"]:
        return {**p, "goal_cents": goal, "needed": needed, "days": 0,
                "note": "The catalog is big enough for the target at today's earnings per product."}
    if p["pace_per_day"] <= 0:
        return {**p, "goal_cents": goal, "needed": needed, "days": None,
                "note": f"The target needs about {needed:,} products, but the catalog isn't growing this week."}
    days = math.ceil((needed - p["live"]) / pace(state))  # the exact pace, not the rounded one shown
    return {**p, "goal_cents": goal, "needed": needed, "days": days,
            "note": f"About {needed:,} products reach the target at today's earnings: {days} days at the current pace."}


# ------------------------------------------------------------------ Phase 328
def focus(state: Any) -> dict[str, Any]:
    from strategies.catalog_insight import by_type, rows
    from strategies.product_types import TYPES

    types = [t for t in by_type(state) if t["live"] + t["retired"] >= MIN_TYPE_PRODUCTS]
    if not types:
        return {"strong": [], "weak": [], "setting": ""}
    avg = sum(t["revenue_cents"] for t in types) / max(1, sum(t["live"] + t["retired"] for t in types))
    strong = [t["type"] for t in types if avg > 0 and t["per_product_cents"] >= FOCUS_RATIO * avg]
    cutoff = (state.clock() - timedelta(days=DAYS)).isoformat(timespec="seconds")
    aged = {}
    for r in rows(state):
        if r["published_at"] and r["published_at"] <= cutoff:
            aged.setdefault(r["type"], []).append(r)
    weak = [t for t, items in aged.items() if len(items) >= MIN_TYPE_PRODUCTS and not any(i["orders_90d"] for i in items)]
    keep = [t for t in TYPES if t not in weak]
    return {"strong": strong, "weak": weak, "setting": f"factory_types = {keep!r}" if weak else ""}


# ------------------------------------------------------------------ Phase 329
def plan(state: Any, cfg: Any) -> dict[str, Any]:
    return {**what_it_takes(state, cfg), "focus": focus(state)}


def describe(p: dict[str, Any]) -> list[str]:
    lines = [f"Catalog: {p['live']} on sale, {p['pace_per_day']:+.1f} a day this week; a product earns "
             f"${p['cents_per_product_day'] / 100:.2f} a day.",
             f"In 30 days: ~{p['products_30']} products, ~${p['cents_per_day_30'] / 100:,.2f}/day; in 90 days: "
             f"~{p['products_90']} products, ~${p['cents_per_day_90'] / 100:,.2f}/day.", p["note"]]
    f = p["focus"]
    if f["strong"]:
        lines.append("Earning most per product: " + ", ".join(f["strong"]) + ".")
    if f["weak"]:
        lines.append("No sales in 30 days from: " + ", ".join(f["weak"]) + f". To stop making them: {f['setting']}")
    return lines


def weekly_line(state: Any, cfg: Any) -> str:
    p = what_it_takes(state, cfg)
    if not p["live"]:
        return ""
    return f"Catalog plan: {p['note']}"
