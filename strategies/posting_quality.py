"""Posting quality (Phases 230-234): cleaner rows, truer counts.

* **Phase 230, still listed:** a posting the boards stopped listing ``STALE_DAYS`` (21) days before
  the newest one seen is left out of new products and refreshes: it's most likely filled. Measured
  against the newest posting rather than today, so an agent that was switched off for a month
  doesn't throw its data away.
* **Phase 231, one company, one name:** "Acme, Inc.", "ACME Inc" and "Acme GmbH" count as one
  company (``company_key``) in company counts and company lists; the list shows the most common
  spelling.
* **Phase 232, employers, not agencies:** staffing and recruitment agencies (by name,
  ``AGENCY``) are left out of the company lists (top companies, remote-first employers,
  fastest-hiring), which buyers use to reach employers directly. Their postings stay in the
  posting-level datasets, where they're real openings.
* **Phase 233, where the rows come from:** each posting-level dataset's README names its job
  boards and each one's share of the rows.
* **Phase 234, how current it is:** that README also says how many of its postings were seen on
  their board in the last 7 days.
"""

from __future__ import annotations

import re
from collections import Counter
from functools import lru_cache
from datetime import datetime, timedelta, timezone
from typing import Any

STALE_DAYS = 21
LISTED_DAYS = 7
SUFFIXES = {"inc", "incorporated", "llc", "ltd", "limited", "gmbh", "corp", "corporation", "co", "company", "plc", "sa",
            "ag", "bv", "srl", "oy", "ab", "pty", "sas", "sarl", "llp", "lp", "kg", "se", "as", "aps", "nv", "spa"}
AGENCY = re.compile(r"\b(?:staffing|recruit(?:ment|ing|ers)?|talent (?:acquisition|solutions|partners)|personnel|"
                    r"employment agency)\b|headhunt", re.I)


def _when(raw: Any) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(raw or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


# ------------------------------------------------------------------ Phase 230
def drop_unlisted(leads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = [t for t in (_when(lead.get("last_seen")) for lead in leads) if t]
    if not seen:
        return leads
    cutoff = max(seen) - timedelta(days=STALE_DAYS)
    return [lead for lead in leads if (_when(lead.get("last_seen")) or cutoff) >= cutoff]


# ------------------------------------------------------------------ Phase 231
def company_key(name: Any) -> str:
    return _company_key(str(name or ""))


@lru_cache(maxsize=100_000)
def _company_key(name: str) -> str:
    words = re.sub(r"[^\w\s&]", " ", str(name or "").lower()).split()
    while len(words) > 1 and words[-1] in SUFFIXES:
        words.pop()
    return " ".join(words)


# ------------------------------------------------------------------ Phase 232
def is_agency(name: Any) -> bool:
    return bool(AGENCY.search(str(name or "")))


# ------------------------------------------------------------------ Phases 233-234
def sources_line(rows: list[dict[str, Any]]) -> str:
    from strategies.job_sources import CREDITS

    counts = Counter(str(r.get("source") or "") for r in rows if r.get("source"))
    if not counts:
        return ""
    total = sum(counts.values())
    parts = [f"{CREDITS.get(s, (s, ''))[0]} {round(100 * n / total)}%" for s, n in counts.most_common()]
    return "Job boards: " + ", ".join(parts) + "."


def listed_line(rows: list[dict[str, Any]], now: datetime) -> str:
    stamps = [_when(r.get("last_seen")) for r in rows]
    known = [t for t in stamps if t]
    if not known:
        return ""
    recent = sum(1 for t in known if now - t <= timedelta(days=LISTED_DAYS))
    return f"{recent} of {len(rows)} postings were seen on their job board in the last {LISTED_DAYS} days."
