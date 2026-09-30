"""Poll the sender mailbox over IMAP and honour unsubscribe replies automatically."""

from __future__ import annotations

import email
import imaplib
import re
from email.utils import parseaddr
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # pragma: no cover
    from agent.state import StateStore

UNSUBSCRIBE_RE = re.compile(
    r"\b(unsubscribe|remove me|opt[\s-]?out|stop (emailing|contacting)|no thanks|not interested|do not contact)\b", re.I
)


def _text_of(msg: email.message.Message) -> str:
    parts = []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_type() == "text/plain":
            payload = part.get_payload(decode=True) or b""
            parts.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
    return "\n".join(parts)


def is_unsubscribe(subject: str, body: str) -> bool:
    # Only look at the reply itself, not the quoted original (which contains our own opt-out line).
    fresh = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith(">"))
    fresh = re.split(r"\n(On .+ wrote:|-----Original Message-----)", fresh)[0]
    return bool(UNSUBSCRIBE_RE.search(subject or "") or UNSUBSCRIBE_RE.search(fresh))


def poll_unsubscribes(
    state: "StateStore", host: str, username: str, password: str,
    imap_factory: Callable[[str], Any] = imaplib.IMAP4_SSL, max_messages: int = 200,
) -> list[str]:
    """Suppress every sender of an unread unsubscribe reply. Returns the suppressed addresses."""
    suppressed: list[str] = []
    conn = imap_factory(host)
    try:
        conn.login(username, password)
        conn.select("INBOX")
        status, data = conn.search(None, "UNSEEN")
        if status != "OK":
            return suppressed
        for num in (data[0].split() if data and data[0] else [])[:max_messages]:
            status, parts = conn.fetch(num, "(RFC822)")
            if status != "OK" or not parts or not isinstance(parts[0], tuple):
                continue
            msg = email.message_from_bytes(parts[0][1])
            sender = parseaddr(msg.get("From", ""))[1].lower()
            if sender and is_unsubscribe(msg.get("Subject", ""), _text_of(msg)):
                if state.suppress(sender, "unsubscribe reply"):
                    suppressed.append(sender)
                conn.store(num, "+FLAGS", "\\Seen")
            else:
                conn.store(num, "-FLAGS", "\\Seen")  # leave other mail unread for the human
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass
    return suppressed
