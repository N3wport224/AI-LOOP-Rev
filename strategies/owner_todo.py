"""Owner to-do: the few things only you can do, ranked by what they unlock, each with its command.

The agent does everything it's allowed to. What's left needs your accounts, your decisions or your
legal details. ``todo(state, config)`` turns the current state into a short list, most valuable
first, each item with why it matters, roughly how long it takes and the exact command or place:

* switch to real payments (nothing sells before);
* customers waiting: a dispute to answer, an order the agent couldn't deliver;
* the postal address (follow-ups, release, refresh and win-back emails wait for it);
* the public site and articles (traffic);
* sales emails waiting for your OK;
* the heartbeat (you hear about it if the Mac stops);
* an update the agent couldn't install by itself.

Shown at the top of the control panel, in every daily report and by ``automonetize todo``. Items
disappear by themselves once done.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

DISPUTE_WINDOW_DAYS = 21


def todo(state: Any, cfg: Any) -> list[dict[str, Any]]:
    from agent.self_update import info as update_info

    items: list[dict[str, Any]] = []

    def add(rank: int, title: str, why: str, minutes: int, how: str) -> None:
        items.append({"rank": rank, "title": title, "why": why, "minutes": minutes, "how": how})

    live_key = str(cfg.stripe_secret_key or "").startswith(("sk_live_", "rk_live_"))
    if not live_key or cfg.dry_run:
        add(1, "Switch to real payments", "nothing can be sold in test mode or dry run", 10, "automonetize go-live")
    since = (state.clock() - timedelta(days=DISPUTE_WINDOW_DAYS)).isoformat(timespec="seconds")
    disputes = int(state._one(  # recent disputes not yet won (the evidence window is about 3 weeks)
        "SELECT COUNT(*) AS n FROM revenue d WHERE d.external_id LIKE 'dispute:%' AND d.occurred_at >= ? AND NOT EXISTS "
        "(SELECT 1 FROM revenue w WHERE w.external_id = 'dispute_won:' || substr(d.external_id, 9))", (since,))["n"])
    if disputes:
        add(2, f"Answer {disputes} dispute(s) in Stripe", "an unanswered dispute is lost automatically", 10,
            "dashboard.stripe.com → Payments → Disputes")
    manual = int(state._one("SELECT COUNT(*) AS n FROM orders WHERE status = 'needs_manual_delivery'")["n"])
    if manual:
        add(3, f"Deliver {manual} order(s) by hand", "a customer paid and hasn't received their file", 5,
            "automonetize orders list --status needs_manual_delivery, then automonetize orders deliver <order id> <asset id>")
    if not str(cfg.sender_postal_address or "").strip():
        add(4, "Add your postal address", "follow-up, new-release, refresh and win-back emails can't be sent without it (CAN-SPAM)",
            2, "control panel → Settings → Compliance → Postal address (a PO box works)")
    if not (cfg.github_pages_repo and cfg.pages_base_url):
        add(5, "Connect marketing", "without a public site and articles, almost nobody finds the products", 5,
            "automonetize connect-marketing")
    drafts = int(state.outreach_counts().get("pending_review", 0))
    if drafts:
        add(6, f"Review {drafts} sales email(s)", "the agent wrote them but never sends without your OK", 5,
            "control panel → Outreach")
    if not cfg.heartbeat_url:
        add(7, "Turn on the heartbeat", "otherwise nobody tells you if the Mac or the agent stops", 2, "automonetize heartbeat")
    status = str(update_info(state).get("status") or "")
    if status.startswith("not updating:") and "upstream" not in status:
        add(8, "Let the agent update itself", status, 2, "automonetize doctor --fix")
    return sorted(items, key=lambda i: i["rank"])


def as_text(items: list[dict[str, Any]]) -> str:
    if not items:
        return "Nothing for you to do: the agent has everything it needs."
    return "\n".join(f"{n}. {i['title']} (~{i['minutes']} min): {i['why']}.\n   → {i['how']}" for n, i in enumerate(items, 1))
