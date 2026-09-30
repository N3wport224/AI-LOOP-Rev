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
        d["description"] = d["description"][:400]
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


def _int_or_none(value: Any) -> int | None:
    try:
        n = int(value)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def parse_remoteok(payload: Any) -> list[Lead]:
    leads = []
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict) or "position" not in item:
            continue  # first element is the API legal notice
        desc = _clean(item.get("description"))
        leads.append(
            Lead(
                source="remoteok",
                source_id=str(item.get("id", "")),
                company=_clean(item.get("company")),
                title=_clean(item.get("position")),
                url=str(item.get("url") or ""),
                apply_url=str(item.get("apply_url") or ""),
                location=_clean(item.get("location")) or "Remote",
                remote=True,
                tags=[str(t).lower() for t in item.get("tags") or []],
                description=desc,
                posted_at=_ts(item.get("date") or item.get("epoch")),
                salary_min=_int_or_none(item.get("salary_min")),
                salary_max=_int_or_none(item.get("salary_max")),
            )
        )
    return leads


def parse_arbeitnow(payload: Any) -> list[Lead]:
    leads = []
    data = payload.get("data", []) if isinstance(payload, dict) else []
    for item in data:
        if not isinstance(item, dict):
            continue
        leads.append(
            Lead(
                source="arbeitnow",
                source_id=str(item.get("slug", "")),
                company=_clean(item.get("company_name")),
                title=_clean(item.get("title")),
                url=str(item.get("url") or ""),
                location=_clean(item.get("location")),
                remote=bool(item.get("remote")),
                tags=[str(t).lower() for t in (item.get("tags") or []) + (item.get("job_types") or [])],
                description=_clean(item.get("description")),
                posted_at=_ts(item.get("created_at")),
            )
        )
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
def fetch_remoteok(http) -> list[Lead]:
    return parse_remoteok(http.get_json("https://remoteok.com/api"))


def fetch_arbeitnow(http) -> list[Lead]:
    return parse_arbeitnow(http.get_json("https://www.arbeitnow.com/api/job-board-api"))


def fetch_hn_hiring(http, max_comments: int = 400) -> list[Lead]:
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


FETCHERS: dict[str, Callable[[Any], list[Lead]]] = {
    "remoteok": fetch_remoteok,
    "arbeitnow": fetch_arbeitnow,
    "hn_hiring": fetch_hn_hiring,
}


class LeadAggregator(Strategy):
    name = "b2b_lead_aggregator"
    tasks = ("aggregate_leads",)

    def __init__(self, fetchers: dict[str, Callable[[Any], list[Lead]]] | None = None):
        self.fetchers = fetchers or FETCHERS

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        tools = ctx.tools
        sources = [s for s in tools.config.lead_sources if s in self.fetchers]
        if ctx.degraded and len(sources) > 1:
            # On retry, drop sources that failed last attempt to reduce the blast radius.
            failed = set(ctx.payload.get("failed_sources", []))
            sources = [s for s in sources if s not in failed] or sources[:1]

        raw: list[Lead] = []
        failed_sources: list[str] = []
        for source in sources:
            try:
                raw.extend(self.fetchers[source](tools.http))
            except CircuitOpenError:
                raise  # budget exhausted: stop the whole task, don't blame the source
            except Exception as exc:  # noqa: BLE001 - one bad source must not sink the others
                failed_sources.append(source)
                tools.state.log_error(f"lead_source:{source}", repr(exc))
        ctx.payload["failed_sources"] = failed_sources
        if sources and len(failed_sources) == len(sources):
            raise RuntimeError(f"all lead sources failed: {failed_sources}")

        valid = []
        for lead in raw:
            if not validate_lead(lead):
                valid.append(enrich_lead(lead))
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
            "new": new, "total": total, "failed_sources": failed_sources,
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
