"""Refunds and disputes: keep revenue honest and tell the owner.

``sync_refunds`` polls Stripe (read-only) for refunds and disputes since the last check:

* **Refund** → a negative, verified revenue row (``refund:<id>``) so today's total, the $10/day goal,
  analytics and reports are net of it; the matching order becomes ``refunded`` (which also drops it
  from the sales counts the pricing and pivot rules use).
* **Dispute (chargeback)** → Stripe takes the amount plus its dispute fee at once, so a negative row
  (``dispute:<id>``) records both; the order becomes ``disputed``; the owner gets an alert with the
  amount, the reason and the evidence deadline (answered in the Stripe dashboard). If the dispute
  is later **won**, the amount comes back (``dispute_won:<id>``).

Every row is keyed by Stripe's id, so a refund or dispute is counted exactly once however often the
task runs. Nothing is refunded or contested automatically: those are your decisions.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from strategies.base import Strategy, TaskContext, TaskResult

STRIPE_API = "https://api.stripe.com/v1"
DISPUTE_FEE_CENTS = 1500  # Stripe's US dispute fee
FIRST_LOOKBACK_DAYS = 30


def _iso(ts: Any) -> str:
    return datetime.fromtimestamp(int(ts or 0), tz=timezone.utc).isoformat(timespec="seconds")


class Refunds(Strategy):
    name = "refunds"
    tasks = ("sync_refunds",)

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        tools, cfg, state = ctx.tools, ctx.tools.config, ctx.tools.state
        if not cfg.stripe_secret_key:
            return TaskResult(True, "no Stripe key", {})
        now = state.clock()
        last = state.get("refunds_synced_at")
        since = (datetime.fromisoformat(last) - timedelta(days=1)) if last else now - timedelta(days=FIRST_LOOKBACK_DAYS)
        headers = {"Authorization": f"Bearer {cfg.stripe_secret_key}"}

        def get(path: str, **params: Any) -> dict[str, Any]:
            return tools.http.get(f"{STRIPE_API}/{path}", params=params, headers=headers, check_robots=False, attempts=2).json()

        gte = int(since.timestamp())
        try:
            refunds = get("refunds", **{"limit": 100, "created[gte]": gte}).get("data") or []
            disputes = get("disputes", **{"limit": 100, "created[gte]": gte}).get("data") or []
        except Exception as exc:  # noqa: BLE001 - e.g. a restricted key without refund access: retried next cycle
            state.log_error("refunds", f"couldn't read refunds/disputes from Stripe: {exc!r}")
            return TaskResult(True, f"couldn't read refunds: {exc!r}"[:200], {"refunds": 0, "disputes": 0})
        new_refunds = sum(self.refund(tools, r, get) for r in refunds if r.get("status") in ("succeeded", "pending"))
        new_disputes = sum(self.dispute(tools, d, get) for d in disputes)
        state.set("refunds_synced_at", now.isoformat(timespec="seconds"))
        return TaskResult(True, f"refunds: {new_refunds} new; disputes: {new_disputes} new or changed",
                          {"refunds": new_refunds, "disputes": new_disputes})

    @staticmethod
    def order_for(tools: Any, payment_intent: str | None, get: Any) -> dict[str, Any] | None:
        if not payment_intent:
            return None
        local = tools.state.session_for_payment_intent(payment_intent)
        if local:
            order = tools.state.get_order("stripe", local["session_id"])
            if order:
                return order
        sessions = get("checkout/sessions", payment_intent=payment_intent, limit=1).get("data") or []
        return tools.state.get_order("stripe", sessions[0]["id"]) if sessions else None

    def refund(self, tools: Any, r: dict[str, Any], get: Any) -> bool:
        state = tools.state
        if state._one("SELECT 1 AS x FROM revenue WHERE source = 'stripe' AND external_id = ?", (f"refund:{r['id']}",)):
            return False
        order = self.order_for(tools, r.get("payment_intent"), get)
        amount = int(r.get("amount") or 0)
        state.record_revenue("stripe", f"refund:{r['id']}", -amount, 0, -amount, True, _iso(r.get("created")),
                             hypothesis_id=(order or {}).get("hypothesis_id"), product_ref=(order or {}).get("product_ref"),
                             note=f"refund of {(order or {}).get('order_id', r.get('payment_intent') or 'a payment')}")
        if order:
            state.set_order_status(order["id"], "refunded")
        state.log_action(int(state.get("iteration", 0)), None, "refund", "ok", f"${amount / 100:.2f} refunded ({r['id']})")
        return True

    def dispute(self, tools: Any, d: dict[str, Any], get: Any) -> bool:
        state = tools.state
        amount = int(d.get("amount") or 0)
        exists = state._one("SELECT 1 AS x FROM revenue WHERE source = 'stripe' AND external_id = ?", (f"dispute:{d['id']}",))
        changed = False
        if not exists:
            order = self.order_for(tools, d.get("payment_intent"), get)
            state.record_revenue("stripe", f"dispute:{d['id']}", -amount, DISPUTE_FEE_CENTS, -(amount + DISPUTE_FEE_CENTS), True,
                                 _iso(d.get("created")), hypothesis_id=(order or {}).get("hypothesis_id"),
                                 product_ref=(order or {}).get("product_ref"), note=f"dispute ({d.get('reason', 'unknown')})")
            if order:
                state.set_order_status(order["id"], "disputed")
            due = (d.get("evidence_details") or {}).get("due_by")
            state.log_error("refunds", f"A customer disputed a ${amount / 100:.2f} payment (reason: {d.get('reason', 'unknown')}). "
                                       f"Respond in Stripe → Payments → Disputes{' by ' + _iso(due)[:10] if due else ''}; "
                                       "unanswered disputes are lost.", kind="alert")
            changed = True
        if d.get("status") == "won" and not state._one("SELECT 1 AS x FROM revenue WHERE source = 'stripe' AND external_id = ?",
                                                       (f"dispute_won:{d['id']}",)):
            state.record_revenue("stripe", f"dispute_won:{d['id']}", amount, 0, amount, True, state.now(), note="dispute won")
            state.log_action(int(state.get("iteration", 0)), None, "dispute", "ok", f"won: ${amount / 100:.2f} returned")
            changed = True
        return changed
