"""Key-age reminders (Phase 90): nudge you to roll keys that have been in use a long time.

The agent never stores a key's value outside ``.env``. Here it keeps only a 12-character fingerprint
(SHA-256) of each secret and the date it first saw it, so it can tell when a key changed and how
old the current one is. A key older than its limit (Stripe: 180 days; the others: a year) shows up
on the to-do list with where to roll it. Updated by the daily security audit. kv ``key_ages``.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

KEY = "key_ages"
SECRETS = {  # config attribute: (label, max age in days, where to roll it)
    "stripe_secret_key": ("Stripe key", 180, "Stripe → Developers → API keys → roll, then `automonetize go-live`"),
    "smtp_password": ("email app password", 365, "Google Account → Security → App passwords; then control panel → Settings"),
    "github_token": ("GitHub token", 365, "github.com/settings/tokens; then `automonetize connect-marketing`"),
    "devto_api_key": ("Dev.to key", 365, "dev.to/settings/extensions; then `automonetize connect-marketing`"),
}


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def update(state: Any, config: Any) -> dict[str, Any]:
    ages = dict(state.get(KEY) or {})
    for attr in SECRETS:
        value = str(getattr(config, attr, "") or "")
        if not value:
            ages.pop(attr, None)
            continue
        fp = fingerprint(value)
        if (ages.get(attr) or {}).get("fp") != fp:
            ages[attr] = {"fp": fp, "since": state.now()}
    state.set(KEY, ages)
    return ages


def overdue(state: Any) -> list[dict[str, Any]]:
    out = []
    now = state.clock()
    for attr, rec in (state.get(KEY) or {}).items():
        label, days, how = SECRETS.get(attr, (attr, 365, ""))
        age = (now - datetime.fromisoformat(rec["since"])).days
        if now - datetime.fromisoformat(rec["since"]) >= timedelta(days=days):
            out.append({"attr": attr, "label": label, "age_days": age, "how": how})
    return out
