"""Disposable email domains: refused at the free-sample form (Phase 58).

Throwaway inboxes inflate the lead list, never buy, and their bounces hurt the sender address.
The built-in list covers the common services; add more with ``blocked_signup_domains`` in
``automonetize.toml``. Subdomains count (``x.mailinator.com`` is mailinator).
"""

from __future__ import annotations

from typing import Any

DISPOSABLE = frozenset({
    "mailinator.com", "guerrillamail.com", "guerrillamail.net", "guerrillamail.org", "sharklasers.com", "grr.la",
    "10minutemail.com", "10minutemail.net", "temp-mail.org", "tempmail.com", "tempmailo.com", "tempail.com", "yopmail.com",
    "yopmail.net", "trashmail.com", "trashmail.de", "getnada.com", "nada.email", "maildrop.cc", "dispostable.com",
    "throwawaymail.com", "fakeinbox.com", "mintemail.com", "mohmal.com", "emailondeck.com", "burnermail.io", "moakt.com",
    "mailnesia.com", "spamgourmet.com", "mytemp.email", "tmpmail.org", "tmpmail.net", "temp-mail.io", "discard.email",
    "getairmail.com", "mailcatch.com", "spambox.us", "33mail.com", "inboxkitten.com", "mail.tm", "emailfake.com",
    "throwam.com", "linshiyouxiang.net", "tempr.email", "harakirimail.com", "mvrht.com", "anonbox.net", "byom.de",
})


def is_disposable(email: str, config: Any = None) -> bool:
    domain = email.rsplit("@", 1)[-1].strip().lower()
    blocked = DISPOSABLE | {d.strip().lower() for d in (getattr(config, "blocked_signup_domains", None) or []) if d.strip()}
    parts = domain.split(".")
    return any(".".join(parts[i:]) in blocked for i in range(len(parts) - 1))
