"""Sales and alerts in Slack or Discord (Phases 305-309).

* **Phase 305, one setting:** ``chat_webhook_url`` (``CHAT_WEBHOOK_URL`` in ``.env``) is a Slack
  incoming-webhook or Discord webhook address. Only those two services are accepted, so a typo can't
  send your notifications somewhere else. Treat it like a password.
* **Phase 306, everything your phone gets:** every notification (each sale, alerts, milestones, the
  daily one-line summary, "the agent restarted") is also posted to the channel. Same content: amounts
  and product names, never a customer's email address.
* **Phase 307, set up and test:** ``automonetize chat <webhook-url>`` checks the address, saves it and
  posts a test message; ``automonetize chat test`` posts another.
* **Phase 308, never a flood:** at most ``PER_HOUR`` (30) messages an hour; past that the rest are
  counted and the next message says how many were held back.
* **Phase 309, visible:** ``automonetize features`` and doctor show whether chat notifications are on
  and to which service; the address itself is never printed or logged.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

PER_HOUR = 30
KEY = "chat_sent"
_SLACK = re.compile(r"^https://hooks\.slack\.com/services/[A-Za-z0-9/_-]{10,200}$")
_DISCORD = re.compile(r"^https://(?:discord\.com|discordapp\.com|ptb\.discord\.com)/api/webhooks/\d{5,30}/[A-Za-z0-9_-]{20,120}$")


def service(url: str) -> str:
    """"slack", "discord" or "" (not an accepted webhook address)."""
    url = str(url or "").strip()
    return "slack" if _SLACK.match(url) else "discord" if _DISCORD.match(url) else ""


def enabled(config: Any) -> bool:
    return bool(service(getattr(config, "chat_webhook_url", "")))


def _allow(state: Any) -> tuple[bool, int]:
    """Phase 308: (may send, messages held back since the last one sent)."""
    if state is None:
        return True, 0
    rec = dict(state.get(KEY) or {})
    now = state.clock()
    if not rec.get("hour") or now - datetime.fromisoformat(rec["hour"]) >= timedelta(hours=1):
        held = int(rec.get("held") or 0)
        state.set(KEY, {"hour": state.now(), "sent": 1, "held": 0})
        return True, held
    if int(rec.get("sent") or 0) >= PER_HOUR:
        rec["held"] = int(rec.get("held") or 0) + 1
        state.set(KEY, rec)
        return False, 0
    rec["sent"] = int(rec.get("sent") or 0) + 1
    state.set(KEY, rec)
    return True, 0


def post(http: Any, config: Any, title: str, message: str, state: Any = None) -> bool:
    """Post one notification. Never raises."""
    url = str(getattr(config, "chat_webhook_url", "") or "").strip()
    kind = service(url)
    if not kind:
        return False
    ok, held = _allow(state)
    if not ok:
        return False
    text = f"{title}\n{message}"[:1800] + (f"\n({held} more notification(s) were held back in the last hour)" if held else "")
    body = {"text": text} if kind == "slack" else {"content": text, "allowed_mentions": {"parse": []}}
    try:
        http.post(url, json_body=body, check_robots=False, attempts=2)
        return True
    except Exception:  # noqa: BLE001 - a missed chat message must never fail a sale or a cycle
        return False
