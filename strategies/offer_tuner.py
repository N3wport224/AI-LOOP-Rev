"""Offer tuner: each offer's discount follows what actually sells.

Every offer email is logged (``tools/contact_policy.py``) and every sale it brings carries its
channel in ``client_reference_id``. Once a week ``tune_offers`` compares the two per offer type,
counting only since the discount last changed:

* at least ``MIN_SENDS`` (30) emails and **no** sale → the discount goes up 10 points;
* a conversion rate of 15% or more → it comes down 5 points (people would buy for less);
* otherwise it stays.

Each offer has bounds (below), so the tuner can't give the store away or make offers pointless.
Changes are logged; ``automonetize offers`` shows the table. Off with ``offer_tuning = false``
(the configured discounts are then used as is). State: kv ``offer_tuning``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from strategies.base import Strategy, TaskContext, TaskResult

KEY = "offer_tuning"
MIN_SENDS = 30
HIGH = 0.15
UP, DOWN = 10, 5
EVERY_DAYS = 7
# kind: (config attribute, order/subscriber channel, campaign or None, min %, max %)
OFFERS: dict[str, tuple[str, str, str | None, int, int]] = {
    "refresh": ("refresh_discount_pct", "refresh", None, 20, 60),
    "winback": ("winback_discount_pct", "winback", None, 20, 60),
    "sample": ("sample_offer_pct", "leadmagnet", "sample_offer", 10, 40),
    "sale": ("sale_pct", "sale", None, 15, 40),
}


def offer_pct(state: Any, cfg: Any, kind: str) -> int:
    attr = OFFERS[kind][0]
    if not getattr(cfg, "offer_tuning", False):
        return int(getattr(cfg, attr))
    return int(((state.get(KEY) or {}).get(kind) or {}).get("pct", getattr(cfg, attr)))


def conversions(state: Any, kind: str, since: str) -> int:
    _, channel, campaign, _, _ = OFFERS[kind]
    where, args = "channel = ? AND occurred_at > ? AND status NOT IN ('refunded', 'disputed')", [channel, since]
    if campaign:
        where += " AND campaign = ?"
        args.append(campaign)
    n = int(state._one(f"SELECT COUNT(*) AS n FROM orders WHERE {where}", tuple(args))["n"])
    sub_where = "tier = 'paid' AND channel = ? AND started_at > ?" + (" AND campaign = ?" if campaign else "")
    return n + int(state._one(f"SELECT COUNT(*) AS n FROM subscribers WHERE {sub_where}", tuple(args))["n"])


def report(state: Any, cfg: Any) -> list[dict[str, Any]]:
    from tools import contact_policy as contact

    tuning = state.get(KEY) or {}
    rows = []
    for kind in OFFERS:
        since = (tuning.get(kind) or {}).get("since") or (state.clock() - timedelta(days=365)).isoformat(timespec="seconds")
        sent = contact.sends(state, kind, since)
        sold = conversions(state, kind, since)
        rows.append({"kind": kind, "pct": offer_pct(state, cfg, kind), "sent": sent, "sold": sold,
                     "rate": round(sold / sent, 4) if sent else None, "since": since})
    return rows


class OfferTuner(Strategy):
    name = "offer_tuner"
    tasks = ("tune_offers",)

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        cfg, state = ctx.tools.config, ctx.tools.state
        if not cfg.offer_tuning:
            return TaskResult(True, "offer tuning off", {})
        tuning: dict[str, Any] = dict(state.get(KEY) or {})
        last = tuning.get("_at")
        if last and state.clock() - datetime.fromisoformat(last) < timedelta(days=EVERY_DAYS):
            return TaskResult(True, f"offers tuned {last[:10]}", {})
        changes = []
        for row in report(state, cfg):
            kind = row["kind"]
            _, _, _, lo, hi = OFFERS[kind]
            pct = row["pct"]
            new = pct
            if row["sent"] >= MIN_SENDS and row["sold"] == 0:
                new = min(hi, pct + UP)
            elif row["sent"] >= MIN_SENDS and row["rate"] is not None and row["rate"] >= HIGH:
                new = max(lo, pct - DOWN)
            if new != pct:
                tuning[kind] = {"pct": new, "since": state.now(), "previous": pct,
                                "why": f"{row['sold']} sale(s) from {row['sent']} email(s)"}
                changes.append(f"{kind} {pct}%→{new}% ({tuning[kind]['why']})")
            elif kind not in tuning:
                tuning[kind] = {"pct": pct, "since": row["since"]}
        tuning["_at"] = state.now()
        state.set(KEY, tuning)
        if changes:
            state.log_action(int(state.get("iteration", 0)), None, "offer_tuner", "ok", "; ".join(changes))
        return TaskResult(True, "offers: " + ("; ".join(changes) or "no change"), {"changes": len(changes)})
