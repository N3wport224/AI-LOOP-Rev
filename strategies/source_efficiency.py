"""Gentler, smarter fetching (Phases 295-299).

* **Phase 295, only download what changed:** job-board requests remember each response's ``ETag``
  and ``Last-Modified`` and ask "has this changed?" next time. A ``304 Not Modified`` answer reuses
  the saved copy (``data/cache/sources``): less traffic for the boards, and for you.
* **Phase 296, how long each board takes:** every fetch is timed (kv ``source_timings``: last and
  average milliseconds, runs), shown by ``automonetize sources``.
* **Phase 297, back off when a board fails:** after two failures in a row a board waits 2, 4, 8... up
  to 24 hours before the next try, instead of every cycle.
* **Phase 298, spread out:** boards on a fixed cadence get a small fixed offset each (up to ±10%), so
  they aren't all fetched in the same cycle.
* **Phase 299, what each board brings:** ``automonetize sources`` and Monday's report show, for the
  last 30 days, new postings per board and how many of them no other board had.
"""

from __future__ import annotations

import hashlib
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any

VALIDATORS = "http_validators"
TIMINGS = "source_timings"
FAILURES = "source_failures"
CACHE_DIR = "cache/sources"
MAX_BACKOFF_HOURS = 24
JITTER = 0.10


def _header(headers: dict[str, str], name: str) -> str:
    return next((v for k, v in (headers or {}).items() if k.lower() == name), "")


# ------------------------------------------------------------------ Phase 295
class ConditionalHttp:
    """Wraps the HTTP client for one job board: conditional GETs, reusing the saved copy on 304."""

    def __init__(self, http: Any, state: Any, files: Any):
        self.http, self.state, self.files = http, state, files

    def __getattr__(self, name: str) -> Any:
        return getattr(self.http, name)

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        from tools.errors import HttpError
        from tools.http_client import Response

        if method != "GET":
            return self.http.request(method, url, **kwargs)
        key = hashlib.sha256((url + repr(sorted((kwargs.get("params") or {}).items()))).encode()).hexdigest()[:24]
        saved = (self.state.get(VALIDATORS) or {}).get(key) or {}
        path = f"{CACHE_DIR}/{key}.bin"
        headers = dict(kwargs.pop("headers", None) or {})
        if saved and self.files.exists(path):
            if saved.get("etag"):
                headers["If-None-Match"] = saved["etag"]
            if saved.get("last_modified"):
                headers["If-Modified-Since"] = saved["last_modified"]
        try:
            resp = self.http.request("GET", url, headers=headers, **kwargs)
        except HttpError as exc:
            if exc.status == 304 and self.files.exists(path):
                self._note(key, saved, not_modified=True)
                return Response(200, url, self.files.read_bytes(path), {"x-from-cache": "1"})
            raise
        etag, modified = _header(resp.headers, "etag"), _header(resp.headers, "last-modified")
        if etag or modified:
            self.files.write_bytes(path, resp.body)
            self._note(key, {"etag": etag, "last_modified": modified})
        return resp

    def _note(self, key: str, saved: dict[str, Any], not_modified: bool = False) -> None:
        all_saved = dict(self.state.get(VALIDATORS) or {})
        entry = dict(saved)
        entry["not_modified"] = int(entry.get("not_modified") or 0) + (1 if not_modified else 0)
        entry["used_at"] = self.state.now()  # kept while it's used (Phase 392)
        all_saved[key] = entry
        self.state.set(VALIDATORS, all_saved)

    def get(self, url: str, **kwargs: Any) -> Any:
        return self.request("GET", url, **kwargs)

    def get_json(self, url: str, **kwargs: Any) -> Any:
        return self.get(url, **kwargs).json()


# ------------------------------------------------------------------ Phases 296-297
def record(state: Any, source: str, ok: bool, seconds: float) -> None:
    timings = dict(state.get(TIMINGS) or {})
    t = dict(timings.get(source) or {"runs": 0, "avg_ms": 0})
    ms = round(seconds * 1000)
    t["avg_ms"] = round((t["avg_ms"] * t["runs"] + ms) / (t["runs"] + 1))
    t["runs"] += 1
    t["last_ms"], t["last_ok"] = ms, ok
    timings[source] = t
    state.set(TIMINGS, timings)
    failures = dict(state.get(FAILURES) or {})
    failures[source] = 0 if ok else int(failures.get(source) or 0) + 1
    state.set(FAILURES, failures)


def backoff_hours(state: Any, source: str) -> float:
    n = int((state.get(FAILURES) or {}).get(source) or 0)
    return 0.0 if n < 2 else float(min(MAX_BACKOFF_HOURS, 2 ** (n - 1)))


# ------------------------------------------------------------------ Phase 298
def jitter(source: str) -> float:
    """A fixed factor between 0.9 and 1.1 for each board."""
    n = int(hashlib.sha256(source.encode()).hexdigest()[:4], 16) % 201
    return 1 + JITTER * (n - 100) / 100


def due(state: Any, source: str, cadence_hours: float, last: str | None) -> bool:
    wait = max(cadence_hours * jitter(source) if cadence_hours else 0.0, backoff_hours(state, source))
    return not last or not wait or state.clock() - datetime.fromisoformat(last) >= timedelta(hours=wait)


# ------------------------------------------------------------------ Phase 299
def contribution(state: Any, days: int = 30) -> list[dict[str, Any]]:
    from strategies.b2b_lead_aggregator import POOL_NICHE
    from strategies.posting_quality import company_key

    since = (state.clock() - timedelta(days=days)).isoformat(timespec="seconds")
    leads = [lead for lead in state.leads_for_niche(POOL_NICHE) if str(lead.get("first_seen") or "") >= since]
    boards: dict[tuple[str, str], set[str]] = defaultdict(set)
    for lead in leads:
        boards[(company_key(lead.get("company") or ""), str(lead.get("title") or "").lower())].add(str(lead.get("source") or ""))
    new, only = Counter(), Counter()
    for lead in leads:
        src = str(lead.get("source") or "unknown")
        new[src] += 1
        if boards[(company_key(lead.get("company") or ""), str(lead.get("title") or "").lower())] == {src}:
            only[src] += 1
    timings = state.get(TIMINGS) or {}
    return [{"source": s, "new": n, "only_here": only[s], "avg_ms": (timings.get(s) or {}).get("avg_ms")}
            for s, n in new.most_common()]


def describe(state: Any) -> str:
    from strategies.job_sources import CREDITS

    rows = contribution(state)
    if not rows:
        return ""
    return "Job boards, last 30 days: " + "; ".join(
        f"{CREDITS.get(r['source'], (r['source'], ''))[0]} {r['new']} new ({r['only_here']} only there)" for r in rows[:8]) + "."


def timed(fn: Any, *args: Any, **kwargs: Any) -> tuple[Any, float]:
    start = time.monotonic()
    out = fn(*args, **kwargs)
    return out, time.monotonic() - start
