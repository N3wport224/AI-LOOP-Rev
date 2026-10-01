"""Contact policy: one place that decides whether a person may get a marketing email now.

Every non-transactional email the agent sends (buyer follow-ups, new-release, refresh, win-back,
bundle-upgrade, free-sample and sale emails) asks ``blocked()`` first and calls ``record()`` after
sending. Purchased files, receipts, support replies and referral rewards are not marketing and
don't go through here.

Rules, in order:

1. **Suppressed** addresses (unsubscribed, bounced, privacy request) never get one.
2. **Paused**: while the bounce guard (``strategies/bounce_guard.py``) has paused promotions, or
   you've switched on quiet mode (``automonetize quiet on``, the panel, or an email command), no
   marketing email goes out at all; approved sales emails wait too.
3. **Per person**: at most one *promotional* email per ``announce_min_gap_days`` (14), across all
   offer types. A follow-up after a purchase doesn't count against it.
4. **Per day**: at most ``promo_daily_cap`` (150) marketing emails in total, well under consumer
   mailbox limits (Gmail: about 500 a day), so the sender address stays healthy.

The log (table ``contact_log``) also feeds the offer tuner (``strategies/offer_tuner.py``), which
compares sends with sales per offer type.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

PROMO = {"release", "refresh", "winback", "upgrade", "sample", "sale"}
RELATIONSHIP = {"followup"}
PAUSE_KEY = "promo_pause"
LEGACY_LAST_SENT = "release_last_sent"  # per-person dates written before the contact log existed

_SCHEMA = """CREATE TABLE IF NOT EXISTS contact_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    kind TEXT NOT NULL,
    ref TEXT,
    sent_at TEXT NOT NULL
)"""


def ensure(state: Any) -> None:
    if not getattr(state, "_contact_log_ready", False):
        state._exec(_SCHEMA)
        state._exec("CREATE INDEX IF NOT EXISTS idx_contact_log_email ON contact_log (email, sent_at)")
        state._exec("CREATE INDEX IF NOT EXISTS idx_contact_log_kind ON contact_log (kind, sent_at)")
        state._contact_log_ready = True


QUIET_KEY = "quiet_mode"


def quiet(state: Any) -> dict[str, Any] | None:
    """Quiet mode (Phase 62): you switched marketing email off; purchases and support still go out."""
    q = state.get(QUIET_KEY)
    if q and (not q.get("until") or datetime.fromisoformat(q["until"]) > state.clock()):
        return q
    return None


def set_quiet(state: Any, on: bool, reason: str = "", days: float | None = None) -> None:
    if on:
        until = (state.clock() + timedelta(days=days)).isoformat(timespec="seconds") if days else None
        state.set(QUIET_KEY, {"since": state.now(), "until": until, "reason": reason or "quiet mode"})
    else:
        state.set(QUIET_KEY, None)
    state.log_action(int(state.get("iteration", 0)), None, "quiet_mode", "ok", ("on: " + (reason or "")) if on else "off")


def paused(state: Any) -> dict[str, Any] | None:
    """Marketing email held: the bounce guard's pause, or quiet mode."""
    pause = state.get(PAUSE_KEY)
    if pause and datetime.fromisoformat(pause["until"]) > state.clock():
        return pause
    q = quiet(state)
    if q:
        return {"until": q.get("until") or "you turn it off", "reason": f"quiet mode, {q.get('reason') or 'on'}"}
    return None


def last_promo(state: Any, email: str) -> str | None:
    ensure(state)
    marks = ",".join("?" * len(PROMO))
    row = state._one(f"SELECT MAX(sent_at) AS t FROM contact_log WHERE email = ? AND kind IN ({marks})", (email, *sorted(PROMO)))
    legacy = (state.get(LEGACY_LAST_SENT) or {}).get(email)
    return max([t for t in ((row or {}).get("t"), legacy) if t], default=None)


def sent_today(state: Any) -> int:
    ensure(state)
    start = state.clock().replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
    return int(state._one("SELECT COUNT(*) AS n FROM contact_log WHERE sent_at >= ? AND kind != 'bounce'", (start,))["n"])


def blocked(state: Any, cfg: Any, email: str, kind: str) -> str | None:
    """Why ``email`` may not get a ``kind`` email now, or None."""
    email = email.strip().lower()
    if state.is_suppressed(email):
        return "suppressed"
    pause = paused(state)
    if pause:
        return f"marketing email paused until {pause['until'][:16]} ({pause['reason']})"
    if kind in PROMO:
        last = last_promo(state, email)
        if last and state.clock() - datetime.fromisoformat(last) < timedelta(days=float(cfg.announce_min_gap_days)):
            return f"had an offer on {last[:10]}"
    if sent_today(state) >= int(cfg.promo_daily_cap):
        return "daily marketing cap reached"
    return None


def record(state: Any, email: str, kind: str, ref: str = "") -> None:
    ensure(state)
    state._exec("INSERT INTO contact_log (email, kind, ref, sent_at) VALUES (?,?,?,?)",
                (email.strip().lower(), kind, ref, state.now()))


def sends(state: Any, kind: str, since: str) -> int:
    """``kind`` emails sent after ``since`` (strictly: a change made at ``since`` starts a fresh count)."""
    ensure(state)
    return int(state._one("SELECT COUNT(*) AS n FROM contact_log WHERE kind = ? AND sent_at > ?", (kind, since))["n"])
