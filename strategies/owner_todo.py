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
    from strategies.testimonials import listing

    quotes = len(listing(state, "pending"))
    if quotes:
        add(6, f"Approve {quotes} customer quote(s)", "real quotes on the product page help visitors decide; none is shown without your OK",
            2, "control panel → Share → Customer quotes")
    privacy = state.get("privacy_requests") or {}
    if privacy:
        first = sorted(privacy.items(), key=lambda kv: kv[1]["at"])[0]
        add(2, f"Answer {len(privacy)} privacy request(s)", f"the law gives you 30 days (oldest: {first[1]['at'][:10]})", 5,
            f"automonetize privacy export {first[0]}, then automonetize privacy forget {first[0]}")
    for f in (state.get("security_audit") or {}).get("findings", []):
        if f["status"] == "fail" or f["name"] == "Stripe key type":
            add(2 if f["status"] == "fail" else 9, f"Security: {f['name']}", f["detail"], 10, f["fix"])
    for d in (state.get("payment_guard") or {}).get("open_duplicates", []):
        add(3, f"Check a double purchase by {d['email']}", f"{d['title']} was bought twice (orders {d['first']} and {d['second']})",
            3, "if it was an accident: Stripe → Payments → refund the second one")
    suggestion = state.get("goal_suggestion")
    if suggestion and int(suggestion["cents"]) > int(cfg.daily_target_cents):
        dollars = int(suggestion["cents"]) // 100
        add(9, f"Raise your daily goal to ${dollars}", f"you've been on goal {suggestion['streak']} days in a row; a higher goal "
            "keeps the agent pushing (pricing, new niches)", 1, f"automonetize goal {dollars}")
    from tools.key_age import overdue

    for k in overdue(state):
        add(9, f"Roll your {k['label']}", f"it's been in use {k['age_days']} days; a fresh one limits the damage if the old one "
            "ever leaked", 5, k["how"])
    if cfg.lead_capture_base:
        from agent.tunnel import missing_paths

        gone = missing_paths()
        if gone:
            add(3, "Re-run the tunnel setup", f"download and rating links ({', '.join(gone)}) don't reach the agent yet", 2,
                "bash deploy/tunnel/setup_tunnel.sh <your hostname>")
    from strategies.money_insight import refund_problem

    refunds = refund_problem(state, cfg)
    if refunds:
        add(3, "Look into refunds", f"{refunds['refunded']} of {refunds['orders']} orders in 30 days were refunded or disputed "
            f"({refunds['rate'] * 100:.0f}%); usually one dataset has a problem", 15,
            "automonetize orders list --status refunded, then read the buyers' messages")
    from strategies.ratings import unhappy

    sad = unhappy(state)
    if sad:
        add(3, f"Reach out to {len(sad)} unhappy buyer(s)", f"rated \"not good\" recently (e.g. {sad[0]['email']})", 5,
            "reply to their follow-up email and ask what went wrong")
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
