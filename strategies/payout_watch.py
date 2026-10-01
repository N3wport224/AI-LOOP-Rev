"""Payout watch (Phase 88): make sure the money actually reaches your bank.

From what ``sync_finance`` already reads (no extra Stripe calls), ``watch_payouts`` alerts you:

* when a payout **failed** or was **canceled** (usually a bank detail to fix in Stripe → Settings →
  Payouts), once per payout;
* when money has been **available** in Stripe for ``payout_watch_days`` (30) with no payout since
  (manual payouts, or a bank account still unverified), at most once a week.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from strategies.base import Strategy, TaskContext, TaskResult

KEY = "payout_watch"


class PayoutWatch(Strategy):
    name = "payout_watch"
    tasks = ("watch_payouts",)

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        cfg, state = ctx.tools.config, ctx.tools.state
        fin = state.get("stripe_finance") or {}
        if not fin or fin.get("error"):
            return TaskResult(True, "no Stripe balance read yet", {})
        data = dict(state.get(KEY) or {})
        today = state.clock().date()
        notes = []
        last = fin.get("last_payout") or {}
        tag = f"{last.get('arrival_date')}:{last.get('amount_cents')}"
        if last.get("status") in ("failed", "canceled") and data.get("failed_alerted") != tag:
            state.log_error("payout_watch", f"A ${int(last.get('amount_cents') or 0) / 100:,.2f} payout to your bank "
                                            f"{last['status']} ({last.get('arrival_date')}). Check Stripe → Settings → Payouts "
                                            "(bank details); Stripe retries once they're fixed.", kind="alert")
            data["failed_alerted"] = tag
            notes.append("failed payout")
        available = int(fin.get("available_cents") or 0)
        last_day = date.fromisoformat(last["arrival_date"]) if last.get("arrival_date") else None
        first_sale = state.first_revenue_at()
        waiting_since = last_day or (datetime.fromisoformat(first_sale).date() if first_sale else today)
        days = (today - waiting_since).days
        alerted = data.get("idle_alerted")
        if available > 0 and days >= int(cfg.payout_watch_days) and (
                not alerted or today - date.fromisoformat(alerted) >= timedelta(days=7)):
            state.log_error("payout_watch", f"${available / 100:,.2f} has been ready in Stripe for {days} days without a payout. "
                                            "Check Stripe → Settings → Payouts: the schedule may be manual, or the bank account "
                                            "may still need verifying.", kind="alert")
            data["idle_alerted"] = today.isoformat()
            notes.append("money waiting in Stripe")
        state.set(KEY, data)
        return TaskResult(True, "payouts: " + ("; ".join(notes) or "ok"), {"notes": len(notes)})
