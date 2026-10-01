"""Config check: catch a typo in ``automonetize.toml`` or ``.env`` before it silently does nothing.

``problems(config, toml_path)`` returns plain-language findings, shown by ``automonetize doctor``:

* **Unknown settings** in ``automonetize.toml`` (a misspelt key is otherwise ignored), with the
  closest real name ("did you mean ...?").
* **Out-of-range values:** percentages outside 1-90, negative or zero day counts, a daily target
  of $0, a backup period shorter than a day, and similar.
* **Malformed values:** email addresses and URLs that can't work.
* **Factory and newer settings** (Phase 385): unknown ``factory_types``, a factory interval under a
  minute, very low product caps, and a chat webhook that isn't Slack or Discord.
* **Times and fractions** (Phase 141): a time zone Python doesn't know (reports would silently use
  UTC), hours outside 0-23, weekdays outside 0-6, and rates that must be a fraction (0.10, not 10).
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
HOURS = ("subscription_delivery_hour", "lead_nurture_hour", "owner_digest_hour")
WEEKDAYS = ("subscription_delivery_weekday", "lead_nurture_weekday")
FRACTIONS = ("refund_alert_rate", "source_max_error_rate", "affiliate_rate")


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
    tz = str(getattr(config, "subscription_timezone", "UTC") or "UTC")
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(tz)
    except Exception:  # noqa: BLE001 - any failure means the name is unusable
        out.append(f"subscription_timezone = '{tz}' isn't a known time zone (e.g. 'America/New_York'); UTC is used")
    for name in HOURS:
        v = getattr(config, name, None)
        if v is not None and not 0 <= int(v) <= 23:
            out.append(f"{name} = {v}: use an hour from 0 to 23")
    for name in WEEKDAYS:
        v = getattr(config, name, None)
        if v is not None and not 0 <= int(v) <= 6:
            out.append(f"{name} = {v}: use 0 (Monday) to 6 (Sunday)")
    for name in FRACTIONS:
        v = getattr(config, name, None)
        if v is not None and not 0 < float(v) < 1:
            out.append(f"{name} = {v}: use a fraction like 0.10 (for 10%)")
    if getattr(config, "bounce_pause_rate", 0.05) <= 0 or getattr(config, "bounce_pause_rate", 0.05) >= 1:
        out.append(f"bounce_pause_rate = {config.bounce_pause_rate}: use a fraction like 0.05")
    out += factory_problems(config)
    return out


def factory_problems(config: Any) -> list[str]:
    """Phase 385: the product factory's and the newer settings."""
    out = []
    try:
        from strategies.product_types import TYPES

        unknown = [t for t in (getattr(config, "factory_types", None) or []) if t not in TYPES]
        if unknown:
            out.append(f"factory_types: unknown type(s) {', '.join(map(str, unknown))}; known: {', '.join(TYPES)}")
    except Exception:  # noqa: BLE001 - the check must never stop doctor
        pass
    interval = int(getattr(config, "factory_interval_seconds", 600) or 0)
    if interval < 60:
        out.append(f"factory_interval_seconds = {interval}: the factory runs at most once a minute (60 is used)")
    if int(getattr(config, "factory_max_live", 500) or 0) < 10:
        out.append(f"factory_max_live = {config.factory_max_live}: fewer than 10 products on sale leaves the factory idle")
    if int(getattr(config, "factory_min_rows", 20) or 0) < 5:
        out.append(f"factory_min_rows = {config.factory_min_rows}: products under 5 rows aren't worth selling")
    webhook = str(getattr(config, "chat_webhook_url", "") or "")
    if webhook:
        from tools.chat import service

        if not service(webhook):
            out.append("chat_webhook_url isn't a Slack (hooks.slack.com/services/...) or Discord (discord.com/api/webhooks/...) "
                       "address; chat notifications are off")
    return out
