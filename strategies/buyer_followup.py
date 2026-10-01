"""Buyer follow-up: one short "did it arrive, was it useful?" email after a purchase.

``follow_up_buyers`` writes once to each buyer ``buyer_followup_days`` (3) days after their file
was delivered: a plain check-in that asks whether everything arrived and what they'd want in the
next version, with a link to the full catalog. Replies land in your inbox, where the support desk
re-sends files on request and passes everything else to you.

* At most one follow-up per order, only for one-off purchases delivered 3-10 days ago (never
  subscriptions, refunds or disputes), at most 20 per cycle.
* Never to a suppressed address. Every email carries the postal address and a reply-"unsubscribe"
  opt-out (``List-Unsubscribe`` mailto); the support desk honours such replies.
* It carries the buyer's personal referral link (``strategies/referrals.py``): a friend's purchase
  earns them the next update free.
* Live sending waits until ``sender_postal_address`` is set. Off with ``buyer_followup = false``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from strategies.base import Strategy, TaskContext, TaskResult
from tools.dispatcher import Email

DONE_KEY = "buyer_followup_done"
WINDOW_DAYS = 10
PER_CYCLE = 20


def followup_email(cfg: Any, order: dict[str, Any], title: str, ref_link: str = "") -> Email:
    mailbox = cfg.unsubscribe_email or cfg.sender_email
    lines = [
        "Hi,",
        "",
        f"A few days ago you bought {title}. Did everything arrive and open fine?",
        "",
        "If anything is missing, just reply \"resend\" and it goes out again right away.",
        "And if there's a company, field or niche you'd like in the next version, reply and tell me: I read every answer.",
    ]
    if ref_link:
        lines += ["", "Know someone who'd use it? Here's your personal link. When they buy through it, you get the next "
                  "updated version free:", ref_link]
    if cfg.pages_base_url:
        lines += ["", f"Everything else on offer: {cfg.pages_base_url.rstrip('/')}/"]
    lines += ["", "Thanks for buying,", cfg.sender_name or "AutoMonetize", "", "--",
              "Don't want emails like this? Reply \"unsubscribe\"."]
    if cfg.sender_postal_address:
        lines.append(cfg.sender_postal_address)
    headers = {"List-Unsubscribe": f"<mailto:{mailbox}?subject=unsubscribe>"} if mailbox else {}
    return Email(to=order["email"], subject=f"Quick check: {title}", body="\n".join(lines), kind="delivery", headers=headers)


def due_orders(state: Any, days: int) -> list[dict[str, Any]]:
    now = state.clock()
    newest = (now - timedelta(days=days)).isoformat(timespec="seconds")
    oldest = (now - timedelta(days=WINDOW_DAYS)).isoformat(timespec="seconds")
    return state._all(
        "SELECT * FROM orders WHERE status = 'delivered' AND kind = 'one_off' AND email IS NOT NULL "
        "AND delivered_at IS NOT NULL AND delivered_at <= ? AND delivered_at >= ? ORDER BY id", (newest, oldest))


class BuyerFollowup(Strategy):
    name = "buyer_followup"
    tasks = ("follow_up_buyers",)

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        tools, cfg, state = ctx.tools, ctx.tools.config, ctx.tools.state
        if not cfg.buyer_followup:
            return TaskResult(True, "buyer follow-up off", {"sent": 0})
        if tools.dispatcher.live and not cfg.sender_postal_address.strip():
            return TaskResult(True, "buyer follow-up waits for sender_postal_address (CAN-SPAM)", {"sent": 0})
        done = list(state.get(DONE_KEY) or [])
        seen = set(done)
        emailed: set[str] = set()
        sent = 0
        for order in due_orders(state, int(cfg.buyer_followup_days)):
            if sent >= PER_CYCLE:
                break
            email = order["email"].lower()
            if order["id"] in seen:
                continue
            if state.is_suppressed(email) or email in emailed:  # one email per buyer per cycle
                done.append(order["id"])
                seen.add(order["id"])
                continue
            asset = state.get_asset(order["asset_id"]) if order.get("asset_id") else None
            try:
                from strategies.referrals import referral_link

                url = str((asset or {}).get("checkout_url") or "")
                ref = referral_link(state, email, url) if cfg.referrals and url.startswith("https://") else ""
                tools.dispatcher.send_transactional(followup_email(cfg, order, asset["title"] if asset else "your dataset", ref),
                                                    audit_key=f"followup:{order['id']}")
            except Exception as exc:  # noqa: BLE001 - retried next cycle
                state.log_error("buyer_followup", f"follow-up to order {order['id']} failed: {exc!r}")
                continue
            done.append(order["id"])
            seen.add(order["id"])
            emailed.add(email)
            sent += 1
        state.set(DONE_KEY, done[-5000:])
        return TaskResult(True, f"buyer follow-up: {sent} sent", {"sent": sent})
