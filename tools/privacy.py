"""Privacy requests: show someone everything stored about them, or erase it.

``export(state, config, email)`` collects every record that mentions the address: orders, paid
and free subscriptions, checkout sessions, API keys (hashes only), sales-email drafts, the contact
log, testimonials, referral links and the email audit log.

``forget(state, config, email)`` erases it:

* **Deleted:** sales-email drafts, the contact log, testimonials, referral links, recovery tokens,
  free-sample signups, and the email audit log lines sent to them.
* **Anonymised, not deleted:** orders, paid subscriptions and checkout sessions keep the amounts,
  dates and products (the law requires keeping financial records), but the address becomes
  ``deleted-<hash>@invalid``. API keys are revoked.
* **Redacted:** the address is replaced with ``[deleted]`` in logs and in scraped job-posting data.
* **Kept:** a suppression entry, so the agent can never email them again. Keeping exactly that is
  allowed (and expected) under GDPR and CCPA.

Backups still hold the old data until they age out (``backup_keep_days``, 14 days).

The support desk spots "delete my data" style requests and alerts you; erasing is your decision,
via ``automonetize privacy forget <email>``. Answer within 30 days (GDPR).
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

REQUESTS = "privacy_requests"   # {email: {"at", "subject"}} waiting for you
PRIVACY_RE = re.compile(r"\b(delete|erase|remove|wipe)\b.{0,30}\b(my|all)\b.{0,20}\b(data|information|details|account|records)\b|"
                        r"\bgdpr\b|\bccpa\b|right to (be forgotten|erasure)|data (deletion|erasure|access) request|"
                        r"\b(what|which) (data|information) do you (have|hold|store)\b", re.I)


def placeholder(email: str) -> str:
    return f"deleted-{hashlib.sha256(email.encode()).hexdigest()[:12]}@invalid"


def export(state: Any, config: Any, email: str) -> dict[str, Any]:
    from tools import contact_policy as contact

    email = email.strip().lower()
    contact.ensure(state)
    one = (email,)
    from tools.dispatcher import AUDIT_FILE

    audit = []
    path = config.data_dir / AUDIT_FILE
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if str(rec.get("to", "")).lower() == email:
                audit.append({k: rec.get(k) for k in ("ts", "kind", "subject", "result")})
    return {
        "email": email,
        "orders": state._all("SELECT order_id, gross_cents, status, occurred_at, product_ref FROM orders WHERE email = ?", one),
        "subscriptions": state._all("SELECT tier, niche, price_cents, interval, subscription_status, started_at, canceled_at "
                                    "FROM subscribers WHERE lower(email) = ?", one),
        "checkout_sessions": state._all("SELECT session_id, status, amount_cents, updated_at FROM checkout_sessions "
                                        "WHERE lower(email) = ?", one),
        "api_keys": state._all("SELECT id, created_at FROM api_keys WHERE lower(email) = ?", one),
        "sales_emails": state._all("SELECT subject, status, created_at FROM outreach_queue WHERE lower(recipient) = ?", one),
        "emails_sent": audit,
        "contact_log": state._all("SELECT kind, sent_at FROM contact_log WHERE email = ?", one),
        "testimonials": [t for t in (state.get("testimonials") or []) if t.get("email") == email],
        "referral_link": any(v == email for v in (state.get("referral_tokens") or {}).values()),
        "suppressed": state.is_suppressed(email),
    }


def forget(state: Any, config: Any, email: str) -> dict[str, int]:
    from tools import contact_policy as contact
    from tools.dispatcher import AUDIT_FILE

    email = email.strip().lower()
    if "@" not in email:
        raise ValueError("not an email address")
    contact.ensure(state)
    anon = placeholder(email)
    done: dict[str, int] = {}

    def run(label: str, sql: str, args: tuple) -> None:
        done[label] = done.get(label, 0) + state._exec(sql, args).rowcount

    run("orders anonymised", "UPDATE orders SET email = ? WHERE lower(email) = ?", (anon, email))
    run("subscriptions anonymised", "UPDATE subscribers SET email = ?, token = NULL, customer_id = NULL "
                                    "WHERE lower(email) = ? AND tier = 'paid'", (anon, email))
    run("free signups deleted", "DELETE FROM subscribers WHERE lower(email) = ? AND tier = 'free'", (email,))
    run("checkout sessions anonymised", "UPDATE checkout_sessions SET email = ? WHERE lower(email) = ?", (anon, email))
    run("API keys revoked", "UPDATE api_keys SET email = ?, status = 'revoked', revoked_at = COALESCE(revoked_at, ?) WHERE lower(email) = ?",
        (anon, state.now(), email))
    run("sales emails deleted", "DELETE FROM outreach_queue WHERE lower(recipient) = ?", (email,))
    run("recovery tokens deleted", "DELETE FROM recovery_tokens WHERE lower(email) = ?", (email,))
    run("contact log deleted", "DELETE FROM contact_log WHERE email = ?", (email,))
    for table, column in (("errors", "message"), ("actions", "detail"), ("webhook_events", "detail"),
                          ("dunning_cases", "detail"), ("subscription_deliveries", "detail")):
        run("log lines redacted", f"UPDATE {table} SET {column} = replace({column}, ?, '[deleted]') "
                                  f"WHERE instr(lower({column}), ?) > 0", (email, email))
    rows = state._all("SELECT id, data FROM leads WHERE instr(lower(data), ?) > 0", (email,))
    for r in rows:
        state._exec("UPDATE leads SET data = ? WHERE id = ?", (re.sub(re.escape(email), "[deleted]", r["data"], flags=re.I), r["id"]))
    done["job postings redacted"] = len(rows)
    quotes = state.get("testimonials") or []
    state.set("testimonials", [t for t in quotes if t.get("email") != email])
    done["testimonials deleted"] = len(quotes) - len(state.get("testimonials") or [])
    tokens = state.get("referral_tokens") or {}
    state.set("referral_tokens", {k: v for k, v in tokens.items() if v != email})
    done["referral links deleted"] = len(tokens) - len(state.get("referral_tokens") or {})
    path = config.data_dir / AUDIT_FILE
    if path.exists():
        kept, dropped = [], 0
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                to = str(json.loads(line).get("to", "")).lower()
            except ValueError:
                to = ""
            if to == email:
                dropped += 1
            else:
                kept.append(line)
        path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        done["email log lines deleted"] = dropped
    state.suppress(email, "privacy request")
    requests = dict(state.get(REQUESTS) or {})
    requests.pop(email, None)
    state.set(REQUESTS, requests)
    state.log_action(int(state.get("iteration", 0)), None, "privacy:forget", "ok", f"erased {anon}")
    return {k: v for k, v in done.items() if v}


def note_request(state: Any, email: str, subject: str) -> bool:
    requests = dict(state.get(REQUESTS) or {})
    if email in requests:
        return False
    requests[email.lower()] = {"at": state.now(), "subject": subject[:120]}
    state.set(REQUESTS, requests)
    return True
