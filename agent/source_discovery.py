"""Autonomous discovery of new public job sources, with a sandboxed trial before any of them feeds the pipeline.

**Where candidates come from** (no hard-coded list of boards to maintain):

1. **Company ATS boards** found in the agent's own data. Every careers URL it verifies and every
   apply link in its leads is checked for Greenhouse (``boards.greenhouse.io/<board>``), Lever
   (``jobs.lever.co/<company>``) and Ashby (``jobs.ashbyhq.com/<company>``). All three publish a
   documented public JSON API of the company's open roles, meant for exactly this kind of reuse,
   and a company that already showed buying intent is the best one to watch.
2. **RSS/Atom autodiscovery**: ``<link rel="alternate" type="application/rss+xml">`` on the
   pages of job sites the leads link to.
3. **A small seed catalogue** of public job feeds (``SEED_FEEDS``) plus ``source_seed_feeds`` from
   the config. Review each site's terms before relying on it: robots.txt says what may be
   fetched, not what may be resold.

GitHub trending isn't used: it lists repositories, not job postings.

**The sandbox** (``probe``). Every check has to pass:

* HTTPS only, to a public host. IP literals, localhost and names that resolve to private,
  loopback, link-local or reserved addresses are refused, because candidate URLs come from
  scraped data and must not reach the local network (SSRF).
* robots.txt allows the URL (checked even when ``respect_robots_txt`` is off).
* One GET, one attempt, polite timeouts; a 429 or ``Retry-After`` counts as a failure
  ("rate-limit friendliness"), and each host is probed at most once a day during the trial.
* Response ≤ 2 MB, content type and structure match the adapter (JSON shape, or RSS/Atom
  parsed with no DOCTYPE allowed).
* Yield: the share of items that become valid leads (company + title + URL), how many carry a
  recognisable stack, and how many carry a buying-intent signal.

**Lifecycle**: ``candidate`` → first healthy probe → ``trial`` → healthy on ``source_trial_days``
separate days, error rate ≤ ``source_max_error_rate``, valid ratio ≥ ``source_min_valid_ratio``
and at least one buying-intent signal → ``active``. Robots disallow or 3 failures in a row →
``rejected``. Active sources are ingested by the lead aggregator every cycle (read from SQLite,
so no restart is needed); an active source failing more than half of its last 10 runs is
``suspended`` and re-trialled after a week. At most ``source_max_active`` are active.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

from strategies.b2b_lead_aggregator import Lead, _clean, _ts, enrich_lead, validate_lead
from strategies.base import Strategy, TaskContext, TaskResult
from strategies.tech_stack_intel import commercial_intent, fingerprint
from tools.errors import CircuitOpenError, ToolError

MAX_BYTES = 2 * 1024 * 1024
MAX_ITEMS = 300
ATS_PATTERNS = [
    ("greenhouse", re.compile(r"https?://(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)", re.I),
     "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"),
    ("lever", re.compile(r"https?://jobs\.lever\.co/([A-Za-z0-9_.-]+)", re.I), "https://api.lever.co/v0/postings/{slug}?mode=json"),
    ("ashby", re.compile(r"https?://jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)", re.I),
     "https://api.ashbyhq.com/posting-api/job-board/{slug}"),
]
SEED_FEEDS = [
    ("weworkremotely-programming", "rss", "https://weworkremotely.com/categories/remote-programming-jobs.rss"),
    ("weworkremotely-devops", "rss", "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss"),
    ("remotive-software-dev", "json", "https://remotive.com/api/remote-jobs?category=software-dev&limit=100"),
    ("jobicy-dev", "json", "https://jobicy.com/api/v2/remote-jobs?count=50&industry=dev"),
]
BLOCKED_HOSTS = ("localhost", ".local", ".internal", ".lan", ".home", ".corp", ".localdomain")
RESOLVER: Callable[..., Any] = socket.getaddrinfo  # module hook: tests stub DNS here


def _rfc822(value: str) -> str:
    """RSS pubDate (RFC 2822) → ISO; falls back to the ISO parser for Atom dates."""
    from email.utils import parsedate_to_datetime

    try:
        return _ts(parsedate_to_datetime(value).timestamp()) if value and "," in value else _ts(value)
    except (TypeError, ValueError):
        return _ts(value)


# ----------------------------------------------------------------------------- safety
def resolve_public(host: str, resolver: Callable[..., Any] | None = None) -> bool:
    """True only if every address the host resolves to is public."""
    try:
        infos = (resolver or RESOLVER)(host, 443, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return False
    addrs = {info[4][0] for info in infos}
    return bool(addrs) and all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addrs)


def url_problem(url: str, resolver: Callable[..., Any] | None | bool = None) -> str | None:
    """Why a URL may not be fetched, or None. ``resolver=False`` skips the DNS check."""
    parts = urlsplit(url or "")
    host = (parts.hostname or "").lower()
    if parts.scheme != "https":
        return "only https sources are allowed"
    if not host or parts.username or parts.password or (parts.port not in (None, 443)):
        return "unexpected host, credentials or port in URL"
    try:
        ipaddress.ip_address(host)
        return "IP-address URLs are not allowed"
    except ValueError:
        pass
    if host.endswith(BLOCKED_HOSTS) or host == "localhost" or "." not in host:
        return "internal hostname"
    if resolver is not False and not resolve_public(host, resolver or None):
        return "host doesn't resolve to a public address"
    return None


# ----------------------------------------------------------------------------- adapters
def _lead(source: str, sid: Any, company: str, title: str, url: str, **kw: Any) -> Lead:
    return Lead(source=source, source_id=str(sid or url), company=_clean(company)[:120], title=_clean(title)[:200],
                url=url or "", **kw)


def parse_greenhouse(payload: Any, name: str, company: str) -> list[Lead]:
    jobs = (payload or {}).get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        raise ValueError("greenhouse: no jobs array")
    return [_lead(name, j.get("id"), company, j.get("title", ""), j.get("absolute_url", ""),
                  location=_clean((j.get("location") or {}).get("name", "")), description=_clean(j.get("content", ""))[:4000],
                  posted_at=_ts(j.get("updated_at") or j.get("first_published")), apply_url=j.get("absolute_url", ""),
                  remote="remote" in str((j.get("location") or {}).get("name", "")).lower())
            for j in jobs[:MAX_ITEMS] if isinstance(j, dict)]


def parse_lever(payload: Any, name: str, company: str) -> list[Lead]:
    if not isinstance(payload, list):
        raise ValueError("lever: expected a list of postings")
    out = []
    for j in payload[:MAX_ITEMS]:
        if not isinstance(j, dict):
            continue
        cats = j.get("categories") or {}
        out.append(_lead(name, j.get("id"), company, j.get("text", ""), j.get("hostedUrl", ""), location=_clean(cats.get("location", "")),
                         description=_clean(j.get("descriptionPlain") or j.get("description") or "")[:4000],
                         posted_at=_ts((j.get("createdAt") or 0) / 1000 if j.get("createdAt") else None),
                         apply_url=j.get("applyUrl", ""), remote="remote" in str(cats.get("location", "")).lower(),
                         tags=[t for t in [cats.get("team"), cats.get("department")] if t]))
    return out


def parse_ashby(payload: Any, name: str, company: str) -> list[Lead]:
    jobs = (payload or {}).get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        raise ValueError("ashby: no jobs array")
    return [_lead(name, j.get("id") or j.get("jobUrl"), company, j.get("title", ""), j.get("jobUrl", ""),
                  location=_clean(j.get("location", "")), description=_clean(j.get("descriptionPlain", ""))[:4000],
                  posted_at=_ts(j.get("publishedAt")), apply_url=j.get("applyUrl", ""), remote=bool(j.get("isRemote")))
            for j in jobs[:MAX_ITEMS] if isinstance(j, dict)]


_TITLE_KEYS = ("title", "position", "job_title", "jobTitle", "name")
_COMPANY_KEYS = ("company_name", "company", "companyName", "employer", "organization")
_URL_KEYS = ("url", "apply_url", "applyUrl", "jobUrl", "link", "job_url")
_DESC_KEYS = ("description", "content", "body", "jobDescription", "descriptionPlain", "excerpt")
_DATE_KEYS = ("publication_date", "pubDate", "date", "created_at", "createdAt", "published_at", "postedAt", "date_posted")


def _first(d: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        v = d.get(k)
        if isinstance(v, dict):
            v = v.get("name") or v.get("title")
        if v:
            return v
    return None


def parse_json_generic(payload: Any, name: str, company: str = "") -> list[Lead]:
    items = payload
    if isinstance(payload, dict):
        items = next((payload[k] for k in ("jobs", "data", "results", "items", "postings") if isinstance(payload.get(k), list)), None)
    if not isinstance(items, list):
        raise ValueError("json: no list of jobs found")
    out = []
    for j in items[:MAX_ITEMS]:
        if not isinstance(j, dict):
            continue
        tags = j.get("tags") if isinstance(j.get("tags"), list) else []
        out.append(_lead(name, j.get("id"), _first(j, _COMPANY_KEYS) or company, _first(j, _TITLE_KEYS) or "",
                         str(_first(j, _URL_KEYS) or ""), description=_clean(_first(j, _DESC_KEYS) or "")[:4000],
                         posted_at=_ts(_first(j, _DATE_KEYS)), location=_clean(_first(j, ("candidate_required_location", "location", "jobGeo")) or ""),
                         tags=[str(t) for t in tags][:15], remote=True if "remote" in name else bool(j.get("remote"))))
    return out


_TITLE_SPLIT = [re.compile(r"^(?P<company>[^:|]{2,80})\s*[:|]\s*(?P<title>.+)$"), re.compile(r"^(?P<title>.+?)\s+at\s+(?P<company>[^,]{2,80})$", re.I)]


def parse_rss(body: bytes, name: str, company: str = "") -> list[Lead]:
    if b"<!DOCTYPE" in body[:2048].upper() or b"<!ENTITY" in body.upper():
        raise ValueError("rss: DOCTYPE/ENTITY declarations are refused")
    root = ET.fromstring(body)
    atom = "{http://www.w3.org/2005/Atom}"
    items = root.findall("./channel/item") or root.findall(f"{atom}entry")
    if not items and root.tag not in ("rss", f"{atom}feed"):
        raise ValueError("rss: not an RSS or Atom document")
    out = []
    for it in items[:MAX_ITEMS]:
        def text(tag: str, it: ET.Element = it) -> str:
            el = it.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""
        link = text("link") or ((it.find(f"{atom}link").get("href") if it.find(f"{atom}link") is not None else "") or "")
        raw_title = text("title") or text(f"{atom}title")
        comp, title = company, raw_title
        for rx in _TITLE_SPLIT:
            m = rx.match(raw_title)
            if m:
                comp, title = m.group("company").strip(), m.group("title").strip()
                break
        out.append(_lead(name, text("guid") or link, comp, title, link,
                         description=_clean(text("description") or text(f"{atom}summary") or text(f"{atom}content"))[:4000],
                         posted_at=_rfc822(text("pubDate") or text(f"{atom}updated") or text(f"{atom}published")),
                         location=_clean(text("region") or ""), remote=True))
    return out


ADAPTERS: dict[str, Callable[..., list[Lead]]] = {
    "greenhouse": parse_greenhouse, "lever": parse_lever, "ashby": parse_ashby, "json": parse_json_generic,
}


def parse(kind: str, body: bytes, name: str, company: str) -> list[Lead]:
    if kind == "rss":
        return parse_rss(body, name, company)
    return ADAPTERS[kind](json.loads(body.decode("utf-8", errors="replace")), name, company)


# ----------------------------------------------------------------------------- discovery
class _FeedLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.feeds: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "link" and "alternate" in a.get("rel", "").lower() and a.get("type", "").lower() in (
                "application/rss+xml", "application/atom+xml") and a.get("href"):
            self.feeds.append(a["href"])


def autodiscover_feeds(html_text: str, base_url: str) -> list[str]:
    p = _FeedLinks()
    try:
        p.feed(html_text[:500_000])
    except Exception:  # noqa: BLE001 - junk HTML just yields nothing
        return []
    return list(dict.fromkeys(urljoin(base_url, f) for f in p.feeds))[:5]


def ats_candidates(urls: list[str]) -> list[tuple[str, str, str, str]]:
    """(name, kind, api_url, company) for every ATS board seen in the given URLs."""
    out = {}
    for url in urls:
        for kind, rx, api in ATS_PATTERNS:
            m = rx.search(url or "")
            if m:
                slug = m.group(1).strip("/").lower()
                if slug and slug not in ("embed", "jobs", "api"):
                    out[f"{kind}:{slug}"] = (f"{kind}:{slug}", kind, api.format(slug=slug), slug.replace("-", " ").title())
    return sorted(out.values())


class SourceRegistry:
    def __init__(self, state: Any, config: Any):
        self.state = state
        self.config = config

    def get(self, name: str) -> dict[str, Any] | None:
        return self.state._one("SELECT * FROM sources WHERE name = ?", (name,))

    def by_status(self, *statuses: str) -> list[dict[str, Any]]:
        marks = ",".join("?" * len(statuses))
        return self.state._all(f"SELECT * FROM sources WHERE status IN ({marks}) ORDER BY COALESCE(last_checked_at, ''), id",
                               statuses)

    def all(self) -> list[dict[str, Any]]:
        return self.state._all("SELECT * FROM sources ORDER BY status, name")

    def add_candidate(self, name: str, kind: str, url: str, discovered_from: str, company: str = "") -> bool:
        now = self.state.now()
        cur = self.state._exec(
            "INSERT OR IGNORE INTO sources (name, kind, url, status, discovered_from, created_at, updated_at, notes) "
            "VALUES (?,?,?,?,?,?,?,?)", (name, kind, url, "candidate", discovered_from[:200], now, now,
                                         json.dumps({"company": company})))
        return cur.rowcount == 1

    def set_status(self, source_id: int, status: str, error: str = "") -> None:
        self.state._exec("UPDATE sources SET status = ?, last_error = ?, updated_at = ? WHERE id = ?",
                         (status, error[:300] or None, self.state.now(), source_id))

    def record_run(self, source_id: int, mode: str, ok: bool, items: int = 0, valid: int = 0, signals: int = 0,
                   latency_ms: int | None = None, error: str = "") -> None:
        now = self.state.now()
        self.state._exec("INSERT INTO source_runs (source_id, at, mode, ok, items, valid, signals, latency_ms, error) "
                         "VALUES (?,?,?,?,?,?,?,?,?)", (source_id, now, mode, int(ok), items, valid, signals, latency_ms, error[:300] or None))
        self.state._exec("UPDATE sources SET last_checked_at = ?, last_error = ?, updated_at = ? WHERE id = ?",
                         (now, None if ok else error[:300], now, source_id))

    def runs(self, source_id: int, limit: int = 50) -> list[dict[str, Any]]:
        return self.state._all("SELECT * FROM source_runs WHERE source_id = ? ORDER BY id DESC LIMIT ?", (source_id, limit))

    def health(self, source_id: int) -> dict[str, Any]:
        runs = self.runs(source_id)
        ok = [r for r in runs if r["ok"]]
        items = sum(r["items"] for r in ok)
        return {
            "runs": len(runs), "ok_runs": len(ok), "error_rate": round(1 - len(ok) / len(runs), 3) if runs else 0.0,
            "healthy_days": len({r["at"][:10] for r in ok}), "valid_ratio": round(sum(r["valid"] for r in ok) / items, 3) if items else 0.0,
            "signals": sum(r["signals"] for r in ok), "consecutive_failures": next((i for i, r in enumerate(runs) if r["ok"]), len(runs)),
            "recent_error_rate": round(sum(1 for r in runs[:10] if not r["ok"]) / len(runs[:10]), 3) if runs else 0.0,
        }


def probe(http: Any, source: dict[str, Any], resolver: Callable[..., Any] | None = None,
          clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Fetch once in the sandbox and measure. Never raises for source problems (CircuitOpenError
    propagates: that's our budget, not the source's fault)."""
    url = source["url"]
    problem = url_problem(url, resolver)
    if problem:
        return {"ok": False, "fatal": True, "error": problem}
    try:
        if not http.allowed_by_robots(url):
            return {"ok": False, "fatal": True, "error": "robots.txt disallows it"}
    except CircuitOpenError:
        raise
    except Exception as exc:  # noqa: BLE001 - an unreadable robots.txt means "allowed" per the spec, but be conservative
        return {"ok": False, "error": f"robots.txt check failed: {exc!r}"[:200]}
    start = clock()
    try:
        resp = http.get(url, check_robots=False, attempts=1, headers={"Accept": "application/json, application/rss+xml, application/xml;q=0.9"})
    except CircuitOpenError:
        raise
    except ToolError as exc:
        return {"ok": False, "error": str(exc)[:200]}
    latency = int((clock() - start) * 1000)
    if resp.headers.get("retry-after") or resp.status == 429:
        return {"ok": False, "error": "rate limited (429 / Retry-After)", "latency_ms": latency}
    if len(resp.body) > MAX_BYTES:
        return {"ok": False, "error": f"response too large ({len(resp.body)} bytes)", "latency_ms": latency}
    meta = json.loads(source.get("notes") or "{}")
    try:
        leads = parse(source["kind"], resp.body, source["name"], meta.get("company", ""))
    except (ValueError, ET.ParseError, KeyError, TypeError) as exc:
        return {"ok": False, "error": f"unexpected structure: {exc}"[:200], "latency_ms": latency}
    valid = [enrich_lead(lead) for lead in leads if not validate_lead(lead)]
    stacked = sum(1 for lead in valid if fingerprint(" ".join([lead.title, lead.description, " ".join(lead.tags)])))
    signals = sum(1 for lead in valid if commercial_intent(" ".join([lead.title, lead.description]))["intent_score"] > 0)
    if not leads:
        return {"ok": False, "error": "no items", "latency_ms": latency}
    return {"ok": True, "items": len(leads), "valid": len(valid), "stacked": stacked, "signals": signals, "latency_ms": latency,
            "leads": valid}


def evaluate(registry: SourceRegistry, source: dict[str, Any], config: Any) -> str:
    """Apply the lifecycle rules after a probe. Returns the (possibly new) status."""
    h = registry.health(source["id"])
    status = source["status"]
    if status in ("candidate", "trial") and h["consecutive_failures"] >= 3:
        registry.set_status(source["id"], "rejected", "3 failed probes in a row")
        return "rejected"
    if status == "candidate" and h["ok_runs"] >= 1:
        registry.set_status(source["id"], "trial")
        status = "trial"
    if status == "trial" and (h["healthy_days"] >= config.source_trial_days and h["error_rate"] <= config.source_max_error_rate
                              and h["valid_ratio"] >= config.source_min_valid_ratio and h["signals"] >= 1):
        if len(registry.by_status("active")) >= config.source_max_active:
            return "trial"  # full: stays qualified, waits for a slot
        registry.set_status(source["id"], "active")
        return "active"
    if status == "active" and h["runs"] >= 5 and h["recent_error_rate"] > 0.5:
        registry.set_status(source["id"], "suspended", f"{h['recent_error_rate']:.0%} of recent runs failed")
        return "suspended"
    return status


def discover(tools: Any, registry: SourceRegistry) -> dict[str, int]:
    """Collect candidates from our own data, autodiscovered feeds and the seed catalogue."""
    state, cfg = tools.state, tools.config
    urls: list[str] = []
    root = tools.files.resolve("exports/intel")
    for path in (sorted(root.glob("*/tech_radar.json")) if root.exists() else []):
        try:
            urls += [r.get("careers_url") or "" for r in json.loads(path.read_text())]
        except (OSError, ValueError):
            continue
    from strategies.b2b_lead_aggregator import POOL_NICHE

    for row in state._all("SELECT data FROM leads WHERE niche = ? ORDER BY id DESC LIMIT 2000", (POOL_NICHE,)):
        d = json.loads(row["data"])
        urls += [d.get("apply_url") or "", d.get("url") or ""]
    added = {"ats": 0, "seed": 0, "rss": 0}
    for name, kind, api, company in ats_candidates(urls):
        added["ats"] += registry.add_candidate(name, kind, api, "careers/apply URL in collected data", company)
    for name, kind, url in SEED_FEEDS + [(f"seed:{urlsplit(u).netloc}{urlsplit(u).path}"[:80], "rss" if re.search(r"(rss|atom|feed|\.xml)", u, re.I) else "json", u)
                                         for u in cfg.source_seed_feeds]:
        added["seed"] += registry.add_candidate(name, kind, url, "seed catalogue")
    return added


def autodiscover(tools: Any, registry: SourceRegistry, limit: int = 2, resolver: Callable[..., Any] | None = None) -> int:
    """Look for RSS/Atom links on a few job-site pages the leads point to (non-ATS hosts)."""
    from strategies.b2b_lead_aggregator import POOL_NICHE

    tried = set(tools.state.get("source_autodiscovered_hosts", []) or [])
    hosts: dict[str, str] = {}
    for row in tools.state._all("SELECT data FROM leads WHERE niche = ? ORDER BY id DESC LIMIT 500", (POOL_NICHE,)):
        url = json.loads(row["data"]).get("url") or ""
        host = urlsplit(url).netloc.lower()
        if host and host not in tried and not any(rx.search(url) for _, rx, _ in ATS_PATTERNS):
            hosts.setdefault(host, f"https://{host}/")
    added = 0
    for host, home in list(hosts.items())[:limit]:
        tried.add(host)
        if url_problem(home, resolver) or not tools.http.allowed_by_robots(home):
            continue
        try:
            page = tools.http.get(home, check_robots=False, attempts=1).text
        except CircuitOpenError:
            raise
        except ToolError:
            continue
        for feed in autodiscover_feeds(page, home):
            added += registry.add_candidate(f"rss:{urlsplit(feed).netloc}{urlsplit(feed).path}"[:80], "rss", feed, f"<link rel=alternate> on {home}")
    tools.state.set("source_autodiscovered_hosts", sorted(tried)[-500:])
    return added


def ingest_active(tools: Any, max_sources: int = 5, resolver: Callable[..., Any] | None = None) -> tuple[list[Lead], list[str]]:
    """Fetch the least-recently-checked active sources. Returns (valid leads, failed source names)."""
    registry = SourceRegistry(tools.state, tools.config)
    leads: list[Lead] = []
    failed: list[str] = []
    for src in registry.by_status("active")[:max_sources]:
        result = probe(tools.http, src, resolver)
        registry.record_run(src["id"], "ingest", result["ok"], result.get("items", 0), result.get("valid", 0),
                            result.get("signals", 0), result.get("latency_ms"), result.get("error", ""))
        if result["ok"]:
            leads += result["leads"]
        else:
            failed.append(src["name"])
            evaluate(registry, registry.get(src["name"]), tools.config)
    return leads, failed


class SourceDiscovery(Strategy):
    name = "source_discovery"
    tasks = ("discover_sources",)

    def __init__(self, resolver: Callable[..., Any] | None = None):
        self.resolver = resolver

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        tools, cfg, state = ctx.tools, ctx.tools.config, ctx.tools.state
        if not cfg.source_discovery_enabled:
            return TaskResult(True, "source discovery disabled", {})
        now = state.clock()
        last = state.get("source_discovery_last")
        if last and now - datetime.fromisoformat(last) < timedelta(hours=cfg.source_discovery_interval_hours):
            return TaskResult(True, f"next discovery pass after {last} + {cfg.source_discovery_interval_hours}h", {"due": False})
        registry = SourceRegistry(state, cfg)
        added = discover(tools, registry)
        try:
            added["rss"] = autodiscover(tools, registry, resolver=self.resolver)
        except CircuitOpenError:
            pass
        # Re-trial suspended sources after a week.
        for src in registry.by_status("suspended"):
            if src["updated_at"] and now - datetime.fromisoformat(src["updated_at"]) > timedelta(days=7):
                registry.set_status(src["id"], "trial", "re-trial after suspension")
        today = now.astimezone(timezone.utc).date().isoformat()
        probed, changes = 0, []
        for src in registry.by_status("candidate", "trial"):
            if probed >= cfg.source_probes_per_run:
                break
            if (src["last_checked_at"] or "")[:10] == today:
                continue  # at most one probe per source per day: polite, and trial days must be distinct
            try:
                result = probe(tools.http, src, self.resolver)
            except CircuitOpenError:
                break
            probed += 1
            registry.record_run(src["id"], "probe", result["ok"], result.get("items", 0), result.get("valid", 0),
                                result.get("signals", 0), result.get("latency_ms"), result.get("error", ""))
            if result.get("fatal"):
                registry.set_status(src["id"], "rejected", result["error"])
                changes.append(f"{src['name']}: rejected ({result['error']})")
                continue
            new = evaluate(registry, registry.get(src["name"]), cfg)
            if new != src["status"]:
                changes.append(f"{src['name']}: {src['status']} → {new}")
        state.set("source_discovery_last", now.isoformat(timespec="seconds"))
        counts = {s: len(registry.by_status(s)) for s in ("candidate", "trial", "active", "suspended", "rejected")}
        return TaskResult(True, f"sources: {counts}; new candidates {added}; probed {probed}"
                          + (f"; {'; '.join(changes)}" if changes else ""),
                          {"added": added, "probed": probed, "counts": counts, "changes": changes})
