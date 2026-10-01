"""Bounce guard: keep the sender address healthy by never mailing dead addresses twice.

Mail providers judge a sender by how often its mail bounces. ``process_bounces`` reads bounce
notices in the sender mailbox (read-only, like the support desk: only messages from
``mailer-daemon@`` / ``postmaster@``, nothing is marked read or moved):

* **Hard bounces** (the address doesn't exist, the domain is gone: "5.x.x", "wasn't delivered",
  "does not exist") → the address is suppressed for good. Temporary failures (mailbox full, "4.x.x")
  are ignored; the provider retries those itself.
* **Bounce rate**: if more than ``bounce_pause_rate`` (5%) of the last 7 days' marketing emails
  bounced (with at least 20 sent), all marketing email pauses for 7 days and you get an alert.
  Purchases, receipts and support replies are never paused.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from strategies.base import Strategy, TaskContext, TaskResult
from tools import contact_policy as contact

SEEN = "bounces_handled"
LOG = "bounces"
BOUNCE_SENDERS = ("mailer-daemon@", "postmaster@", "mail-daemon@")
TEMPORARY = re.compile(r"\b4\.\d\.\d\b|temporar|mailbox (is )?full|over quota|try again later|deferred", re.I)
PERMANENT = re.compile(r"\b5\.\d\.\d+\b|wasn'?t delivered|couldn'?t be (found|delivered)|does not exist|doesn'?t exist|"
                       r"no such user|user unknown|address not found|recipient address rejected|failed permanently|"
                       r"undeliverable|unknown user|invalid recipient", re.I)
ADDR = r"([A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})"
RECIPIENT = [re.compile(p, re.I) for p in (
    r"Final-Recipient:\s*rfc822;\s*<?" + ADDR,
    r"X-Failed-Recipients:\s*<?" + ADDR,
    r"(?:wasn'?t delivered to|delivery to the following recipient failed permanently:?|could not be delivered to:?)\s*<?" + ADDR,
    r"<" + ADDR + r">:?\s*(?:host|said|5\.\d\.\d)",
)]
WINDOW_DAYS = 7
MIN_SENDS = 20
PAUSE_DAYS = 7


def failed_recipients(body: str, own: set[str]) -> list[str]:
    """Hard-bounced addresses in a bounce notice; [] for temporary failures."""
    if TEMPORARY.search(body) and not PERMANENT.search(body):
        return []
    if not PERMANENT.search(body):
        return []
    found = []
    for pattern in RECIPIENT:
        for m in pattern.finditer(body):
            addr = m.group(1).strip(".,;:<>()[]\"'").lower()
            if "@" in addr and addr not in own and addr not in found and not addr.startswith(BOUNCE_SENDERS):
                found.append(addr)
    return found


def bounce_rate(state: Any) -> tuple[float, int, int]:
    start = state.clock() - timedelta(days=WINDOW_DAYS)
    since = start.isoformat(timespec="seconds")
    contact.ensure(state)
    sent = int(state._one("SELECT COUNT(*) AS n FROM contact_log WHERE sent_at >= ? AND kind != 'bounce'", (since,))["n"])
    sent += state.sent_since(start)  # approved sales emails (outreach) bounce too
    bounced = int(state._one("SELECT COUNT(*) AS n FROM contact_log WHERE sent_at >= ? AND kind = 'bounce'", (since,))["n"])
    return (bounced / sent if sent else 0.0), bounced, sent


class BounceGuard(Strategy):
    name = "bounce_guard"
    tasks = ("process_bounces",)

    def __init__(self, scan: Any = None):
        self._scan = scan  # injectable for tests

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        from tools.inbox import imap_settings, scan_mail

        cfg, state = ctx.tools.config, ctx.tools.state
        host, user, password = imap_settings(cfg)
        if not (host and user and password):
            return TaskResult(True, "mailbox not connected: bounces can't be read", {"bounced": 0})
        since = (state.clock() - timedelta(days=WINDOW_DAYS)).strftime("%d-%b-%Y")
        try:
            messages = (self._scan or scan_mail)(host, user, password, lambda s: s.startswith(BOUNCE_SENDERS), since)
        except Exception as exc:  # noqa: BLE001 - retried next cycle
            state.log_error("bounce_guard", f"couldn't read the mailbox: {exc!r}")
            return TaskResult(True, f"mailbox unreadable: {exc!r}"[:200], {"bounced": 0})
        handled = list(state.get(SEEN) or [])
        seen = set(handled)
        own = {a.lower() for a in (cfg.sender_email, cfg.owner_email, cfg.unsubscribe_email, user) if a}
        new = []
        for m in messages:
            if m["message_id"] in seen:
                continue
            for addr in failed_recipients(m["body"], own):
                if state.suppress(addr, "hard bounce"):
                    new.append(addr)
                contact.record(state, addr, "bounce", m["message_id"][:100])
            handled.append(m["message_id"])
            seen.add(m["message_id"])
        state.set(SEEN, handled[-3000:])
        if new:
            log = list(state.get(LOG) or [])
            log += [{"email": a, "at": state.now()} for a in new]
            state.set(LOG, log[-2000:])
        rate, bounced, sent = bounce_rate(state)
        paused = contact.paused(state)
        if sent >= MIN_SENDS and rate > float(cfg.bounce_pause_rate) and not paused:
            until = (state.clock() + timedelta(days=PAUSE_DAYS)).isoformat(timespec="seconds")
            reason = f"{bounced} of {sent} emails bounced in 7 days ({rate:.0%})"
            state.set(contact.PAUSE_KEY, {"until": until, "reason": reason, "at": state.now()})
            state.log_error("bounce_guard", f"Marketing email paused for {PAUSE_DAYS} days: {reason}. Bounced addresses are "
                                            "suppressed; purchases and support replies still go out.", kind="alert")
        return TaskResult(True, f"bounces: {len(new)} new hard bounce(s); 7-day rate {rate:.1%} of {sent}",
                          {"bounced": len(new), "rate": round(rate, 4), "sent": sent})
