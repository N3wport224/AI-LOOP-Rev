"""Personal data in the newer stores, and how long anything is kept (Phases 390-394).

``tools/privacy.py`` exports and erases a person's data across the original tables. The stores added
since keep personal data too, so:

* **Phase 390, export covers them:** ``automonetize privacy export`` now includes opt-out requests
  made with that address, "email me when it's ready" requests, and dataset requests they emailed.
* **Phase 391, erase covers them:** ``automonetize privacy forget`` anonymises their opt-out requests
  (the company's decision is kept, the address isn't), and deletes their "it's ready" requests and the
  address on their dataset requests.
* **Phase 392, kept no longer than needed:** daily, housekeeping anonymises decided opt-out requests
  after ``OPTOUT_KEEP_DAYS`` (365), drops saved job-board responses unused for ``CACHE_KEEP_DAYS`` (30)
  days, and forgets dead-posting marks for postings no longer stored.
* **Phase 393, an inventory:** ``automonetize privacy inventory`` lists every store that holds personal
  data, how many records it holds, and how long they're kept.
* **Phase 394, daily:** the pruning runs in housekeeping; the inventory is also in the control panel's
  Health tab data (``privacy_inventory`` in ``GET /api/health``).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

OPTOUT_KEEP_DAYS = 365
CACHE_KEEP_DAYS = 30


# ------------------------------------------------------------------ Phase 390
def export_extra(state: Any, email: str) -> dict[str, Any]:
    from strategies.buyer_experience import WATCH
    from strategies.customer_requests import KEY as REQUESTS
    from strategies.opt_out import REQUESTS as OPTOUTS

    email = email.strip().lower()
    return {"opt_out_requests": [{k: r.get(k) for k in ("company", "status", "at", "note")}
                                 for r in state.get(OPTOUTS) or [] if r.get("email") == email],
            "ready_notices": [{"wanted": w.get("wanted"), "at": w.get("at")} for w in state.get(WATCH) or [] if w.get("email") == email],
            "dataset_requests": [{"text": r.get("text"), "at": r.get("at")} for r in state.get(REQUESTS) or []
                                 if str(r.get("email") or "").lower() == email]}


# ------------------------------------------------------------------ Phase 391
def forget_extra(state: Any, email: str) -> dict[str, int]:
    from strategies.buyer_experience import WATCH
    from strategies.customer_requests import KEY as REQUESTS
    from strategies.opt_out import REQUESTS as OPTOUTS
    from tools.privacy import placeholder

    email = email.strip().lower()
    done = {}
    optouts = list(state.get(OPTOUTS) or [])
    n = 0
    for r in optouts:
        if r.get("email") == email:
            r["email"], n = placeholder(email), n + 1
    if n:
        state.set(OPTOUTS, optouts)
        done["opt-out requests anonymised"] = n
    watchers = list(state.get(WATCH) or [])
    kept = [w for w in watchers if w.get("email") != email]
    if len(kept) != len(watchers):
        state.set(WATCH, kept)
        done["ready notices deleted"] = len(watchers) - len(kept)
    requests = list(state.get(REQUESTS) or [])
    n = 0
    for r in requests:
        if str(r.get("email") or "").lower() == email:
            r["email"], n = "", n + 1
    if n:
        state.set(REQUESTS, requests)
        done["dataset requests anonymised"] = n
    return done


# ------------------------------------------------------------------ Phase 392
def prune(state: Any, files: Any) -> dict[str, int]:
    from strategies.opt_out import REQUESTS as OPTOUTS
    from strategies.posting_hygiene import DEAD
    from strategies.source_efficiency import CACHE_DIR, VALIDATORS
    from tools.privacy import placeholder

    now = state.clock()
    out = {}
    cutoff = (now - timedelta(days=OPTOUT_KEEP_DAYS)).isoformat(timespec="seconds")
    optouts = list(state.get(OPTOUTS) or [])
    n = 0
    for r in optouts:
        decided = r.get("status") != "pending" and str(r.get("decided_at") or r.get("at") or "") < cutoff
        if decided and not str(r.get("email") or "").endswith("@invalid"):
            r["email"], n = placeholder(str(r.get("email") or "")), n + 1
    if n:
        state.set(OPTOUTS, optouts)
        out["old opt-out addresses"] = n
    saved = dict(state.get(VALIDATORS) or {})
    stale_cutoff = (now - timedelta(days=CACHE_KEEP_DAYS)).isoformat(timespec="seconds")
    gone = [k for k, v in saved.items() if str(v.get("used_at") or "") < stale_cutoff]
    for k in gone:
        saved.pop(k)
        path = f"{CACHE_DIR}/{k}.bin"
        if files.exists(path):
            files.resolve(path).unlink(missing_ok=True)
    if gone:
        state.set(VALIDATORS, saved)
        out["unused saved responses"] = len(gone)
    dead = list(state.get(DEAD) or [])
    if dead:
        present = {r["dedupe_key"] for r in state._all("SELECT DISTINCT dedupe_key FROM leads")}
        kept = [k for k in dead if k in present]
        if len(kept) != len(dead):
            state.set(DEAD, kept)
            out["dead-posting marks"] = len(dead) - len(kept)
    return out


# ------------------------------------------------------------------ Phase 393
def inventory(state: Any, cfg: Any) -> list[dict[str, Any]]:
    from strategies.buyer_experience import WATCH
    from strategies.customer_requests import KEY as REQUESTS
    from strategies.opt_out import REQUESTS as OPTOUTS

    def count(sql: str) -> int:
        try:
            return int(state._one(sql)["n"])
        except Exception:  # noqa: BLE001 - a table that doesn't exist yet holds nothing
            return 0

    return [
        {"store": "Orders (buyer email)", "records": count("SELECT COUNT(*) AS n FROM orders WHERE email IS NOT NULL AND email "
                                                           "NOT LIKE '%@invalid'"),
         "kept": "for your records (tax law); anonymised on a privacy request"},
        {"store": "Subscribers (paid and free)", "records": count("SELECT COUNT(*) AS n FROM subscribers WHERE email IS NOT NULL"),
         "kept": f"while subscribed; unconfirmed sign-ups {cfg.unconfirmed_signup_days} days"},
        {"store": "Checkout sessions", "records": count("SELECT COUNT(*) AS n FROM checkout_sessions WHERE email IS NOT NULL"),
         "kept": "with the order they belong to"},
        {"store": "Download links", "records": count("SELECT COUNT(*) AS n FROM download_tokens"),
         "kept": f"{cfg.download_link_days} days, then pruned"},
        {"store": "Contact log", "records": count("SELECT COUNT(*) AS n FROM contact_log"), "kept": "pruned by housekeeping"},
        {"store": "Opt-out requests", "records": len(state.get(OPTOUTS) or []),
         "kept": f"address anonymised {OPTOUT_KEEP_DAYS} days after the decision"},
        {"store": "\"It's ready\" requests", "records": len(state.get(WATCH) or []), "kept": "until the email is sent, at most 90 days"},
        {"store": "Dataset requests", "records": sum(1 for r in state.get(REQUESTS) or [] if r.get("email")),
         "kept": "the 200 most recent"},
        {"store": "Suppression list", "records": count("SELECT COUNT(*) AS n FROM suppression"),
         "kept": "always (so nobody who opted out is emailed again)"},
    ]


def describe(rows: list[dict[str, Any]]) -> list[str]:
    return [f"{r['store']}: {r['records']} ({r['kept']})" for r in rows]

