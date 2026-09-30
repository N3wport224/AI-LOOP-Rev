"""Customer lifecycle: dunning, grace periods, recovery and churn analytics (Phase 8).

**Payment fails** (``invoice.payment_failed``, or a subscription seen as ``past_due`` by the
webhook or the polling sweep) → a *dunning case* opens (once per subscriber):

* API keys drop to ``api_degraded_quota`` (50/day) at once: degraded, not deleted.
* A polite reminder goes out straight away with a link to Stripe's hosted **billing portal**
  (``billing_portal.Session`` for the customer, so they can update the card in two clicks),
  then again on the days in ``dunning_reminder_days`` (0, 3, 6).
* After ``dunning_grace_days`` (7) unpaid, API keys are **suspended** (402 ``payment_required``),
  still not deleted, and the Monday dataset stops (Stripe's status is no longer active).
* A payment at any point (``invoice.paid``) closes the case as *recovered* and restores the keys.

Stripe has no ``customer.subscription.past_due`` event: it reports past-due as
``customer.subscription.updated`` with ``status: past_due``, which is handled here. (Listing the
made-up name among the endpoint's events would make Stripe reject the registration.)

**Subscription ends** (``customer.subscription.deleted``): the subscriber becomes ``canceled``, API
keys are revoked, any open case closes, and a churn event is logged (product, Stripe's
``cancellation_details.reason``, tenure, MRR lost, whether it followed a failed payment), so
analytics can separate involuntary churn (payments) from voluntary churn.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from agent.state import iso
from strategies.base import Strategy, TaskContext, TaskResult
from tools.dispatcher import Email


def open_case(state: Any, subscriber_id: int) -> dict[str, Any] | None:
    return state._one("SELECT * FROM dunning_cases WHERE subscriber_id = ? AND status IN ('open', 'expired') ORDER BY id DESC LIMIT 1",
                      (subscriber_id,))


def grace_expired(state: Any, subscriber_id: int) -> bool:
    case = open_case(state, subscriber_id)
    return bool(case and case["status"] == "expired")


def portal_link(tools: Any, sub: dict[str, Any]) -> str:
    """A fresh billing-portal URL for the customer (falls back to the lander when Stripe can't make one)."""
    from strategies.subscription_engine import stripe_storefront

    sf = stripe_storefront(tools)
    fallback = tools.config.pages_base_url or ""
    if sf is None or not sub.get("customer_id"):
        return fallback
    try:
        s = sf.client.create_portal_session(sub["customer_id"], tools.config.pages_base_url or tools.config.lead_capture_base or "https://stripe.com",
                                            key=f"portal-{sub['id']}-{tools.state.clock().strftime('%Y%m%d%H')}")
        return s.get("url") or fallback
    except Exception as exc:  # noqa: BLE001 - the portal must be activated once in the Stripe dashboard
        tools.state.log_error("retention", f"billing portal for subscriber {sub['id']} failed: {exc!r}")
        return fallback


def product_of(state: Any, sub: dict[str, Any]) -> str:
    from api.auth import is_api_subscriber

    return "api" if is_api_subscriber(state, sub) else "dataset"


def reminder(tools: Any, sub: dict[str, Any], case: dict[str, Any], number: int) -> str:
    cfg = tools.config
    product = "Developer API" if product_of(tools.state, sub) == "api" else "weekly tech-stack updates"
    link = portal_link(tools, sub)
    grace = datetime.fromisoformat(case["grace_until"]).strftime("%A %d %B")
    first = number == 1
    subject = ("Your payment didn't go through" if first else f"Reminder: please update your card ({product})")
    lines = [
        "Hi,", "",
        (f"Your latest payment for {product} didn't go through. It happens: an expired card, a new card number, or a bank "
         "being cautious." if first else f"Just a reminder that the payment for {product} is still outstanding."),
        "",
        f"Update your payment details here (secure Stripe page, takes a minute):\n{link}" if link else
        "You can update your card from the link in any Stripe receipt email.",
        "",
    ]
    if product_of(tools.state, sub) == "api":
        lines.append(f"Your API key keeps working meanwhile at {cfg.api_degraded_quota} requests a day, until {grace}. "
                     "Full access comes back the moment the payment succeeds.")
    else:
        lines.append(f"Nothing changes until {grace}; after that the weekly updates pause until the payment succeeds.")
    lines += ["", "If you meant to cancel, no action is needed.", "", cfg.sender_name or "AutoMonetize"]
    email = Email(to=sub["email"], subject=subject, body="\n".join(lines), kind="delivery")
    try:
        outcome = tools.dispatcher.send_transactional(email, audit_key=f"dunning:{case['id']}:{number}")
    except Exception as exc:  # noqa: BLE001 - retried by run_dunning
        tools.state.log_error("retention", f"reminder {number} to subscriber {sub['id']} failed: {exc!r}")
        return "failed"
    tools.state._exec("UPDATE dunning_cases SET reminders_sent = ?, last_reminder_at = ? WHERE id = ?",
                      (number, tools.state.now(), case["id"]))
    return outcome


def start_case(tools: Any, subscriber_id: int, invoice_id: str | None, why: str) -> tuple[dict[str, Any] | None, bool]:
    """Open a dunning case (idempotent) and degrade API keys. Returns (case, created)."""
    from api.auth import ApiKeys

    state = tools.state
    sub = state.subscriber(subscriber_id)
    if not sub or sub.get("tier") != "paid" or sub.get("subscription_status") == "canceled":
        return None, False
    case = open_case(state, subscriber_id)
    created = case is None
    if created:
        now = state.clock()
        state._exec("INSERT INTO dunning_cases (subscriber_id, invoice_id, status, started_at, grace_until, detail) VALUES (?,?,?,?,?,?)",
                    (subscriber_id, invoice_id, "open", iso(now), iso(now + timedelta(days=tools.config.dunning_grace_days)), why[:200]))
        case = open_case(state, subscriber_id)
        state.log_action(int(state.get("iteration", 0)), None, "dunning:open", "ok", f"subscriber {subscriber_id}: {why}")
    if case and case["status"] == "open":
        ApiKeys(state, tools.config).sync_subscriber(subscriber_id, "degraded", why)
    return case, created


def on_payment_failed(tools: Any, inv: dict[str, Any]) -> tuple[str, Any]:
    """Webhook: returns (detail, after-callable or None). The reminder is sent after responding."""
    from strategies.subscription_engine import invoice_subscription_id

    sub_id = invoice_subscription_id(inv)
    sub = tools.state.get_subscriber(sub_id) if sub_id else None
    if not sub:
        return "not a known subscription", None
    case, created = start_case(tools, sub["id"], str(inv.get("id") or ""), f"invoice {inv.get('id')} payment failed")
    if case is None:
        return "ignored", None
    if created and sub.get("email"):
        return f"dunning opened for subscriber {sub['id']}", (lambda: reminder(tools, tools.state.subscriber(sub["id"]), open_case(tools.state, sub["id"]), 1))
    return f"dunning already open for subscriber {sub['id']}", None


def on_past_due(tools: Any, subscriber_id: int) -> None:
    """Polling or ``customer.subscription.updated`` saw ``past_due``: same as a failed payment."""
    case, created = start_case(tools, subscriber_id, None, "subscription past_due")
    sub = tools.state.subscriber(subscriber_id)
    if case and created and sub and sub.get("email"):
        reminder(tools, sub, case, 1)


def close_recovered(tools: Any, subscriber_id: int, why: str) -> bool:
    """Payment came through: close the open (or expired) case and give full access back."""
    from api.auth import ApiKeys

    case = open_case(tools.state, subscriber_id)
    if not case:
        return False
    tools.state._exec("UPDATE dunning_cases SET status = 'recovered', closed_at = ?, detail = ? WHERE id = ?",
                      (tools.state.now(), f"recovered: {why}"[:200], case["id"]))
    ApiKeys(tools.state, tools.config).sync_subscriber(subscriber_id, "active", why)
    tools.state.log_action(int(tools.state.get("iteration", 0)), None, "dunning:recovered", "ok",
                           f"subscriber {subscriber_id} after {case['reminders_sent']} reminder(s)")
    return True


def on_paid(tools: Any, inv: dict[str, Any]) -> bool:
    from strategies.subscription_engine import invoice_subscription_id

    sub_id = invoice_subscription_id(inv)
    sub = tools.state.get_subscriber(sub_id) if sub_id else None
    return bool(sub) and close_recovered(tools, sub["id"], f"invoice {inv.get('id')} paid")


def on_canceled(tools: Any, subscription: dict[str, Any]) -> bool:
    """``customer.subscription.deleted``: log churn once, close any case (keys are revoked by the status sync)."""
    from dashboard.analytics import monthly_amount

    state = tools.state
    sub = state.get_subscriber(subscription.get("id") or "")
    if not sub:
        return False
    case = open_case(state, sub["id"])
    if case:
        state._exec("UPDATE dunning_cases SET status = 'canceled', closed_at = ? WHERE id = ?", (state.now(), case["id"]))
    reason = ((subscription.get("cancellation_details") or {}).get("reason")
              or ("payment_failed" if case else "cancellation_requested"))
    tenure = (state.clock() - datetime.fromisoformat(sub["started_at"])).total_seconds() / 86400
    cur = state._exec(
        "INSERT OR IGNORE INTO churn_events (subscriber_id, product, reason, tenure_days, mrr_lost_cents, had_dunning, at) VALUES (?,?,?,?,?,?,?)",
        (sub["id"], product_of(state, sub), reason, round(tenure, 2), round(monthly_amount(sub["price_cents"], sub["interval"])),
         int(case is not None), state.now()))
    return cur.rowcount == 1


def churn_summary(state: Any, since: datetime) -> dict[str, Any]:
    rows = state._all("SELECT product, reason, COUNT(*) AS n, SUM(mrr_lost_cents) AS mrr FROM churn_events WHERE at >= ? GROUP BY product, reason",
                      (since.isoformat(timespec="seconds"),))
    cases = {r["status"]: r["n"] for r in state._all("SELECT status, COUNT(*) AS n FROM dunning_cases GROUP BY status")}
    return {"churned": sum(r["n"] for r in rows), "mrr_lost_cents": sum(int(r["mrr"] or 0) for r in rows),
            "by_reason": {f"{r['product']}:{r['reason']}": r["n"] for r in rows},
            "involuntary": sum(r["n"] for r in rows if r["reason"] == "payment_failed"),
            "dunning": {"open": cases.get("open", 0), "expired": cases.get("expired", 0), "recovered": cases.get("recovered", 0),
                        "canceled": cases.get("canceled", 0)}}


class RetentionEngine(Strategy):
    name = "retention_engine"
    tasks = ("run_dunning",)

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        """Follow-up reminders on schedule, and grace-period expiry."""
        from api.auth import ApiKeys

        tools, state, cfg = ctx.tools, ctx.tools.state, ctx.tools.config
        now = state.clock()
        sent = expired = 0
        for case in state._all("SELECT * FROM dunning_cases WHERE status = 'open' ORDER BY id"):
            sub = state.subscriber(case["subscriber_id"])
            if not sub:
                state._exec("UPDATE dunning_cases SET status = 'canceled', closed_at = ? WHERE id = ?", (state.now(), case["id"]))
                continue
            started = datetime.fromisoformat(case["started_at"])
            if now >= datetime.fromisoformat(case["grace_until"]):
                ApiKeys(state, cfg).sync_subscriber(sub["id"], "suspended", "grace period over, payment still failing")
                state._exec("UPDATE dunning_cases SET status = 'expired', detail = ? WHERE id = ?",
                            (f"grace ended {now.date()}", case["id"]))
                state.log_action(int(state.get("iteration", 0)), None, "dunning:expired", "ok", f"subscriber {sub['id']}: keys suspended")
                expired += 1
                continue
            due = [d for d in sorted(cfg.dunning_reminder_days) if now - started >= timedelta(days=d)]
            if sub.get("email") and len(due) > case["reminders_sent"]:
                sent += reminder(tools, sub, case, case["reminders_sent"] + 1) in ("delivered", "dry_run")
        summary = churn_summary(state, now - timedelta(days=30))
        return TaskResult(True, f"dunning: {sent} reminders, {expired} grace periods ended; open {summary['dunning']['open']}, "
                                f"recovered {summary['dunning']['recovered']}; churn 30d {summary['churned']}",
                          {"reminders": sent, "expired": expired, **summary})
