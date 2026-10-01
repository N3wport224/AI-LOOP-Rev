"""How current each factory product is (Phases 375-379).

* **Phase 375, measured at build:** every new version records how old its postings are: the newest
  posting date and the median posting age when it was built (``measure``, kept in ``listing.json``).
* **Phase 376, on the product page:** "Built 2026-10-01 from postings up to 2026-09-30; half are
  from the last 6 days."
* **Phase 377, stale factory products:** the freshness guard now covers factory products too. One
  whose weekly refresh hasn't succeeded for ``REFRESH_STALE_DAYS`` (14) days, or whose newest posting
  is over ``NEWEST_STALE_DAYS`` (30) days old, is marked stale: not promoted, one alert, still on
  sale. The mark lifts when a refresh brings it up to date.
* **Phase 378, in search:** the site's search index carries each product's newest posting date (``d``).
* **Phase 379, in the catalog spreadsheet:** ``newest_posting`` and ``median_posting_age_days`` columns.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

PRIMARY = ("leads.csv", "companies.csv", "salaries.csv")
REFRESH_STALE_DAYS = 14
NEWEST_STALE_DAYS = 30


def _date(raw: Any) -> datetime | None:
    s = str(raw or "")[:25]
    if not s:
        return None
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            t = datetime.fromisoformat(s[:10])
        except ValueError:
            return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


# ------------------------------------------------------------------ Phase 375
def measure(files: dict[str, Any], now: datetime) -> dict[str, Any]:
    for name in PRIMARY:
        body = files.get(name)
        if body is None:
            continue
        text = body.decode("utf-8-sig") if isinstance(body, bytes) else str(body)
        dates = [d for d in (_date(r.get("posted_at") or r.get("latest_posting")) for r in csv.DictReader(io.StringIO(text))) if d]
        if not dates:
            return {}
        ages = [max(0.0, (now - d).total_seconds() / 86400) for d in dates]
        return {"newest": max(dates).date().isoformat(), "median_days": round(median(ages), 1), "built": now.date().isoformat()}
    return {}


# ------------------------------------------------------------------ Phase 376
def page_line(listing: dict[str, Any]) -> str:
    f = listing.get("freshness") or {}
    if not f.get("newest"):
        return ""
    return (f'<p class="muted">Built {f["built"]} from postings up to {f["newest"]}; half are from the last '
            f'{max(1, round(f["median_days"]))} days.</p>')


# ------------------------------------------------------------------ Phase 377
def stale_reason(state: Any, slug: str, listing: dict[str, Any]) -> str:
    row = state._one("SELECT published_at, refreshed_at, newest_posting FROM factory_products WHERE slug = ? AND status = 'live'",
                     (slug,))
    if not row:
        return ""
    now = state.clock()
    last = row["refreshed_at"] or row["published_at"]
    if last and not str(last).startswith("1970") and now - datetime.fromisoformat(last) > timedelta(days=REFRESH_STALE_DAYS):
        return f"not refreshed since {str(last)[:10]}"
    newest = _date(row["newest_posting"] or (listing.get("freshness") or {}).get("newest"))
    if newest and now - newest > timedelta(days=NEWEST_STALE_DAYS):
        return f"newest posting is from {newest.date().isoformat()}"
    return ""


def listing_for(files: Any, slug: str) -> dict[str, Any]:
    rel = f"assets/{slug}/micro-v1/listing.json"
    return files.read_json(rel) if files.exists(rel) else {}
