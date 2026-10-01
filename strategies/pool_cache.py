"""Read the posting pool once (Phase 365).

Several parts of a factory step read every pooled posting (candidates, new employers, trends,
comparisons, link checks). Decoding thousands of JSON rows each time adds up, so the decoded pool
is kept in memory and reused until the pool changes. A cheap signature (row count, newest row,
newest sighting, total size of the data) tells when it has.

Phase 366 lives next to it: a posting's facets (technologies, regions, level) are worked out once per
distinct (stack, location, remote, seniority) rather than once per use.
"""

from __future__ import annotations

import threading
from functools import lru_cache
from typing import Any

_CACHE: dict[str, tuple[tuple[Any, ...], list[dict[str, Any]]]] = {}
_LOCK = threading.Lock()


def pool(state: Any) -> list[dict[str, Any]]:
    from strategies.b2b_lead_aggregator import POOL_NICHE

    row = state._one("SELECT COUNT(*) AS n, MAX(id) AS mx, MAX(last_seen) AS ls, SUM(LENGTH(data)) AS sz FROM leads "
                     "WHERE niche = ?", (POOL_NICHE,))
    sig = (row["n"], row["mx"], row["ls"], row["sz"])
    key = str(getattr(state, "db_path", id(state)))
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and hit[0] == sig:
            return list(hit[1])
    leads = state.leads_for_niche(POOL_NICHE)
    with _LOCK:
        if len(_CACHE) > 8:
            _CACHE.clear()
        _CACHE[key] = (sig, leads)
    return list(leads)


def clear() -> None:
    with _LOCK:
        _CACHE.clear()


# ------------------------------------------------------------------ Phase 366
@lru_cache(maxsize=50_000)
def _facets(stack: tuple[str, ...], location: str, remote: str, seniority: str) -> dict[str, Any]:
    from strategies.b2b_lead_aggregator import TECH_KEYWORDS
    from strategies.dataset_extras import region_of

    return {"techs": frozenset(t for t in (s.lower() for s in stack) if t in TECH_KEYWORDS),
            "regions": frozenset(region_of({"location": location, "remote": remote})), "level": seniority}


def facets(lead: dict[str, Any]) -> dict[str, Any]:
    stack = lead.get("stack") or []
    return _facets(tuple(str(t) for t in stack) if isinstance(stack, (list, tuple)) else (), str(lead.get("location") or ""),
                   str(lead.get("remote")), str(lead.get("seniority") or ""))
