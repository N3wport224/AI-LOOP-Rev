"""Customers (Phases 67-68): one row per buyer, and what they're worth over time.

* ``customers(state)``: everyone who paid (one-off orders and subscription invoices), with first and
  last purchase, number of purchases, total spent (refunded and disputed orders excluded), what they
  bought, the channel that brought them, whether they're subscribed or suppressed, and when they
  last got an email from the agent.
* ``export_csv``: that list as ``data/exports/customers.csv`` (``automonetize customers``).
* ``report(state)``: repeat-purchase rate, average lifetime value, lifetime value by first channel
  and by first-purchase month (``automonetize customers --report``, and a line in Monday's report).
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from typing import Any

FIELDS = ("email", "first_purchase", "last_purchase", "purchases", "spent_usd", "products", "first_channel", "subscribed",
          "suppressed", "last_email")


def customers(state: Any) -> list[dict[str, Any]]:
    from tools import contact_policy as contact

    contact.ensure(state)
    rows = state._all("SELECT * FROM orders WHERE email IS NOT NULL AND email NOT LIKE 'deleted-%' "
                      "AND status NOT IN ('refunded', 'disputed') ORDER BY occurred_at, id")
    people: dict[str, dict[str, Any]] = {}
    titles: dict[int, str] = {}
    for o in rows:
        email = o["email"].lower()
        p = people.setdefault(email, {"email": email, "first_purchase": o["occurred_at"], "last_purchase": o["occurred_at"],
                                      "purchases": 0, "spent_cents": 0, "products": [], "first_channel": o.get("channel") or "direct"})
        p["last_purchase"] = o["occurred_at"]
        p["purchases"] += 1
        p["spent_cents"] += int(o["gross_cents"] or 0)
        if o.get("asset_id"):
            if o["asset_id"] not in titles:
                titles[o["asset_id"]] = (state.get_asset(o["asset_id"]) or {}).get("title", "")
            if titles[o["asset_id"]] and titles[o["asset_id"]] not in p["products"]:
                p["products"].append(titles[o["asset_id"]])
    active = {(r["email"] or "").lower() for r in state._all(
        "SELECT email FROM subscribers WHERE tier = 'paid' AND subscription_status IN ('active', 'trialing', 'past_due')")}
    for email, p in people.items():
        p["subscribed"] = email in active
        p["suppressed"] = state.is_suppressed(email)
        last = state._one("SELECT MAX(sent_at) AS t FROM contact_log WHERE email = ? AND kind != 'bounce'", (email,))
        p["last_email"] = (last or {}).get("t") or ""
    return sorted(people.values(), key=lambda p: -p["spent_cents"])


def export_csv(state: Any, files: Any) -> tuple[Any, int]:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(FIELDS)
    people = customers(state)
    for p in people:
        w.writerow([p["email"], p["first_purchase"][:10], p["last_purchase"][:10], p["purchases"], f"{p['spent_cents'] / 100:.2f}",
                    "; ".join(p["products"]), p["first_channel"], "yes" if p["subscribed"] else "no",
                    "yes" if p["suppressed"] else "no", p["last_email"][:10]])
    path = files.write_text("exports/customers.csv", buf.getvalue())
    return path, len(people)


def report(state: Any) -> dict[str, Any]:
    people = customers(state)
    n = len(people)
    by_channel: dict[str, list[int]] = defaultdict(list)
    by_month: dict[str, list[int]] = defaultdict(list)
    for p in people:
        by_channel[p["first_channel"]].append(p["spent_cents"])
        by_month[p["first_purchase"][:7]].append(p["spent_cents"])

    def summary(groups: dict[str, list[int]]) -> dict[str, dict[str, int]]:
        return {k: {"customers": len(v), "ltv_cents": round(sum(v) / len(v))} for k, v in sorted(groups.items())}

    return {
        "customers": n,
        "repeat_rate": round(sum(1 for p in people if p["purchases"] > 1) / n, 4) if n else None,
        "avg_ltv_cents": round(sum(p["spent_cents"] for p in people) / n) if n else 0,
        "by_channel": summary(by_channel),
        "by_month": summary(by_month),
    }


def describe(r: dict[str, Any]) -> str:
    if not r["customers"]:
        return "Customers: none yet."
    best = max(r["by_channel"].items(), key=lambda kv: kv[1]["ltv_cents"])[0] if r["by_channel"] else ""
    return (f"Customers: {r['customers']}, {r['repeat_rate'] * 100:.0f}% bought more than once, average lifetime value "
            f"${r['avg_ltv_cents'] / 100:,.2f}" + (f"; most valuable channel: {best}" if best else ""))
