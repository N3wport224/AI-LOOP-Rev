"""Email lint (Phase 140): a last look at every email before it leaves.

Marketing emails (follow-ups, releases, refresh and upgrade offers, win-back, sales, sample offers)
must carry what CAN-SPAM requires: an unsubscribe instruction and your postal address in the
body, a ``List-Unsubscribe`` header and no leftover template placeholders (``{title}``, ``None``),
the signs of a bug that would look careless to a buyer. Every email needs a subject without them.

A failing email isn't sent: ``EmailLintError`` is raised, the sending task logs it and retries
next cycle, and you get one alert per email.
"""

from __future__ import annotations

import re
from typing import Any

MARKETING = ("followup", "release", "refresh", "upgrade", "winback", "sale", "sample_offer")
PLACEHOLDER = re.compile(r"\{[a-z_][a-z0-9_]*\}|\bNone\b|\bnan\b")


class EmailLintError(ValueError):
    pass


def is_marketing(audit_key: str) -> bool:
    return audit_key.split(":", 1)[0] in MARKETING


def problems(email: Any, cfg: Any, audit_key: str) -> list[str]:
    out = []
    subject, body = str(email.subject or ""), str(email.body or "")
    if not subject.strip():
        out.append("no subject")
    # Bodies can quote people ("None of the links work"), so only our own templates are checked there.
    checked = subject + (" " + body if is_marketing(audit_key) else "")
    leftover = PLACEHOLDER.search(checked)
    if leftover:
        out.append(f"leftover placeholder: {leftover.group(0)}")
    if is_marketing(audit_key):
        if "unsubscribe" not in body.lower():
            out.append("no unsubscribe instruction in the body")
        address = str(cfg.sender_postal_address or "").strip()
        if not address or address.splitlines()[0] not in body:
            out.append("no postal address in the body")
        if not any(k.lower() == "list-unsubscribe" for k in (email.headers or {})):
            out.append("no List-Unsubscribe header")
    return out


def check(email: Any, cfg: Any, state: Any, audit_key: str) -> None:
    found = problems(email, cfg, audit_key)
    if not found:
        return
    alerted = list(state.get("email_lint_alerted") or [])
    if audit_key not in alerted:
        state.log_error("email_lint", f"Not sent: \"{email.subject}\" to {email.to}: {'; '.join(found)}. "
                                      "Usually a missing setting (postal address) or a bug in that email.", kind="alert")
        state.set("email_lint_alerted", (alerted + [audit_key])[-500:])
    raise EmailLintError("; ".join(found))
