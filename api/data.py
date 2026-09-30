"""The data behind the API: every niche's tech radar, merged per company, plus history and postings.

* **Signals** come from ``exports/intel/<niche>/tech_radar.json`` for every niche the agent has
  worked (the primary, satellites and older niches), merged by company: a company hiring
  in two niches is one record with both niches, the union of stacks and its strongest intent.
* **History** is ``company_history``: one row per company per day the radar was rebuilt, so
  ``/v1/companies/{domain}`` can show how a company's intent tag and migration path changed.
* **Postings** are the company's leads still being seen in the feeds, trimmed to public facts:
  title, location, remote, dates and the public listing URL. No descriptions, no contact details.

The index is rebuilt only when a radar file changes (by mtime), so a burst of API calls doesn't
re-read the files. Ordering is deterministic (intent score, then urgency, then company id), so
cursors stay stable between identical requests.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PUBLIC_FIELDS = [
    "company_id", "company", "domain", "niches", "stack", "intent_tag", "intent_level", "intent_score",
    "intent_category", "commercial_signals", "migration_path", "urgency_score", "openings", "open_positions",
    "remote_friendly", "careers_url", "careers_url_verified", "latest_posted_at",
]
POSTING_FIELDS = ["title", "location", "remote", "posted_at", "url", "source", "first_seen", "last_seen"]
ACTIVE_POSTING_DAYS = 14


class QueryError(ValueError):
    def __init__(self, code: str, message: str, param: str | None = None):
        super().__init__(message)
        self.code, self.message, self.param = code, message, param


def company_id(name: str) -> str:
    base = re.sub(r"\b(inc|llc|ltd|gmbh|corp|co)\b\.?", "", (name or "").lower())
    return re.sub(r"[^a-z0-9]+", "-", base).strip("-") or "unknown"


def _level_rank(level: str | None) -> int:
    return {"High": 3, "Medium": 2, "Low": 1}.get(level or "", 0)


def public_record(r: dict[str, Any], niches: list[str]) -> dict[str, Any]:
    out = {f: r.get(f) for f in PUBLIC_FIELDS}
    out.update(company_id=company_id(r.get("company", "")), niches=sorted(niches), stack=sorted(r.get("stack") or []),
               commercial_signals=list(r.get("commercial_signals") or []), open_positions=list(r.get("open_positions") or [])[:20],
               intent_score=int(r.get("intent_score") or 0), urgency_score=int(r.get("urgency_score") or 0),
               openings=int(r.get("openings") or 0), remote_friendly=bool(r.get("remote_friendly")),
               careers_url_verified=bool(r.get("careers_url_verified")), domain=r.get("domain") or None,
               intent_tag=r.get("intent_tag") or None, intent_level=r.get("intent_level") or None,
               intent_category=r.get("intent_category") or None, migration_path=r.get("migration_path") or None,
               careers_url=r.get("careers_url") or None, latest_posted_at=r.get("latest_posted_at") or None)
    return out


def merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Two niches' views of one company → one record (strongest intent wins, stacks union)."""
    strong, weak = (a, b) if (a["intent_score"], a["urgency_score"]) >= (b["intent_score"], b["urgency_score"]) else (b, a)
    out = dict(strong)
    out["niches"] = sorted(set(a["niches"]) | set(b["niches"]))
    out["stack"] = sorted(set(a["stack"]) | set(b["stack"]))
    out["commercial_signals"] = list(dict.fromkeys(strong["commercial_signals"] + weak["commercial_signals"]))
    out["open_positions"] = sorted(set(a["open_positions"]) | set(b["open_positions"]))[:20]
    out["openings"] = max(a["openings"], b["openings"])
    out["domain"] = strong["domain"] or weak["domain"]
    out["careers_url"] = strong["careers_url"] or weak["careers_url"]
    out["careers_url_verified"] = a["careers_url_verified"] or b["careers_url_verified"]
    out["latest_posted_at"] = max(filter(None, [a["latest_posted_at"], b["latest_posted_at"]]), default=None)
    out["urgency_score"] = max(a["urgency_score"], b["urgency_score"])
    return out


def encode_cursor(offset: int, version: str, filters: str) -> str:
    raw = json.dumps({"o": offset, "v": version[:10], "f": filters[:10]}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str, version: str, filters: str) -> int:
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        offset = int(data["o"])
    except (ValueError, KeyError, TypeError):
        raise QueryError("invalid_cursor", "cursor is not valid; start again without it", "cursor") from None
    if data.get("f") != filters[:10]:
        raise QueryError("invalid_cursor", "cursor was issued for different filters", "cursor")
    if data.get("v") != version[:10]:
        raise QueryError("cursor_expired", "the dataset was refreshed since this cursor was issued; start again", "cursor")
    if offset < 0:
        raise QueryError("invalid_cursor", "cursor is not valid", "cursor")
    return offset


def parse_since(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise QueryError("invalid_parameter", "since must be an ISO 8601 date or datetime (e.g. 2026-09-01)", "since") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _int_param(value: str | None, name: str, lo: int, hi: int, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    if not re.fullmatch(r"-?\d+", value):
        raise QueryError("invalid_parameter", f"{name} must be an integer", name)
    n = int(value)
    if not lo <= n <= hi:
        raise QueryError("invalid_parameter", f"{name} must be between {lo} and {hi}", name)
    return n


class SignalIndex:
    def __init__(self, files: Any, state: Any):
        self.files = files
        self.state = state
        self._lock = threading.Lock()
        self._stamp: tuple = ()
        self._records: list[dict[str, Any]] = []
        self._by_id: dict[str, dict[str, Any]] = {}
        self._by_domain: dict[str, dict[str, Any]] = {}
        self.version = ""

    def _radar_paths(self) -> list[Path]:
        root = self.files.resolve("exports/intel")
        return sorted(root.glob("*/tech_radar.json")) if root.exists() else []

    def refresh(self) -> None:
        paths = self._radar_paths()
        stamp = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in paths)
        with self._lock:
            if stamp == self._stamp and self._records is not None and self.version:
                return
            merged: dict[str, dict[str, Any]] = {}
            for p in paths:
                niche = p.parent.name
                try:
                    rows = json.loads(p.read_text())
                except (OSError, ValueError):
                    continue
                for r in rows if isinstance(rows, list) else []:
                    if not r.get("company"):
                        continue
                    rec = public_record(r, [niche])
                    cid = rec["company_id"]
                    merged[cid] = merge(merged[cid], rec) if cid in merged else rec
            records = sorted(merged.values(), key=lambda r: (-r["intent_score"], -r["urgency_score"], r["company_id"]))
            self._records = records
            self._by_id = {r["company_id"]: r for r in records}
            self._by_domain = {r["domain"].lower(): r for r in records if r.get("domain")}
            self.version = hashlib.sha256(repr(stamp).encode()).hexdigest()[:16]
            self._stamp = stamp

    # -- /v1/signals -------------------------------------------------------------------------
    def query(self, params: dict[str, str], max_limit: int = 100) -> dict[str, Any]:
        self.refresh()
        tech = (params.get("tech") or "").strip().lower() or None
        tag = (params.get("intent_tag") or "").strip().lower() or None
        min_urgency = _int_param(params.get("min_urgency"), "min_urgency", 0, 100)
        min_intent = _int_param(params.get("min_intent"), "min_intent", 0, 100)
        since = parse_since(params.get("since"))
        limit = _int_param(params.get("limit"), "limit", 1, max_limit, default=min(25, max_limit))
        unknown = set(params) - {"tech", "intent_tag", "min_urgency", "min_intent", "since", "limit", "cursor"}
        if unknown:
            raise QueryError("invalid_parameter", f"unknown parameter(s): {', '.join(sorted(unknown))}", sorted(unknown)[0])
        filters = json.dumps([tech, tag, min_urgency, min_intent, since], separators=(",", ":"))
        fkey = hashlib.sha256(filters.encode()).hexdigest()
        offset = decode_cursor(params["cursor"], self.version, fkey) if params.get("cursor") else 0

        def keep(r: dict[str, Any]) -> bool:
            if tech and tech not in {t.lower() for t in r["stack"]}:
                return False
            if tag:
                haystack = " ".join(filter(None, [r["intent_tag"], r["intent_level"], *r["commercial_signals"]])).lower()
                if tag not in haystack:
                    return False
            if min_urgency is not None and r["urgency_score"] < min_urgency:
                return False
            if min_intent is not None and r["intent_score"] < min_intent:
                return False
            return not (since and (r["latest_posted_at"] or "") < since)

        matched = [r for r in self._records if keep(r)]
        page = matched[offset: offset + limit]
        more = offset + limit < len(matched)
        return {
            "object": "list",
            "data": page,
            "pagination": {"limit": limit, "total": len(matched), "has_more": more,
                           "next_cursor": encode_cursor(offset + limit, self.version, fkey) if more else None},
            "meta": {"dataset_version": self.version},
        }

    # -- /v1/companies/{id} ---------------------------------------------------------------------
    def company(self, ident: str, now: datetime) -> dict[str, Any] | None:
        self.refresh()
        ident = (ident or "").strip().lower()
        rec = self._by_domain.get(ident) or self._by_id.get(ident)
        if rec is None and "." in ident:
            rec = self._by_domain.get(ident.removeprefix("www."))
        if rec is None:
            return None
        return {
            "object": "company",
            **rec,
            "history": history(self.state, rec["company_id"]),
            "active_postings": postings(self.state, rec["company_id"], now),
            "meta": {"dataset_version": self.version},
        }

    def stats(self) -> dict[str, Any]:
        self.refresh()
        return {"companies": len(self._records), "with_domain": len(self._by_domain), "version": self.version}


def history(state: Any, cid: str, limit: int = 90) -> list[dict[str, Any]]:
    rows = state._all(
        "SELECT observed_on, intent_tag, intent_score, migration_path, stack FROM company_history "
        "WHERE company_id = ? ORDER BY observed_on DESC LIMIT ?", (cid, limit))
    out, previous = [], None
    for r in reversed(rows):  # keep only days where something changed, oldest first
        item = {"date": r["observed_on"], "intent_tag": r["intent_tag"] or None, "intent_score": int(r["intent_score"] or 0),
                "migration_path": r["migration_path"] or None, "stack": json.loads(r["stack"] or "[]")}
        signature = (item["intent_tag"], item["migration_path"], tuple(item["stack"]))
        if signature != previous:
            out.append(item)
            previous = signature
    return out


def postings(state: Any, cid: str, now: datetime, limit: int = 20) -> list[dict[str, Any]]:
    from strategies.b2b_lead_aggregator import POOL_NICHE

    cutoff = (now - timedelta(days=ACTIVE_POSTING_DAYS)).isoformat(timespec="seconds")
    rows = state._all("SELECT data, first_seen, last_seen FROM leads WHERE niche = ? AND last_seen >= ?", (POOL_NICHE, cutoff))
    out = []
    for r in rows:
        d = json.loads(r["data"])
        if company_id(d.get("company", "")) != cid:
            continue
        out.append({"title": d.get("title") or "", "location": d.get("location") or None, "remote": bool(d.get("remote")),
                    "posted_at": d.get("posted_at") or None, "url": d.get("url") or None, "source": d.get("source") or None,
                    "first_seen": r["first_seen"], "last_seen": r["last_seen"]})
    out.sort(key=lambda p: (p["posted_at"] or p["first_seen"] or "", p["title"]), reverse=True)
    return out[:limit]


def record_history(state: Any, records: list[dict[str, Any]], now: datetime) -> int:
    """Called after each radar rebuild: one row per company per day (the day's latest view wins)."""
    day = now.astimezone(timezone.utc).date().isoformat()
    rows = [(company_id(r.get("company", "")), day, r.get("company", ""), r.get("domain") or None, r.get("intent_tag") or None,
             int(r.get("intent_score") or 0), r.get("migration_path") or None, json.dumps(sorted(r.get("stack") or [])))
            for r in records if r.get("company")]
    with state.tx() as conn:
        conn.executemany(
            "INSERT INTO company_history (company_id, observed_on, company, domain, intent_tag, intent_score, migration_path, stack) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(company_id, observed_on) DO UPDATE SET company = excluded.company, "
            "domain = excluded.domain, intent_tag = excluded.intent_tag, intent_score = excluded.intent_score, "
            "migration_path = excluded.migration_path, stack = excluded.stack", rows)
    return len(rows)
