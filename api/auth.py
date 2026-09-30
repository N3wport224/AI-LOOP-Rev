"""API keys: issue, verify, rotate, degrade and revoke.

* A key is ``am_live_`` + 48 hex characters (192 bits from ``secrets``). Only its SHA-256 is
  stored, so a copy of the database can't be used to call the API. A plain hash is enough here
  (unlike passwords) because the key is random and long: there's nothing to brute-force.
* The plaintext exists twice: in the welcome email and in the ``/v1/auth/rotate`` response.
  Lost keys are reissued by the operator (``automonetize api reissue``), which emails a new
  one and retires the old.
* Status follows the Stripe subscription:

  ==========================  ==========  ==============================================
  Stripe                      key status  effect
  ==========================  ==========  ==============================================
  active / trialing           active      full quota (``api_daily_quota``)
  invoice.payment_failed,     degraded    ``api_degraded_quota`` a day while Stripe
  past_due                                retries the card; restored when it's paid
  canceled / unpaid /         revoked     401 ``key_revoked`` from the next request
  incomplete_expired
  ==========================  ==========  ==============================================
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timezone
from typing import Any

KEY_PREFIX = "am_live_"
KEY_RE = re.compile(r"^am_live_[0-9a-f]{48}$")
API_KIND = "api_subscription"
DEFAULT_PERMISSIONS = ["signals:read", "companies:read"]
LIVE = ("active", "degraded")

STRIPE_TO_KEY = {
    "active": "active", "trialing": "active",
    "past_due": "degraded", "incomplete": "degraded",
    "canceled": "revoked", "unpaid": "revoked", "incomplete_expired": "revoked", "paused": "revoked",
}


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_hex(24)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def display_prefix(key: str) -> str:
    return key[: len(KEY_PREFIX) + 6]


def utc_day(now: datetime) -> str:
    return now.astimezone(timezone.utc).date().isoformat()


class ApiKeys:
    def __init__(self, state: Any, config: Any):
        self.state = state
        self.config = config

    def quota_for(self, status: str) -> int:
        return self.config.api_degraded_quota if status == "degraded" else self.config.api_daily_quota

    # -- issue / lookup ---------------------------------------------------------------
    def issue(self, subscriber_id: int | None, email: str | None, status: str = "active", plan: str = "developer",
              rotated_from: int | None = None) -> tuple[str, dict[str, Any]]:
        key = generate_key()
        now = self.state.now()
        cur = self.state._exec(
            "INSERT INTO api_keys (key_hash, prefix, subscriber_id, email, status, plan, daily_quota, permissions, "
            "created_at, updated_at, rotated_from) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (hash_key(key), display_prefix(key), subscriber_id, (email or "").lower() or None, status, plan,
             self.quota_for(status), json.dumps(DEFAULT_PERMISSIONS), now, now, rotated_from),
        )
        return key, self.get(int(cur.lastrowid))

    @staticmethod
    def _row(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        row = dict(row)
        row["permissions"] = json.loads(row.get("permissions") or "[]")
        return row

    def get(self, key_id: int) -> dict[str, Any] | None:
        return self._row(self.state._one("SELECT * FROM api_keys WHERE id = ?", (key_id,)))

    def lookup(self, key: str | None) -> dict[str, Any] | None:
        if not key or not KEY_RE.match(key):
            return None
        return self._row(self.state._one("SELECT * FROM api_keys WHERE key_hash = ?", (hash_key(key),)))

    def for_subscriber(self, subscriber_id: int, statuses: tuple[str, ...] = LIVE) -> list[dict[str, Any]]:
        marks = ",".join("?" * len(statuses))
        rows = self.state._all(f"SELECT * FROM api_keys WHERE subscriber_id = ? AND status IN ({marks}) ORDER BY id",
                               (subscriber_id, *statuses))
        return [self._row(r) for r in rows]  # type: ignore[misc]

    def list(self, statuses: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        if statuses:
            marks = ",".join("?" * len(statuses))
            rows = self.state._all(f"SELECT * FROM api_keys WHERE status IN ({marks}) ORDER BY id", statuses)
        else:
            rows = self.state._all("SELECT * FROM api_keys ORDER BY id")
        return [self._row(r) for r in rows]  # type: ignore[misc]

    # -- lifecycle ------------------------------------------------------------------------
    def set_status(self, key_id: int, status: str, reason: str = "") -> None:
        now = self.state.now()
        self.state._exec(
            "UPDATE api_keys SET status = ?, daily_quota = ?, updated_at = ?, status_reason = ?, "
            "revoked_at = CASE WHEN ? IN ('revoked', 'rotated') THEN COALESCE(revoked_at, ?) ELSE NULL END WHERE id = ?",
            (status, self.quota_for(status), now, reason[:200], status, now, key_id),
        )

    def sync_subscriber(self, subscriber_id: int, status: str, reason: str = "") -> int:
        """Move every live key of a subscriber to ``status``. Returns how many changed."""
        changed = 0
        for row in self.for_subscriber(subscriber_id):
            if row["status"] != status:
                self.set_status(row["id"], status, reason)
                changed += 1
        return changed

    def rotate(self, row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """New key with the same subscriber, plan and status; the old one stops working now."""
        key, new = self.issue(row["subscriber_id"], row["email"], row["status"] if row["status"] in LIVE else "active",
                              row["plan"], rotated_from=row["id"])
        self.set_status(row["id"], "rotated", f"rotated to key {new['id']}")
        return key, new

    # -- metering -----------------------------------------------------------------------------
    def consume(self, row: dict[str, Any], endpoint: str, now: datetime) -> tuple[bool, int]:
        """Count one request against today's quota, atomically. Returns (allowed, used_after)."""
        day = utc_day(now)
        with self.state.tx() as conn:
            used = conn.execute("SELECT COALESCE(SUM(requests), 0) FROM api_usage WHERE key_id = ? AND day = ?",
                                (row["id"], day)).fetchone()[0]
            if used >= row["daily_quota"]:
                return False, int(used)
            conn.execute(
                "INSERT INTO api_usage (key_id, day, endpoint, requests) VALUES (?,?,?,1) "
                "ON CONFLICT(key_id, day, endpoint) DO UPDATE SET requests = requests + 1",
                (row["id"], day, endpoint),
            )
            conn.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (now.isoformat(timespec="seconds"), row["id"]))
            return True, int(used) + 1

    def used_today(self, key_id: int, now: datetime) -> int:
        row = self.state._one("SELECT COALESCE(SUM(requests), 0) AS n FROM api_usage WHERE key_id = ? AND day = ?",
                              (key_id, utc_day(now)))
        return int(row["n"]) if row else 0

    def volume(self, days: int, now: datetime) -> list[dict[str, Any]]:
        """Requests per day (oldest first) for the dashboards."""
        from datetime import timedelta

        start = (now.astimezone(timezone.utc) - timedelta(days=days - 1)).date().isoformat()
        rows = self.state._all("SELECT day, SUM(requests) AS n FROM api_usage WHERE day >= ? GROUP BY day ORDER BY day", (start,))
        return [{"day": r["day"], "requests": int(r["n"])} for r in rows]

    def by_endpoint(self, now: datetime) -> dict[str, int]:
        rows = self.state._all("SELECT endpoint, SUM(requests) AS n FROM api_usage WHERE day = ? GROUP BY endpoint",
                               (utc_day(now),))
        return {r["endpoint"]: int(r["n"]) for r in rows}


# ----------------------------------------------------------------------------- Stripe integration
def is_api_subscriber(state: Any, sub: dict[str, Any] | None) -> bool:
    if not sub or not sub.get("asset_id"):
        return False
    asset = state.get_asset(sub["asset_id"])
    return bool(asset and asset["kind"] == API_KIND)


def api_asset(state: Any) -> dict[str, Any] | None:
    return next((a for a in state.list_assets() if a["kind"] == API_KIND and a.get("checkout_url")), None)


def provision(tools: Any, subscriber_id: int) -> str:
    """Issue a key for a newly active API subscriber (once) and email it. Idempotent: returns
    ``exists`` when the subscriber already has a live key."""
    state = tools.state
    sub = state.subscriber(subscriber_id)
    if not sub or sub.get("subscription_status") not in ("active", "trialing"):
        return "not active"
    keys = ApiKeys(state, tools.config)
    if keys.for_subscriber(subscriber_id):
        return "exists"
    key, row = keys.issue(subscriber_id, sub.get("email"))
    state.log_action(int(state.get("iteration", 0)), None, "api:provision", "ok",
                     f"key {row['prefix']}… for subscriber {subscriber_id}")
    return send_welcome(tools, sub, key, row)


def send_welcome(tools: Any, sub: dict[str, Any], key: str, row: dict[str, Any], rotated: bool = False) -> str:
    from tools.dispatcher import Email

    cfg = tools.config
    base = cfg.lead_capture_base or "https://<your API host>"
    if not sub.get("email"):
        tools.state.log_error("api", f"subscriber {sub['id']} has no email: key {row['prefix']}… can't be delivered")
        return "no email"
    heading = "Your new API key" if rotated else "Welcome to the Developer API"
    body = f"""{heading}

API key (keep it secret; it is shown only here):

    {key}

Quickstart:

    curl -H "Authorization: Bearer {key}" "{base}/v1/signals?tech=kubernetes&min_urgency=60&limit=5"

    curl -H "Authorization: Bearer {key}" "{base}/v1/companies/example.com"

Your plan: {row['daily_quota']} requests a day, {cfg.api_burst} per burst. Every response carries
X-RateLimit-Remaining; a 429 tells you when to retry (Retry-After).

Documentation: {base}/docs/api    OpenAPI 3.1: {base}/openapi.json

Rotate the key any time (the old one stops working immediately):

    curl -X POST -H "Authorization: Bearer {key}" "{base}/v1/auth/rotate"

Manage or cancel the subscription from your Stripe receipt email.

{cfg.sender_name or 'AutoMonetize'}
"""
    email = Email(to=sub["email"], subject=heading + ": AutoMonetize hiring-signal API", body=body, kind="delivery")
    try:
        outcome = tools.dispatcher.send_transactional(email, audit_key=f"api:{row['id']}:welcome")
    except Exception as exc:  # noqa: BLE001 - the operator can reissue; the key itself is valid
        tools.state.log_error("api", f"welcome for key {row['prefix']}… failed: {exc!r}")
        return "failed"
    if outcome == "dry_run":
        # The plaintext only lives in this email; in dry-run nobody receives it. Say so plainly.
        tools.state.log_error("api", f"DRY_RUN: key {row['prefix']}… for subscriber {sub['id']} was not emailed. "
                                     f"After going live run `automonetize api reissue {sub['id']}`.", kind="alert")
    return outcome


def sync_from_stripe_status(tools: Any, subscriber_id: int, stripe_status: str | None) -> str | None:
    """Apply a Stripe subscription status to the subscriber's keys; provision on first activation."""
    target = STRIPE_TO_KEY.get(stripe_status or "")
    if target is None:
        return None
    keys = ApiKeys(tools.state, tools.config)
    if target == "active" and not keys.for_subscriber(subscriber_id):
        provision(tools, subscriber_id)
        return "provisioned"
    changed = keys.sync_subscriber(subscriber_id, target, f"stripe: {stripe_status}")
    return target if changed else None


def payment_failed(tools: Any, subscription_id: str, invoice_id: str = "") -> int:
    """``invoice.payment_failed``: degrade now, don't wait for Stripe to mark it past_due."""
    sub = tools.state.get_subscriber(subscription_id)
    if not sub or not is_api_subscriber(tools.state, sub):
        return 0
    return ApiKeys(tools.state, tools.config).sync_subscriber(sub["id"], "degraded", f"invoice {invoice_id} payment failed")
