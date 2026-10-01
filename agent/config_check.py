"""Config check: catch a typo in ``automonetize.toml`` or ``.env`` before it silently does nothing.

``problems(config, toml_path)`` returns plain-language findings, shown by ``automonetize doctor``:

* **Unknown settings** in ``automonetize.toml`` (a misspelt key is otherwise ignored), with the
  closest real name ("did you mean ...?").
* **Out-of-range values:** percentages outside 1-90, negative or zero day counts, a daily target
  of $0, a backup period shorter than a day, and similar.
* **Malformed values:** email addresses and URLs that can't work.
"""

from __future__ import annotations

import difflib
import re
import tomllib
from dataclasses import fields
from pathlib import Path
from typing import Any

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PERCENT = ("refresh_discount_pct", "winback_discount_pct", "sample_offer_pct", "sale_pct", "launch_discount_pct")
POSITIVE = ("daily_target_cents", "interval_seconds", "backup_keep_days", "buyer_followup_days", "announce_min_gap_days",
            "refresh_after_days", "sale_days", "sale_every_days", "stale_after_days", "log_keep_days", "keep_versions",
            "promo_daily_cap", "storefront_check_hours", "auto_update_hours")
EMAILS = ("sender_email", "owner_email", "unsubscribe_email")
URLS = ("pages_base_url", "public_webhook_url", "heartbeat_url")


def toml_keys(path: Path) -> set[str]:
    if not path.exists():
        return set()
    data = tomllib.loads(path.read_text())
    return set(data.get("automonetize", data))  # the same rule Config.load uses


def problems(config: Any, toml_path: Path | None = None) -> list[str]:
    out: list[str] = []
    known = {f.name for f in fields(config)}
    if toml_path is not None:
        try:
            for key in sorted(toml_keys(toml_path) - known):
                guess = difflib.get_close_matches(key, known, n=1)
                out.append(f"automonetize.toml: unknown setting '{key}'" + (f" (did you mean '{guess[0]}'?)" if guess else "")
                           + "; it is ignored")
        except (OSError, tomllib.TOMLDecodeError) as exc:
            out.append(f"automonetize.toml can't be read: {exc}")
    for name in PERCENT:
        v = getattr(config, name, None)
        if v is not None and not 1 <= int(v) <= 90:
            out.append(f"{name} = {v}: use a discount between 1 and 90")
    for name in POSITIVE:
        v = getattr(config, name, None)
        if v is not None and float(v) <= 0:
            out.append(f"{name} = {v}: must be more than 0")
    for name in EMAILS:
        v = str(getattr(config, name, "") or "")
        if v and not EMAIL_RE.match(v):
            out.append(f"{name} = '{v}' isn't an email address")
    for name in URLS:
        v = str(getattr(config, name, "") or "")
        if v and not v.startswith("https://"):
            out.append(f"{name} = '{v}' must start with https://")
    if getattr(config, "bounce_pause_rate", 0.05) <= 0 or getattr(config, "bounce_pause_rate", 0.05) >= 1:
        out.append(f"bounce_pause_rate = {config.bounce_pause_rate}: use a fraction like 0.05")
    return out
