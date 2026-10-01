"""Aggregate public developer job listings into validated, enriched, deduplicated datasets.

Sources are official public JSON APIs (no HTML scraping, no logins):

* ``remoteok``  – https://remoteok.com/api (terms: attribute and link back to Remote OK)
* ``arbeitnow`` – https://www.arbeitnow.com/api/job-board-api
* ``hn_hiring`` – the latest Hacker News "Who is hiring?" thread via the Algolia HN API

Every record keeps its ``source`` and ``url`` so exported datasets carry attribution.
Check each source's terms before selling a derived dataset.
"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from strategies.base import Strategy, TaskContext, TaskResult
from tools.errors import CircuitOpenError

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
URL_RE = re.compile(r"https?://[^\s<>\"')]+")

TECH_KEYWORDS = [
    "python", "django", "flask", "fastapi", "javascript", "typescript", "react", "next.js", "vue", "angular",
    "node", "golang", "rust", "java", "kotlin", "scala", "c++", "c#", ".net", "ruby", "rails", "php",
    "laravel", "elixir", "swift", "ios", "android", "flutter", "aws", "gcp", "azure", "kubernetes", "docker",
    "terraform", "postgres", "mysql", "mongodb", "redis", "kafka", "spark", "airflow", "pytorch", "tensorflow",
    "llm", "machine learning", "graphql", "devops", "sre",
]
SENIORITY = [
    ("intern", ("intern", "internship")),
    ("junior", ("junior", "jr.", "jr ", "entry level", "graduate")),
    ("principal", ("principal", "distinguished")),
    ("staff", ("staff",)),
    ("lead", ("lead", "head of", "manager", "director")),
    ("senior", ("senior", "sr.", "sr ")),
]
JOB_BOARD_DOMAINS = {"remoteok.com", "www.remoteok.com", "arbeitnow.com", "www.arbeitnow.com", "news.ycombinator.com"}

POOL_NICHE = "__all__"

EXPORT_FIELDS = [
    "company", "title", "location", "remote", "seniority", "stack", "tags", "salary_min", "salary_max",
    "company_domain", "contact_email", "url", "posted_at", "source",
]


@dataclass
class Lead:
    source: str
    source_id: str
    company: str
    title: str
    url: str
    location: str = ""
    remote: bool = False
    tags: list[str] = field(default_factory=list)
    description: str = ""
    posted_at: str = ""
    contact_email: str = ""
    salary_min: int | None = None
    salary_max: int | None = None
    apply_url: str = ""
    # enrichment
    company_domain: str = ""
    seniority: str = ""
    stack: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["description"] = d["description"][:1500]
        return d


# --------------------------------------------------------------------------- parsing
def _clean(text: Any) -> str:
    text = html.unescape(str(text or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _ts(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            dt = datetime.fromtimestamp(int(value), tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (ValueError, OSError, OverflowError):
        return ""


# When a feed renames a field (say "position" becomes "title"), the agent learns the new name from
# the payload shape and adds it here (agent/evolution/diagnostics.py): {source: {field: [aliases]}}.
# <evolved:FIELD_ALIASES> auto-evolution may rewrite this block (one literal assignment)
FIELD_ALIASES: dict[str, dict[str, list[str]]] = {}
# </evolved:FIELD_ALIASES>

# What each parser saw last time: {source: {"items", "parsed", "keys": {key: kind}}}. Read (and
# cleared) by the aggregator after each fetch; kinds only, never values.
LAST_SHAPE: dict[str, dict[str, Any]] = {}


def _field(item: dict[str, Any], source: str, name: str, default: Any = None) -> Any:
    if item.get(name) not in (None, ""):
        return item[name]
    for alias in FIELD_ALIASES.get(source, {}).get(name, []):
        if item.get(alias) not in (None, ""):
            return item[alias]
    return item.get(name, default)


def _kind(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "epoch" if value > 10**9 else "number"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    text = str(value or "").strip()
    if not text:
        return "empty"
    if text.startswith(("http://", "https://")):
        return "url"
    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        return "date"
    return "text" if len(text) > 80 else "short_text"


def _note_shape(source: str, items: list[Any], parsed: int) -> None:
    dicts = [i for i in items if isinstance(i, dict)]
    keys: dict[str, str] = {}
    for item in dicts[:25]:
        for k, v in item.items():
            if isinstance(k, str) and len(k) <= 40 and (keys.get(k) in (None, "empty")):
                keys[k] = _kind(v)
    LAST_SHAPE[source] = {"items": len(dicts), "parsed": parsed, "keys": dict(sorted(keys.items())[:60])}


def _int_or_none(value: Any) -> int | None:
    try:
        n = int(value)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _append(leads: list[Lead], build: Callable[[], Lead]) -> None:
    """Phase 319: a posting with a malformed field is skipped; the rest of the feed is kept."""
    try:
        leads.append(build())
    except (TypeError, ValueError, AttributeError, KeyError, OverflowError, OSError):
        pass


def parse_remoteok(payload: Any) -> list[Lead]:
    leads = []
    items = payload if isinstance(payload, list) else []
    for item in items:
        if not isinstance(item, dict) or not _field(item, "remoteok", "position"):
            continue  # first element is the API legal notice
        f = lambda name, default=None: _field(item, "remoteok", name, default)  # noqa: E731
        _append(leads, lambda: Lead(
                source="remoteok",
                source_id=str(f("id", "")),
                company=_clean(f("company")),
                title=_clean(f("position")),
                url=str(f("url") or ""),
                apply_url=str(f("apply_url") or ""),
                location=_clean(f("location")) or "Remote",
                remote=True,
                tags=[str(t).lower() for t in f("tags") or []],
                description=_clean(f("description")),
                posted_at=_ts(f("date") or f("epoch")),
                salary_min=_int_or_none(f("salary_min")),
                salary_max=_int_or_none(f("salary_max")),
            ))
    _note_shape("remoteok", items, len(leads))
    return leads


def parse_arbeitnow(payload: Any) -> list[Lead]:
    leads = []
    data = payload.get("data", []) if isinstance(payload, dict) else []
    data = data if isinstance(data, list) else []
    for item in data:
        if not isinstance(item, dict):
            continue
        f = lambda name, default=None: _field(item, "arbeitnow", name, default)  # noqa: E731
        _append(leads, lambda: Lead(
                source="arbeitnow",
                source_id=str(f("slug", "")),
                company=_clean(f("company_name")),
                title=_clean(f("title")),
                url=str(f("url") or ""),
                location=_clean(f("location")),
                remote=bool(f("remote")),
                tags=[str(t).lower() for t in (f("tags") or []) + (f("job_types") or [])],
                description=_clean(f("description")),
                posted_at=_ts(f("created_at")),
            ))
    _note_shape("arbeitnow", data if isinstance(data, list) else [], sum(1 for lead in leads if lead.title and lead.company))
    return leads


def parse_hn_comment(comment: dict[str, Any], story_id: Any = "") -> Lead | None:
    """HN hiring posts conventionally start with ``Company | Role | Location | REMOTE | url``."""
    raw = comment.get("text") or comment.get("comment_text") or ""
    if not raw:
        return None
    first_para = re.split(r"<p>|\n", raw, maxsplit=1)[0]
    header = _clean(first_para)
    parts = [p.strip() for p in header.split("|") if p.strip()]
    if len(parts) < 2:
        return None
    text = _clean(raw)
    urls = URL_RE.findall(html.unescape(raw))
    emails = EMAIL_RE.findall(text)
    location = next((p for p in parts[2:] if not URL_RE.match(p)), "")
    cid = comment.get("id") or comment.get("objectID") or ""
    return Lead(
        source="hn_hiring",
        source_id=str(cid),
        company=parts[0][:120],
        title=parts[1][:160],
        url=f"https://news.ycombinator.com/item?id={cid}",
        apply_url=urls[0] if urls else "",
        location=location[:120],
        remote="remote" in header.lower(),
        description=text,
        posted_at=_ts(comment.get("created_at_i") or comment.get("created_at")),
        contact_email=emails[0] if emails else "",
    )


# --------------------------------------------------------------------------- pipeline
def validate_lead(lead: Lead) -> list[str]:
    problems = []
    if not lead.company or len(lead.company) < 2:
        problems.append("missing company")
    if not lead.title or len(lead.title) < 2:
        problems.append("missing title")
    parts = urlsplit(lead.url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        problems.append("invalid url")
    if lead.contact_email and not EMAIL_RE.fullmatch(lead.contact_email):
        problems.append("invalid email")
    if lead.salary_min and lead.salary_max and lead.salary_min > lead.salary_max:
        problems.append("salary range inverted")
    return problems


def _has_keyword(haystack: str, keyword: str) -> bool:
    return re.search(rf"(?<![\w+#.]){re.escape(keyword)}(?![\w+#])", haystack) is not None


def enrich_lead(lead: Lead) -> Lead:
    haystack = " ".join([lead.title, " ".join(lead.tags), lead.description]).lower()
    lead.stack = [kw for kw in TECH_KEYWORDS if _has_keyword(haystack, kw)]
    title = f" {lead.title.lower()} "
    lead.seniority = next((level for level, words in SENIORITY if any(w in title for w in words)), "mid")
    if not lead.remote and "remote" in f"{lead.location} {lead.title}".lower():
        lead.remote = True
    for candidate in (lead.apply_url, lead.url):
        host = urlsplit(candidate).netloc.lower()
        if host and host not in JOB_BOARD_DOMAINS:
            lead.company_domain = host.removeprefix("www.")
            break
    if lead.contact_email:
        lead.contact_email = lead.contact_email.lower()
    return lead


def _norm(text: str) -> str:
    text = text.lower()
    text = re.sub(r"\b(inc|llc|ltd|gmbh|corp|co)\b\.?", "", text)
    return re.sub(r"[^a-z0-9]+", "", text)


def dedupe_key(lead: Lead) -> str:
    basis = "|".join([_norm(lead.company), _norm(lead.title), _norm(lead.location)])
    return hashlib.sha1(basis.encode()).hexdigest()[:16]


def dedupe(leads: Iterable[Lead]) -> list[tuple[str, Lead]]:
    seen: dict[str, Lead] = {}
    for lead in leads:
        key = dedupe_key(lead)
        current = seen.get(key)
        # prefer the record that carries a contact email / more detail
        if current is None or (lead.contact_email and not current.contact_email):
            seen[key] = lead
    return list(seen.items())


def matches_niche(lead: Lead, keywords: Iterable[str]) -> bool:
    haystack = " ".join([lead.title, " ".join(lead.tags), " ".join(lead.stack)]).lower()
    return any(_has_keyword(haystack, kw.lower()) for kw in keywords)


# --------------------------------------------------------------------------- fetchers
def fetch_remoteok(http, depth: int = 1) -> list[Lead]:
    return parse_remoteok(http.get_json("https://remoteok.com/api"))  # single endpoint: depth doesn't apply


def fetch_arbeitnow(http, depth: int = 1) -> list[Lead]:
    leads: list[Lead] = []
    for page in range(1, max(1, depth) + 1):
        payload = http.get_json("https://www.arbeitnow.com/api/job-board-api", params={"page": page} if page > 1 else None)
        batch = parse_arbeitnow(payload)
        leads.extend(batch)
        if not batch or not ((payload or {}).get("links") or {}).get("next"):
            break
    return leads


def fetch_hn_hiring(http, depth: int = 1, max_comments: int = 400) -> list[Lead]:
    max_comments *= max(1, depth)
    search = http.get_json(
        "https://hn.algolia.com/api/v1/search_by_date",
        params={"tags": "story,author_whoishiring", "query": "who is hiring", "hitsPerPage": 5},
        check_robots=False,  # documented public API endpoint
    )
    hits = [h for h in search.get("hits", []) if "who is hiring" in (h.get("title") or "").lower()]
    if not hits:
        return []
    story_id = hits[0]["objectID"]
    item = http.get_json(f"https://hn.algolia.com/api/v1/items/{story_id}", check_robots=False)
    leads = []
    for child in (item.get("children") or [])[:max_comments]:
        lead = parse_hn_comment(child, story_id)
        if lead:
            leads.append(lead)
    return leads


PARSE_HISTORY = 10


def record_parse(state: Any, source: str, leads: int, shape: dict[str, Any] | None, error: str = "") -> None:
    """Per-source parser health (kv ``parser_health:<source>``, last 10 runs) for self-diagnosis."""
    runs = (state.get(f"parser_health:{source}") or [])[-(PARSE_HISTORY - 1):]
    runs.append({"at": state.now(), "leads": leads, "items": (shape or {}).get("items"), "parsed": (shape or {}).get("parsed"),
                 "keys": (shape or {}).get("keys") or {}, "error": error[:300]})
    state.set(f"parser_health:{source}", runs)


FETCHERS: dict[str, Callable[[Any], list[Lead]]] = {
    "remoteok": fetch_remoteok,
    "arbeitnow": fetch_arbeitnow,
    "hn_hiring": fetch_hn_hiring,
}
from strategies.job_sources import EXTRA_FETCHERS  # noqa: E402 - needs Lead and the helpers above

FETCHERS.update(EXTRA_FETCHERS)  # Phases 195-198


class LeadAggregator(Strategy):
    name = "b2b_lead_aggregator"
    tasks = ("aggregate_leads",)

    def __init__(self, fetchers: dict[str, Callable[[Any], list[Lead]]] | None = None):
        self.fetchers = fetchers or FETCHERS

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        tools = ctx.tools
        from strategies.job_sources import due, fetched
        from strategies.source_efficiency import ConditionalHttp, record as record_fetch, timed

        http = ConditionalHttp(tools.http, tools.state, tools.files)  # Phase 295

        # Phase 199: some boards ask for a few requests a day; those wait their turn (not a failure).
        sources = [s for s in tools.config.lead_sources if s in self.fetchers and due(tools.state, s)]
        if ctx.degraded and len(sources) > 1:
            # On retry, drop sources that failed last attempt to reduce the blast radius.
            failed = set(ctx.payload.get("failed_sources", []))
            sources = [s for s in sources if s not in failed] or sources[:1]

        raw: list[Lead] = []
        failed_sources: list[str] = []
        for source in sources:
            LAST_SHAPE.pop(source, None)
            try:
                depth = int(ctx.params.get("depth", 1))
                fetch = self.fetchers[source]
                got, took = timed(fetch, http, depth=depth) if depth > 1 else timed(fetch, http)
                raw.extend(got)
                fetched(tools.state, source)
                record_fetch(tools.state, source, True, took)  # Phases 296-297
                record_parse(tools.state, source, len(got), LAST_SHAPE.pop(source, None))
            except CircuitOpenError:
                raise  # budget exhausted: stop the whole task, don't blame the source
            except Exception as exc:  # noqa: BLE001 - one bad source must not sink the others
                fetched(tools.state, source)  # a rate-limited board waits its turn after a failure too
                record_fetch(tools.state, source, False, 0.0)
                failed_sources.append(source)
                tools.state.log_error(f"lead_source:{source}", repr(exc))
                record_parse(tools.state, source, 0, LAST_SHAPE.pop(source, None), error=repr(exc))
        ctx.payload["failed_sources"] = failed_sources
        if sources and len(failed_sources) == len(sources):
            raise RuntimeError(f"all lead sources failed: {failed_sources}")
        discovered = 0
        if tools.config.source_discovery_enabled:
            # Sources the agent discovered and trialled itself (agent/source_discovery.py). Read
            # from SQLite each run, so a newly activated source is used without a restart; its
            # failures are tracked there and never fail this task.
            from agent.source_discovery import ingest_active

            extra, _ = ingest_active(tools)
            raw.extend(extra)
            discovered = len(extra)

        valid = []
        for lead in raw:
            if not validate_lead(lead):
                valid.append(enrich_lead(lead))
        # Pool every valid lead (all niches) so hypothesis scoring can measure demand for
        # clusters that aren't being worked yet.
        for key, lead in dedupe(valid):
            tools.state.upsert_lead(key, POOL_NICHE, lead.to_dict())
        keywords = ctx.params.get("keywords", [])
        matched = [lead for lead in valid if matches_niche(lead, keywords)]
        unique = dedupe(matched)

        new = sum(tools.state.upsert_lead(key, ctx.niche, lead.to_dict()) for key, lead in unique)
        total = tools.state.count_leads(ctx.niche)
        rows = tools.state.leads_for_niche(ctx.niche)
        tools.files.write_json(f"exports/{ctx.niche}/leads.json", rows)
        tools.files.write_csv(f"exports/{ctx.niche}/leads.csv", rows, EXPORT_FIELDS)

        metrics = {
            "fetched": len(raw), "valid": len(valid), "matched": len(matched), "unique": len(unique),
            "new": new, "total": total, "failed_sources": failed_sources, "from_discovered_sources": discovered,
        }
        min_leads = tools.config.min_leads_for_asset
        # Only condemn the niche when sources actually answered and still produced too little.
        starving = total < min_leads and not failed_sources and ctx.hypothesis["iterations"] >= 2
        return TaskResult(
            ok=True,
            summary=f"{new} new / {total} total leads for {ctx.niche} ({len(raw)} fetched, {len(failed_sources)} sources failed)",
            metrics=metrics,
            invalidates_hypothesis=starving,
        )
