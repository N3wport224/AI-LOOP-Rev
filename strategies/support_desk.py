"""Customer support autopilot: answers "I didn't get my file" by itself, tells you about the rest.

``answer_support`` reads the sender mailbox over IMAP each cycle (the same Gmail app password as
SMTP works; ``tools.inbox.imap_settings``). It's deliberately narrow:

* **Only customers.** Only unread mail from an address that bought something or subscribes is
  looked at; everything else in your inbox is never opened. The mailbox is opened read-only, so
  nothing is marked read, moved or deleted. Handled messages are remembered by Message-ID.
* **"Didn't get it / resend / can't download / lost the file"** → everything that customer bought
  is re-sent at once (the same path as ``POST /v1/orders/recover``, with its per-address daily cap).
* **Refunds, cancellations, disputes, complaints** → never answered automatically: you get an
  alert email (through the owner reports) with the customer, subject and first lines. Money
  decisions stay yours.
* **"Delete my data" / GDPR / CCPA** → recorded as a privacy request and passed to you with the
  exact commands (``tools/privacy.py``); erasing is your decision.
* **"OK to quote"** → the reply becomes a pending testimonial (``strategies/testimonials.py``),
  published on the product page only after you approve it.
* **"Unsubscribe"** → the address goes on the suppression list (no more follow-ups or updates).
* **Anything else from a customer** → the same alert, once, so a real question is never missed.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from strategies.base import Strategy, TaskContext, TaskResult

RESEND_RE = re.compile(
    r"\b(didn'?t|did not|haven'?t|have not|never)\s+(get|got|receive[d]?)\b|\bnot\s+(received|arrived|delivered)\b|"
    r"\bre-?send\b|\bsend (it|the (file|dataset|data|zip)) (again|over)\b|\b(can'?t|cannot|unable to|couldn'?t)\s+(find|download|open)\b|"
    r"\bdownload (link|it)\b|\bwhere('?s| is) (my|the) (file|dataset|data|download|order|purchase)\b|\blost (my|the)\b|\bmissing\b|"
    r"\bno (email|file|attachment)\b", re.I)
MONEY_RE = re.compile(r"\b(refund|money back|charge ?back|dispute|cancel|scam|fraud|unauthori[sz]ed|wrong charge|overcharged)\b", re.I)
SEEN_KEY = "support_handled"
LOOKBACK_DAYS = 7


def fresh_text(subject: str, body: str) -> str:
    """The customer's own words: no quoted reply chain."""
    lines = [ln for ln in body.splitlines() if not ln.lstrip().startswith(">")]
    text = re.split(r"\n(On .+ wrote:|-----Original Message-----|From: )", "\n".join(lines))[0]
    return f"{subject}\n{text}"


def classify(subject: str, body: str) -> tuple[bool, bool]:
    """(asks for a resend, raises a money issue)."""
    text = fresh_text(subject, body)
    return bool(RESEND_RE.search(text)), bool(MONEY_RE.search(text))


def customers(state: Any) -> set[str]:
    emails = {(r["email"] or "").lower() for r in state._all("SELECT email FROM orders WHERE email IS NOT NULL")}
    emails |= {(r["email"] or "").lower() for r in state._all("SELECT email FROM subscribers WHERE email IS NOT NULL AND tier = 'paid'")}
    return {e for e in emails if e}


class SupportDesk(Strategy):
    name = "support_desk"
    tasks = ("answer_support",)

    def __init__(self, scan: Any = None):
        self._scan = scan  # injectable for tests

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        from tools.inbox import imap_settings, is_unsubscribe, scan_mail
        from tools.storefront.recovery_endpoint import RecoveryService

        tools, cfg, state = ctx.tools, ctx.tools.config, ctx.tools.state
        host, user, password = imap_settings(cfg)
        if not (host and user and password):
            return TaskResult(True, "support inbox not connected (uses your email login; IMAP host unknown)", {"handled": 0})
        known = customers(state)
        if not known:
            return TaskResult(True, "no customers yet: nothing to watch", {"handled": 0})
        since = (state.clock() - timedelta(days=LOOKBACK_DAYS)).strftime("%d-%b-%Y")
        try:
            messages = (self._scan or scan_mail)(host, user, password, lambda s: s in known, since)
        except Exception as exc:  # noqa: BLE001 - e.g. IMAP off or a changed password: retried next cycle
            state.log_error("support_desk", f"couldn't read the inbox: {exc!r}")
            return TaskResult(True, f"inbox unreadable: {exc!r}"[:200], {"handled": 0})
        handled = list(state.get(SEEN_KEY) or [])
        seen = set(handled)
        from strategies.testimonials import add_pending, niche_for_customer
        from tools.privacy import PRIVACY_RE, note_request
        from strategies.testimonials import extract as extract_quote

        resent = alerted = processed = unsubscribed = quotes = 0
        recovery = RecoveryService(tools)
        for m in messages:
            if m["message_id"] in seen:
                continue
            processed += 1
            wants_resend, money = classify(m["subject"], m["body"])
            if PRIVACY_RE.search(fresh_text(m["subject"], m["body"])):
                if note_request(state, m["sender"], m["subject"]):
                    state.log_error("support_desk", f"Privacy request from {m['sender']}: \"{m['subject'][:120]}\". See what's stored "
                                                    f"with `automonetize privacy export {m['sender']}`; erase it with "
                                                    f"`automonetize privacy forget {m['sender']}`. Answer within 30 days.", kind="alert")
                    alerted += 1
                handled.append(m["message_id"])
                seen.add(m["message_id"])
                continue
            quote = None if money else extract_quote(m["subject"], m["body"])
            if quote:
                add_pending(state, m["sender"], niche_for_customer(state, m["sender"]), quote)
                quotes += 1
                if not wants_resend:  # a consenting testimonial needs your OK in the panel, not an alert
                    handled.append(m["message_id"])
                    seen.add(m["message_id"])
                    continue
            if is_unsubscribe(m["subject"], m["body"]):
                state.suppress(m["sender"], "replied unsubscribe")
                unsubscribed += 1
                if not (money or wants_resend):  # a plain opt-out needs nothing from you
                    handled.append(m["message_id"])
                    seen.add(m["message_id"])
                    continue
            if wants_resend:
                result = recovery.dispatch(m["sender"])
                resent += int(bool(result.get("sent")))
                state.log_action(int(state.get("iteration", 0)), None, "support:resend", "ok" if result.get("sent") else "skipped",
                                 f"{m['sender']}: {result}")
            if money or not wants_resend:
                first = " ".join(fresh_text("", m["body"]).split())[:300]
                why = "asks about a refund/cancellation/charge" if money else "wrote to you"
                state.log_error("support_desk", f"customer {m['sender']} {why}: \"{m['subject'][:120]}\" {first}"
                                + (" (their purchase was also re-sent)" if wants_resend else ""), kind="alert")
                alerted += 1
            handled.append(m["message_id"])
            seen.add(m["message_id"])
        state.set(SEEN_KEY, handled[-3000:])
        return TaskResult(True, f"support: {processed} new customer email(s), {resent} resent, {alerted} passed to you"
                          + (f", {unsubscribed} unsubscribed" if unsubscribed else "") + (f", {quotes} quote(s) for your OK" if quotes else ""),
                          {"handled": processed, "resent": resent, "alerted": alerted, "unsubscribed": unsubscribed, "quotes": quotes})
