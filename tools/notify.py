"""Phone notifications through ntfy (Phase 60): a buzz on your phone for every sale and alert.

ntfy (https://ntfy.sh) is free and needs no account: the agent POSTs a short message to a topic,
and the ntfy app on your phone, subscribed to the same topic, shows it. The topic name is the only
secret, so ``automonetize phone`` makes a long random one and saves it as ``NTFY_TOPIC``.

Messages carry only what's needed at a glance (amount, product, "needs attention"), never a
customer's email address: they pass through the ntfy server.
"""

from __future__ import annotations

import re
import secrets
from typing import Any

SERVER = "https://ntfy.sh"


def new_topic() -> str:
    return "am-" + secrets.token_urlsafe(18).replace("_", "").replace("-", "").lower()[:24]


def enabled(config: Any) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{8,64}", str(getattr(config, "ntfy_topic", "") or "")))


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def no_emails(text: str) -> str:
    return _EMAIL.sub("[email]", str(text or ""))


def _ascii(text: str) -> str:
    # HTTP headers must be latin-1; keep titles plain.
    return text.encode("ascii", "ignore").decode().strip()[:120]


def push(http: Any, config: Any, title: str, message: str, priority: str = "default", tags: str = "",
         state: Any = None) -> bool:
    """Send one notification (phone, and Slack/Discord when set: Phase 306). Never raises: returns
    whether either accepted it."""
    from tools.chat import post as chat_post

    title, message = no_emails(title), no_emails(message)  # these pass through ntfy, Slack or Discord
    chatted = chat_post(http, config, title, message, state)
    if not enabled(config):
        return chatted
    headers = {"Title": _ascii(title), "Priority": priority}
    if tags:
        headers["Tags"] = tags
    server = str(getattr(config, "ntfy_server", "") or SERVER).rstrip("/")
    try:
        http.post(f"{server}/{config.ntfy_topic}", raw_body=message[:1000].encode(), headers=headers, check_robots=False, attempts=2)
        return True
    except Exception:  # noqa: BLE001 - a missed buzz must never fail a sale email or a cycle
        return chatted
