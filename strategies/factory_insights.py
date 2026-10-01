"""What the catalog teaches (Phases 335-339): ``automonetize factory --insights`` and the control panel.

* **Phase 335, when products sell:** orders and revenue by the product's age at the time of the sale
  (first week, weeks 2-4, months 2, older), so you can see whether the catalog lives on new products
  or on long sellers.
* **Phase 336, time to first sale:** the median days from going on sale to the first order, and the
  share of products that sold within 14 and 30 days.
* **Phase 337, why products left:** each retirement is recorded with its reason (no sale in time,
  retired by you, download couldn't be rebuilt), and counted.
* **Phase 338, did the price rises work:** for each automatic or manual price change, orders in the
  4 weeks before and after (``price_effects``); a change younger than 4 weeks is shown as "too early".
* **Phase 339, type by technology:** revenue in 90 days for each product type and the eight best
  technologies, a small grid that shows which combinations earn.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from statistics import median
from typing import Any

AGE_BUCKETS = ((7, "first week"), (28, "weeks 2-4"), (60, "month 2"), (10**6, "older"))
REASONS = {"unsold": "no sale in time", "owner": "retired by you", "missing_download": "download couldn't be rebuilt"}


def _dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def _orders(state: Any) -> list[dict[str, Any]]:
    from strategies.product_factory import ensure

    ensure(state)
    return state._all("SELECT o.occurred_at AS at, o.gross_cents AS gross, f.slug AS slug, f.published_at AS published, "
                      "f.filters AS filters FROM orders o JOIN assets a ON a.id = o.asset_id JOIN factory_products f "
                      "ON f.slug = a.niche WHERE o.status NOT IN ('refunded', 'disputed') AND f.published_at IS NOT NULL")


# ------------------------------------------------------------------ Phase 335
def by_age(state: Any) -> list[dict[str, Any]]:
    counts: dict[str, list[int]] = {label: [0, 0] for _, label in AGE_BUCKETS}
    for o in _orders(state):
        age = (_dt(o["at"]) - _dt(o["published"])).days
        label = next(lbl for limit, lbl in AGE_BUCKETS if age < limit)
        counts[label][0] += 1
        counts[label][1] += int(o["gross"])
    return [{"age": label, "orders": n, "revenue_cents": g} for label, (n, g) in counts.items()]


# ------------------------------------------------------------------ Phase 336
def first_sale(state: Any) -> dict[str, Any]:
    firsts: dict[str, float] = {}
    for o in _orders(state):
        days = (_dt(o["at"]) - _dt(o["published"])).total_seconds() / 86400
        firsts[o["slug"]] = min(firsts.get(o["slug"], days), days)
    published = state._all("SELECT slug, published_at FROM factory_products WHERE published_at IS NOT NULL")
    now = state.clock()
    old_enough = {r["slug"] for r in published if (now - _dt(r["published_at"])).days >= 30}

    def share(limit: int) -> int | None:
        if not old_enough:
            return None
        return round(100 * sum(1 for s in old_enough if firsts.get(s, 10**6) <= limit) / len(old_enough))

    return {"median_days": round(median(firsts.values()), 1) if firsts else None, "sold": len(firsts),
            "within_14_pct": share(14), "within_30_pct": share(30)}


# ------------------------------------------------------------------ Phase 337
def ensure_reason(state: Any) -> None:
    cols = {r["name"] for r in state._all("PRAGMA table_info(factory_products)")}
    if "retire_reason" not in cols:
        state._exec("ALTER TABLE factory_products ADD COLUMN retire_reason TEXT")


def set_reason(state: Any, slug: str, reason: str) -> None:
    ensure_reason(state)
    state._exec("UPDATE factory_products SET retire_reason = ? WHERE slug = ?", (reason, slug))


def retirements(state: Any) -> dict[str, int]:
    from strategies.product_factory import ensure

    ensure(state)
    ensure_reason(state)
    rows = state._all("SELECT COALESCE(retire_reason, 'unsold') AS r, COUNT(*) AS n FROM factory_products "
                      "WHERE status = 'retired' GROUP BY r")
    return {REASONS.get(r["r"], r["r"]): int(r["n"]) for r in rows}


# ------------------------------------------------------------------ Phase 338
def price_effects(state: Any, limit: int = 20) -> list[dict[str, Any]]:
    from strategies.money_insight import price_history

    now = state.clock()
    out = []
    for p in price_history(state, 200)[:limit]:
        at = _dt(p["at"])
        a = state.get_asset(int(p["asset_id"])) or {}
        niche = a.get("niche")
        if not niche:
            continue

        def count(start: datetime, end: datetime) -> int:
            return int(state._one("SELECT COUNT(*) AS n FROM orders o JOIN assets x ON x.id = o.asset_id WHERE x.niche = ? "
                                  "AND o.occurred_at >= ? AND o.occurred_at < ? AND o.status NOT IN ('refunded', 'disputed')",
                                  (niche, start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")))["n"])
        early = now - at < timedelta(days=28)
        out.append({"title": p["title"], "at": p["at"][:10], "old_cents": p["old_cents"], "new_cents": p["new_cents"],
                    "before": count(at - timedelta(days=28), at), "after": None if early else count(at, at + timedelta(days=28))})
    return out


# ------------------------------------------------------------------ Phase 339
def grid(state: Any) -> dict[str, Any]:
    since = (state.clock() - timedelta(days=90)).isoformat(timespec="seconds")
    cells: dict[tuple[str, str], int] = defaultdict(int)
    techs: Counter = Counter()
    for o in _orders(state):
        if o["at"] < since:
            continue
        f = json.loads(o["filters"])
        tech = f.get("tech") or "(any)"
        cells[(f.get("type", "slice"), tech)] += int(o["gross"])
        techs[tech] += int(o["gross"])
    top = [t for t, _ in techs.most_common(8)]
    types = sorted({k for k, _ in cells})
    return {"techs": top, "types": types, "cents": {t: {x: cells.get((t, x), 0) for x in top} for t in types}}


def insights(state: Any) -> dict[str, Any]:
    return {"by_age": by_age(state), "first_sale": first_sale(state), "retirements": retirements(state),
            "price_effects": price_effects(state), "grid": grid(state)}


def describe(data: dict[str, Any]) -> list[str]:
    from strategies.product_factory import label

    lines = ["Sales by product age: " + ", ".join(f"{b['age']} {b['orders']}" for b in data["by_age"])]
    fs = data["first_sale"]
    if fs["median_days"] is not None:
        lines.append(f"First sale after {fs['median_days']} days (median of {fs['sold']} products)"
                     + (f"; {fs['within_30_pct']}% sold within 30 days" if fs["within_30_pct"] is not None else "") + ".")
    if data["retirements"]:
        lines.append("Retired: " + ", ".join(f"{n} {why}" for why, n in data["retirements"].items()) + ".")
    for p in data["price_effects"][:5]:
        after = "too early to tell" if p["after"] is None else f"{p['after']} order(s) in the 4 weeks after"
        lines.append(f"Price {p['title']}: ${p['old_cents'] / 100:.0f} → ${p['new_cents'] / 100:.0f} on {p['at']}: "
                     f"{p['before']} order(s) in the 4 weeks before, {after}.")
    g = data["grid"]
    for t in g["types"]:
        cells = [f"{label(x) if x != '(any)' else x} ${g['cents'][t][x] / 100:,.0f}" for x in g["techs"] if g["cents"][t][x]]
        if cells:
            lines.append(f"{t}: " + ", ".join(cells))
    return lines
