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


def imap_settings(config: Any) -> tuple[str, str, str]:
    """(host, user, password) for reading the sender mailbox. Falls back to the SMTP login for the
    common providers, where the same app password works for IMAP (Gmail, Fastmail, Outlook, iCloud)."""
    if config.imap_host:
        return config.imap_host, config.imap_username or config.smtp_username, config.imap_password or config.smtp_password
    known = {"smtp.gmail.com": "imap.gmail.com", "smtp.fastmail.com": "imap.fastmail.com",
             "smtp.office365.com": "outlook.office365.com", "smtp-mail.outlook.com": "outlook.office365.com",
             "smtp.mail.me.com": "imap.mail.me.com"}
    host = known.get((config.smtp_host or "").lower(), "")
    return (host, config.smtp_username, config.smtp_password) if host and config.smtp_username else ("", "", "")


def scan_mail(host: str, username: str, password: str, wanted: Callable[[str], bool], since: str,
              imap_factory: Callable[[str], Any] = imaplib.IMAP4_SSL, max_messages: int = 300) -> list[dict[str, str]]:
    """Unread messages since ``since`` (IMAP date, e.g. 01-Oct-2026) whose sender passes ``wanted``.

    Read-only: headers first, bodies only for wanted senders, all with BODY.PEEK so nothing is marked
    read. The caller decides what to do; the mailbox is left exactly as it was."""
    out: list[dict[str, str]] = []
    conn = imap_factory(host)
    try:
        conn.login(username, password)
        conn.select("INBOX", readonly=True)
        status, data = conn.search(None, f"(UNSEEN SINCE {since})")
        if status != "OK":
            return out
        for num in (data[0].split() if data and data[0] else [])[-max_messages:]:
            status, parts = conn.fetch(num, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID)])")
            if status != "OK" or not parts or not isinstance(parts[0], tuple):
                continue
            head = email.message_from_bytes(parts[0][1])
            sender = parseaddr(head.get("From", ""))[1].lower()
            if not sender or not wanted(sender):
                continue
            status, parts = conn.fetch(num, "(BODY.PEEK[])")
            if status != "OK" or not parts or not isinstance(parts[0], tuple):
                continue
            msg = email.message_from_bytes(parts[0][1])
            out.append({"sender": sender, "subject": str(msg.get("Subject", "")), "message_id": str(msg.get("Message-ID", "") or
                        f"{sender}:{msg.get('Date', '')}:{msg.get('Subject', '')}"), "body": _text_of(msg)[:20000]})
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass
    return out
