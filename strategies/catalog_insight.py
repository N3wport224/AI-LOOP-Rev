"""Catalog insight (Phases 235-239): what the factory is making, and what of it sells.

* **Phase 235, by product type:** ``automonetize factory --types`` shows, for each product type,
  products on sale and retired, orders and revenue in the last 90 days, and revenue per product on
  sale: which kinds of product are worth making. ``--csv`` writes the whole catalog to
  ``data/exports/catalog.csv``.
* **Phase 236, by email:** ``AM CATALOG <code>`` replies with the same summary, the five best
  sellers and what the factory makes next.
* **Phase 237, catalog spreadsheet:** the control panel's Products tab downloads the catalog as CSV
  (``GET /api/products.csv``).
* **Phase 238, weekly line:** Monday's report says how many products were made, retired and
  repriced in the past week and which type earns most per product.
* **Phase 239, choose the product types:** ``factory_types`` lists the types the factory makes
  (empty, the default: every kind in ``product_types.TYPES``). Leave one out to stop making it; what's on sale stays
  on sale and keeps refreshing.
"""

from __future__ import annotations

import csv
import io
import json
from collections import defaultdict
from datetime import timedelta
from typing import Any

TYPE_NAMES = {"slice": "Postings by technology", "salary": "Salary benchmarks", "top": "Top companies",
              "remote_first": "Remote-first employers", "fast_hiring": "Fastest-hiring companies", "pack": "Starter packs"}
CSV_FIELDS = ["slug", "title", "type", "technology", "status", "rows", "price_cents", "orders_90d", "revenue_cents_90d",
              "published_at", "retired_at", "checkout_url"]


def _orders(state: Any, days: int = 90) -> dict[str, tuple[int, int]]:
    since = (state.clock() - timedelta(days=days)).isoformat(timespec="seconds")
    rows = state._all("SELECT a.niche AS niche, COUNT(*) AS n, COALESCE(SUM(o.gross_cents), 0) AS gross FROM orders o "
                      "JOIN assets a ON a.id = o.asset_id WHERE o.occurred_at >= ? AND o.status NOT IN ('refunded', 'disputed') "
                      "AND a.niche IS NOT NULL GROUP BY a.niche", (since,))
    return {r["niche"]: (int(r["n"]), int(r["gross"])) for r in rows}


def rows(state: Any) -> list[dict[str, Any]]:
    from strategies.product_factory import ensure

    ensure(state)
    sales = _orders(state)
    out = []
    for r in state._all("SELECT f.*, a.price_cents AS price, a.checkout_url AS url FROM factory_products f "
                        "LEFT JOIN assets a ON a.id = f.asset_id ORDER BY f.created_at"):
        f = json.loads(r["filters"])
        n, gross = sales.get(r["slug"], (0, 0))
        out.append({"slug": r["slug"], "title": r["title"], "type": f.get("type", "slice"), "technology": f.get("tech") or "",
                    "status": r["status"], "rows": r["rows"], "price_cents": int(r["price"] or 0), "orders_90d": n,
                    "revenue_cents_90d": gross, "published_at": r["published_at"] or "", "retired_at": r["retired_at"] or "",
                    "checkout_url": r["url"] or ""})
    return out


# ------------------------------------------------------------------ Phase 235
def by_type(state: Any) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, int]] = defaultdict(lambda: {"live": 0, "retired": 0, "orders": 0, "revenue_cents": 0})
    for r in rows(state):
        g = groups[r["type"]]
        if r["status"] in ("live", "retired"):
            g[r["status"]] += 1
        g["orders"] += r["orders_90d"]
        g["revenue_cents"] += r["revenue_cents_90d"]
    from strategies.product_kinds import KINDS

    out = [{"type": t, "name": TYPE_NAMES.get(t) or (KINDS[t].display if t in KINDS else t), **g, "per_product_cents": g["revenue_cents"] // max(1, g["live"] + g["retired"])}
           for t, g in groups.items()]
    return sorted(out, key=lambda r: (-r["per_product_cents"], -r["live"], r["type"]))


def describe_types(state: Any) -> list[str]:
    return [f"{r['name']}: {r['live']} on sale, {r['retired']} retired; {r['orders']} order(s), "
            f"${r['revenue_cents'] / 100:,.2f} in 90 days (${r['per_product_cents'] / 100:,.2f} per product)"
            for r in by_type(state)]


def catalog_csv(state: Any) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_FIELDS)
    w.writeheader()
    for r in rows(state):
        w.writerow(r)
    return buf.getvalue()


# ------------------------------------------------------------------ Phase 236
def email_summary(state: Any, cfg: Any) -> str:
    from strategies.product_factory import catalog, next_candidate
    from strategies.sales_channels import leaderboard

    counts = catalog(state)
    lines = [f"Catalog: {counts.get('live', 0)} on sale, {counts.get('staged', 0)} waiting, {counts.get('retired', 0)} retired.",
             "", "By type (last 90 days):", *[f"- {line}" for line in describe_types(state)]]
    best = [r for r in leaderboard(state) if r["orders"]][:5]
    if best:
        lines += ["", "Best sellers:", *[f"- {r['title']}: {r['orders']} order(s), ${r['revenue_cents'] / 100:,.2f}" for r in best]]
    nxt = next_candidate(state, cfg, peek=True) if cfg.product_factory else None
    lines += ["", f"Next: {nxt['title']}" if nxt else "Next: waiting for more postings."]
    return "\n".join(lines)


# ------------------------------------------------------------------ Phase 238
def weekly_line(state: Any) -> str:
    from strategies.money_insight import price_history
    from strategies.product_factory import ensure

    ensure(state)
    since = (state.clock() - timedelta(days=7)).isoformat(timespec="seconds")
    made = int(state._one("SELECT COUNT(*) AS n FROM factory_products WHERE published_at >= ?", (since,))["n"])
    retired = int(state._one("SELECT COUNT(*) AS n FROM factory_products WHERE retired_at >= ?", (since,))["n"])
    repriced = sum(1 for p in price_history(state, 500) if p["at"] >= since)
    if not (made or retired or repriced):
        return ""
    best = next((r for r in by_type(state) if r["revenue_cents"]), None)
    return (f"Catalog this week: {made} new, {retired} retired, {repriced} price change(s)."
            + (f" Earning most per product: {best['name']} (${best['per_product_cents'] / 100:,.2f})." if best else ""))


# ------------------------------------------------------------------ Phase 239
def enabled_types(cfg: Any) -> list[str]:
    from strategies.product_types import TYPES

    wanted = [str(t).strip().lower() for t in (getattr(cfg, "factory_types", None) or TYPES)]
    return [t for t in TYPES if t in wanted]
