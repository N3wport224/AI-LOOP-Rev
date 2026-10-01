"""1-click ratings (Phases 101-102): how buyers feel, with one click, and what happens next.

The buyer follow-up email carries three links: 😀 great, 😐 okay, ☹ not good. Each opens a small
page on the agent's public server (``/r/<token>/<score>``) with one button that records it: a
POST, so the link scanners some mail providers run can't vote for the buyer. The token is random,
stored only as a hash, and rates one order once.

What happens next:

* **Not good** → you get an alert with the order, and it's on the to-do list: fix it before it
  turns into a refund request. The page says you'll be in touch and invites a reply.
* **Great** → the page invites a one-line reply with "OK to quote" (``strategies/testimonials.py``).
* Monday's report shows the counts (``summary``).

Table ``ratings``.
"""

from __future__ import annotations

import hashlib
import html
import secrets
from datetime import timedelta
from typing import Any

SCORES = {3: ("😀", "Great"), 2: ("😐", "Okay"), 1: ("☹️", "Not good")}
_SCHEMA = """CREATE TABLE IF NOT EXISTS ratings (
    token_hash TEXT PRIMARY KEY,
    order_pk INTEGER NOT NULL,
    email TEXT,
    score INTEGER,
    created_at TEXT NOT NULL,
    rated_at TEXT
)"""


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def ensure(state: Any) -> None:
    state._exec(_SCHEMA)


def links(state: Any, config: Any, order: dict[str, Any]) -> dict[int, str]:
    """Three rating links for one order ({} if there's no public URL)."""
    base = config.lead_capture_base
    if not base:
        return {}
    ensure(state)
    token = secrets.token_urlsafe(18)
    state._exec("INSERT INTO ratings (token_hash, order_pk, email, created_at) VALUES (?,?,?,?)",
                (_h(token), int(order["id"]), (order.get("email") or "").lower(), state.now()))
    return {score: f"{base}/r/{token}/{score}" for score in SCORES}


def lookup(state: Any, token: str) -> dict[str, Any] | None:
    ensure(state)
    if not token or len(token) > 64:
        return None
    return state._one("SELECT * FROM ratings WHERE token_hash = ?", (_h(token),))


def record(state: Any, token: str, score: int) -> str:
    """"ok", "already" or "invalid"."""
    row = lookup(state, token)
    if row is None or score not in SCORES:
        return "invalid"
    if row["score"] is not None:
        return "already"
    done = state._exec("UPDATE ratings SET score = ?, rated_at = ? WHERE token_hash = ? AND score IS NULL",
                       (score, state.now(), _h(token))).rowcount  # atomic: a double click records once
    if not done:
        return "already"
    if score == 1:
        order = state._one("SELECT * FROM orders WHERE id = ?", (row["order_pk"],)) or {}
        state.log_error("ratings", f"{row['email']} rated their purchase \"not good\" (order {order.get('order_id', row['order_pk'])}). "
                                   "Reach out before it becomes a refund request.", kind="alert")
    return "ok"


def unhappy(state: Any, days: int = 14) -> list[dict[str, Any]]:
    ensure(state)
    since = (state.clock() - timedelta(days=days)).isoformat(timespec="seconds")
    return state._all("SELECT * FROM ratings WHERE score = 1 AND rated_at >= ? ORDER BY rated_at DESC", (since,))


def summary(state: Any, days: int = 30) -> str:
    ensure(state)
    since = (state.clock() - timedelta(days=days)).isoformat(timespec="seconds")
    rows = state._all("SELECT score, COUNT(*) AS n FROM ratings WHERE score IS NOT NULL AND rated_at >= ? GROUP BY score", (since,))
    counts = {r["score"]: r["n"] for r in rows}
    if not counts:
        return ""
    return f"Ratings in the last {days} days: " + " · ".join(f"{SCORES[s][0]} {counts.get(s, 0)}" for s in (3, 2, 1))


def page(config: Any, title: str, body: str) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, '
            f'initial-scale=1"><meta name="robots" content="noindex"><title>{html.escape(title)}</title><style>body{{font:17px/1.5 '
            "-apple-system,sans-serif;max-width:560px;margin:3rem auto;padding:0 16px}}button{{font-size:1.1rem;padding:10px 18px;"
            f"border-radius:8px;border:0;background:#1d4ed8;color:#fff}}</style></head><body>{body}</body></html>")


def confirm_page(config: Any, token: str, score: int, row: dict[str, Any] | None) -> tuple[int, str]:
    if row is None or score not in SCORES:
        return 404, page(config, "Link not recognised", "<h1>Link not recognised</h1><p>This rating link isn't valid.</p>")
    if row["score"] is not None:
        return 200, page(config, "Thanks", "<h1>Thanks!</h1><p>Your rating is already in.</p>")
    emoji, label = SCORES[score]
    return 200, page(config, "Rate your purchase",
                     f"<h1>{emoji} {html.escape(label)}?</h1><form method=\"post\"><button type=\"submit\">Yes, send my rating"
                     "</button></form><p style=\"color:#666\">One click; nothing else is asked.</p>")


def thanks_page(config: Any, score: int, outcome: str) -> tuple[int, str]:
    if outcome == "invalid":
        return 404, page(config, "Link not recognised", "<h1>Link not recognised</h1><p>This rating link isn't valid.</p>")
    if outcome == "already":
        return 200, page(config, "Thanks", "<h1>Thanks!</h1><p>Your rating is already in.</p>")
    reply = html.escape(config.sender_email or "")
    if score == 3:
        body = ("<h1>Thank you! 😀</h1><p>If you have a minute, reply to the email with one line about how you use the data, and "
                "add \"OK to quote\" if it may appear on the product page as \"Verified buyer\".</p>")
    elif score == 1:
        body = ("<h1>Sorry about that.</h1><p>Reply to the email (or write to "
                f"<a href=\"mailto:{reply}\">{reply}</a>) with what went wrong, and it'll be put right.</p>")
    else:
        body = "<h1>Thanks!</h1><p>If there's one thing that would make it better, reply to the email and say so.</p>"
    return 200, page(config, "Thanks", body)
