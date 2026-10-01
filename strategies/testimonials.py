"""Testimonials: real customer quotes on the product pages, only with their consent and your OK.

1. **Ask.** The buyer follow-up email invites a one-line reply about how they use the data,
   with "OK to quote" if they're happy for it to appear (anonymously) on the product page.
2. **Collect.** The support desk (``strategies/support_desk.py``) reads customers' replies. A reply
   that contains an explicit consent phrase ("OK to quote", "you can quote me") becomes a pending
   testimonial: the customer's own words, with email addresses, links and phone numbers removed.
   Nothing without that phrase is ever used.
3. **Approve.** Pending quotes wait for you in the control panel (**Share** tab) and on the to-do
   list. Only approved quotes are published.
4. **Show.** Each product page shows up to three approved quotes for its niche, attributed to
   "Verified buyer" (never a name or address).

State: kv ``testimonials`` = list of {id, email, niche, text, status, at}.
"""

from __future__ import annotations

import re
from typing import Any

KEY = "testimonials"
CONSENT_RE = re.compile(r"\b(ok(ay)?|fine|happy|feel free|free) to quote( me)?\b|\byou (can|may) (quote|use) (me|this|it)\b", re.I)
SCRUB = [re.compile(p, re.I) for p in (r"\S+@\S+", r"https?://\S+", r"\bwww\.\S+", r"\+?\d[\d\s().-]{7,}\d")]
MIN_LEN, MAX_LEN = 15, 300
SHOWN = 3


def extract(subject: str, body: str) -> str | None:
    """The quotable sentence(s) of a consenting reply, or None."""
    from strategies.support_desk import fresh_text

    text = fresh_text("", body)
    if not CONSENT_RE.search(text):
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    lines = [ln for ln in lines if not re.match(r"^(hi|hello|hey|thanks|thank you|cheers|best|regards)\b[ ,!.]*\S{0,20}$", ln, re.I)
             and not ln.startswith("--")]
    # Whole sentences go: the consent itself and anything carrying contact details.
    sentences = re.split(r"(?<=[.!?])\s+", " ".join(lines))
    kept = [x for x in sentences if not CONSENT_RE.search(x) and not any(p.search(x) for p in SCRUB)]
    quote = re.sub(r"\s+", " ", " ".join(kept)).strip(" .,;:-–—()[]\"'")
    if len(quote) < MIN_LEN:
        return None
    if len(quote) > MAX_LEN:
        quote = quote[:MAX_LEN].rsplit(" ", 1)[0] + "…"
    return quote


def add_pending(state: Any, email: str, niche: str, text: str) -> int:
    items = list(state.get(KEY) or [])
    if any(i["email"] == email.lower() and i["text"] == text for i in items):
        return 0
    new_id = max([int(i["id"]) for i in items] or [0]) + 1
    items.append({"id": new_id, "email": email.lower(), "niche": niche, "text": text, "status": "pending", "at": state.now()})
    state.set(KEY, items)
    return new_id


def listing(state: Any, status: str | None = None) -> list[dict[str, Any]]:
    return [i for i in (state.get(KEY) or []) if status is None or i["status"] == status]


def set_status(state: Any, ids: list[int], status: str) -> int:
    if status not in ("approved", "rejected"):
        raise ValueError("status must be approved or rejected")
    items = list(state.get(KEY) or [])
    changed = 0
    for i in items:
        if i["id"] in ids and i["status"] == "pending":
            i["status"] = status
            changed += 1
    state.set(KEY, items)
    return changed


def approved_for(state: Any, niche: str) -> list[str]:
    quotes = [i for i in listing(state, "approved") if i["niche"] == niche]
    return [i["text"] for i in sorted(quotes, key=lambda i: i["at"], reverse=True)[:SHOWN]]


def niche_for_customer(state: Any, email: str) -> str:
    from strategies.freshness_guard import niche_of

    row = state._one("SELECT asset_id FROM orders WHERE email = ? AND asset_id IS NOT NULL ORDER BY id DESC LIMIT 1", (email.lower(),))
    asset = state.get_asset(row["asset_id"]) if row else None
    if asset:
        return niche_of(state, asset)
    sub = state._one("SELECT niche FROM subscribers WHERE email = ? AND tier = 'paid' AND niche IS NOT NULL ORDER BY id DESC LIMIT 1",
                     (email.lower(),))
    return str((sub or {}).get("niche") or "")
