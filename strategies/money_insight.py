"""Money insight (Phases 125-129): where the month is heading and what's leaking.

* **Forecast** (Phase 125): this month's verified net so far, plus the last 14 days' daily average
  times the days left, against the month's goal (``daily_target_cents`` x days). In the daily
  report and ``automonetize forecast``.
* **Price history** (Phase 126): every price change of a product (by the pricing strategy or by
  hand) is recorded with the old and new price (kv ``price_history``, last 500).
  ``automonetize price-history``.
* **Refund-rate guard** (Phase 127): more than ``refund_alert_rate`` (10%) of the last 30 days'
  orders refunded or disputed (with at least 5 orders) puts "Look into refunds" on the to-do list:
  a refund trend usually means a dataset has a problem.
* **Abandoned checkouts** (Phase 128): checkouts started but not paid in the last 7 days, and what
  they were worth, in Monday's report.
* **Niche trends** (Phase 129): each niche's net revenue in the last 14 days against the 14 before,
  in Monday's report, so a fading niche is visible early.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import timedelta, timezone
from typing import Any

HISTORY_KEY = "price_history"
HISTORY_MAX = 500
REFUND_MIN_ORDERS = 5


def money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


# ------------------------------------------------------------------ Phase 125: forecast
def forecast(state: Any, cfg: Any) -> dict[str, Any]:
    now = state.clock().astimezone(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    mtd = int(state.revenue_between(start, now, True)["net"])
    recent = int(state.revenue_between(now - timedelta(days=14), now, True)["net"])
    per_day = recent / 14
    days_left = days_in_month - now.day + (1 - (now.hour * 3600 + now.minute * 60) / 86400)
    projected = round(mtd + per_day * days_left)
    goal = int(cfg.daily_target_cents) * days_in_month
    return {"month": now.strftime("%B"), "mtd_cents": mtd, "per_day_cents": round(per_day), "projected_cents": projected,
            "goal_cents": goal, "on_track": projected >= goal}


def describe_forecast(f: dict[str, Any]) -> str:
    verdict = "on track" if f["on_track"] else f"{money(f['goal_cents'] - f['projected_cents'])} short"
    return (f"{f['month']} so far {money(f['mtd_cents'])}; at the last 14 days' pace ({money(f['per_day_cents'])}/day) "
            f"it ends near {money(f['projected_cents'])} against a {money(f['goal_cents'])} goal: {verdict}.")


# ------------------------------------------------------------------ Phase 126: price history
def record_price_change(state: Any, asset_id: int, old: int | None, new: int) -> None:
    if old is None or int(old) == int(new):
        return
    asset = state._one("SELECT title FROM assets WHERE id = ?", (asset_id,)) or {}
    history = (state.get(HISTORY_KEY) or [])[-(HISTORY_MAX - 1):]
    history.append({"at": state.now(), "asset_id": int(asset_id), "title": asset.get("title", ""), "old_cents": int(old),
                    "new_cents": int(new)})
    state.set(HISTORY_KEY, history)


def price_history(state: Any, limit: int = 50) -> list[dict[str, Any]]:
    return list(reversed((state.get(HISTORY_KEY) or [])[-limit:]))


# ------------------------------------------------------------------ Phase 127: refunds
def refund_rate(state: Any, days: int = 30) -> dict[str, Any]:
    since = (state.clock() - timedelta(days=days)).isoformat(timespec="seconds")
    row = state._one("SELECT COUNT(*) AS n, SUM(CASE WHEN status IN ('refunded', 'disputed') THEN 1 ELSE 0 END) AS bad "
                     "FROM orders WHERE occurred_at >= ?", (since,))
    n, bad = int(row["n"] or 0), int(row["bad"] or 0)
    return {"orders": n, "refunded": bad, "rate": round(bad / n, 3) if n else 0.0}


def refund_problem(state: Any, cfg: Any) -> dict[str, Any] | None:
    r = refund_rate(state)
    if r["orders"] >= REFUND_MIN_ORDERS and r["rate"] > float(cfg.refund_alert_rate):
        return r
    return None


# ------------------------------------------------------------------ Phase 128: abandoned checkouts
def abandoned(state: Any, days: int = 7) -> dict[str, int]:
    since = (state.clock() - timedelta(days=days)).isoformat(timespec="seconds")
    stale = (state.clock() - timedelta(hours=24)).isoformat(timespec="seconds")
    row = state._one("SELECT COUNT(*) AS n, COALESCE(SUM(amount_cents), 0) AS cents FROM checkout_sessions "
                     "WHERE updated_at >= ? AND (status = 'expired' OR (status = 'open' AND updated_at < ?))", (since, stale))
    return {"checkouts": int(row["n"] or 0), "cents": int(row["cents"] or 0)}


def describe_abandoned(a: dict[str, int], days: int = 7) -> str:
    if not a["checkouts"]:
        return ""
    return (f"Abandoned checkouts (last {days} days): {a['checkouts']}, worth {money(a['cents'])}. "
            "Buyers who left at the payment step: the free sample and follow-ups bring some back.")


# ------------------------------------------------------------------ Phase 129: niche trends
def niche_trends(state: Any) -> list[dict[str, Any]]:
    now = state.clock()
    rows = state.revenue_with_orders(now - timedelta(days=28), now)
    niches: dict[int, str] = {}
    cut = (now - timedelta(days=14)).isoformat(timespec="seconds")
    totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        aid = r.get("asset_id")
        if aid is None:
            continue
        if aid not in niches:
            niches[aid] = ((state._one("SELECT niche FROM assets WHERE id = ?", (aid,)) or {}).get("niche") or "other")
        totals[niches[aid]][0 if str(r["occurred_at"]) >= cut else 1] += int(r["net_cents"])
    out = []
    for niche, (recent, before) in sorted(totals.items(), key=lambda kv: -kv[1][0]):
        change = None if not before else round((recent - before) * 100 / before)
        out.append({"niche": niche, "recent_cents": recent, "before_cents": before, "change_pct": change})
    return out


def describe_trends(trends: list[dict[str, Any]]) -> str:
    if not trends:
        return ""
    parts = []
    for t in trends[:6]:
        arrow = "new" if t["change_pct"] is None else (f"+{t['change_pct']}%" if t["change_pct"] >= 0 else f"{t['change_pct']}%")
        parts.append(f"{t['niche']} {money(t['recent_cents'])} ({arrow})")
    return "Last 14 days by niche vs the 14 before: " + ", ".join(parts) + "."
