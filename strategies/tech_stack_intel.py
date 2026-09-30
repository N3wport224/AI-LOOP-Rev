"""High-intent B2B intelligence: turn job postings into company-level tech-stack fingerprints.

For each hiring company in a niche this produces one record with:

* ``company``, ``domain``
* ``tech_stack`` grouped by category (languages, frameworks, databases, data platform, cloud,
  infrastructure, observability, AI/ML) plus a flat ``stack`` list
* ``open_positions`` and ``openings``
* ``intent_signals``: migration, legacy refactor, ERP integration, new team, greenfield, scaling,
  urgent hire, recent funding
* ``urgency_score`` (0-100)
* ``careers_url`` and whether it was verified live with an HTTP 2xx (``careers_url_verified``)

Outputs go to ``exports/intel/<niche>/``: ``tech_radar.json``, ``tech_radar.csv`` and
``EXECUTIVE_TECH_RADAR.md``, and the packager bundles them.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from strategies.b2b_lead_aggregator import JOB_BOARD_DOMAINS
from strategies.base import Strategy, TaskContext, TaskResult
from tools.errors import CircuitOpenError

# category -> canonical name -> regex alternatives (matched case-insensitively on word boundaries)
FINGERPRINTS: dict[str, dict[str, list[str]]] = {
    "languages": {
        "Python": [r"python"], "TypeScript": [r"typescript"], "JavaScript": [r"javascript"], "Go": [r"golang", r"go(?= developer| engineer)"],
        "Rust": [r"rust"], "Java": [r"java(?!script)"], "Kotlin": [r"kotlin"], "Ruby": [r"ruby"], "PHP": [r"php"],
        "C#": [r"c#"], "Scala": [r"scala"], "Elixir": [r"elixir"], "C++": [r"c\+\+"],
    },
    "frameworks": {
        "Django": [r"django"], "Flask": [r"flask"], "FastAPI": [r"fastapi"], "Rails": [r"rails", r"ruby on rails"],
        "Laravel": [r"laravel"], "Spring": [r"spring( boot)?"], "React": [r"react(\.js)?"], "Next.js": [r"next\.?js"],
        "Vue": [r"vue(\.js)?"], "Angular": [r"angular"], "Node.js": [r"node(\.js)?"], ".NET": [r"\.net", r"dotnet"],
    },
    "databases": {
        "PostgreSQL": [r"postgres(ql)?"], "MySQL": [r"mysql"], "MongoDB": [r"mongo(db)?"], "Redis": [r"redis"],
        "Elasticsearch": [r"elastic ?search"], "DynamoDB": [r"dynamo ?db"], "Cassandra": [r"cassandra"],
    },
    "data_platform": {
        "Snowflake": [r"snowflake"], "BigQuery": [r"big ?query"], "Redshift": [r"redshift"], "Databricks": [r"databricks"],
        "dbt": [r"dbt"], "Airflow": [r"airflow"], "Spark": [r"spark"], "Kafka": [r"kafka"],
    },
    "cloud": {"AWS": [r"aws", r"amazon web services"], "GCP": [r"gcp", r"google cloud"], "Azure": [r"azure"]},
    "infrastructure": {
        "Kubernetes": [r"kubernetes", r"k8s"], "Docker": [r"docker"], "Terraform": [r"terraform"],
        "Ansible": [r"ansible"], "Helm": [r"helm"],
    },
    "observability": {"Datadog": [r"datadog"], "Prometheus": [r"prometheus"], "Grafana": [r"grafana"]},
    "ai_ml": {
        "PyTorch": [r"pytorch"], "TensorFlow": [r"tensorflow"], "LLMs": [r"llms?", r"large language models?"],
        "Vector DB": [r"vector (database|db|store)", r"pinecone", r"pgvector", r"weaviate"],
    },
}

# label -> (weight, regex)
INTENT_TRIGGERS: dict[str, tuple[int, str]] = {
    "migration": (20, r"migrat(e|es|ing|ion)"),
    "legacy_refactor": (15, r"legacy|refactor(ing)?|re-?writ(e|ing)|moderni[sz](e|ing|ation)"),
    "erp_integration": (15, r"erp|sap|netsuite|dynamics 365|oracle (ebs|fusion)"),
    "new_team": (20, r"new (team|department|office|division)|founding (engineer|team|member)|first (engineer|hire|\w+ hire)|build(ing)? (out )?(the|a|our) team"),
    "greenfield": (10, r"greenfield|from scratch|zero to one|0 ?(to|->) ?1"),
    "scaling": (10, r"scal(e|ing) (up|our|the)|hyper-?growth|rapid(ly)? grow(ing|th)"),
    "urgent_hire": (25, r"urgent(ly)?|asap|immediate(ly)? (start|hire|available)|start immediately"),
    "recent_funding": (10, r"series [a-d]|just raised|recently raised|newly funded|backed by"),
}

INTEL_FIELDS = [
    "company", "domain", "urgency_score", "intent_signals", "openings", "open_positions", "stack",
    "cloud", "databases", "data_platform", "infrastructure", "careers_url", "careers_url_verified",
    "remote_friendly", "latest_posted_at", "sources",
]

_COMPILED = {
    cat: {name: re.compile(r"(?<![\w+#.])(" + "|".join(pats) + r")(?![\w+#])", re.I) for name, pats in names.items()}
    for cat, names in FINGERPRINTS.items()
}
_TRIGGERS = {label: (w, re.compile(r"\b(" + pat + r")\b", re.I)) for label, (w, pat) in INTENT_TRIGGERS.items()}


def fingerprint(text: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for cat, names in _COMPILED.items():
        hits = [name for name, rx in names.items() if rx.search(text)]
        if hits:
            found[cat] = hits
    return found


def intent_signals(text: str) -> list[str]:
    return [label for label, (_, rx) in _TRIGGERS.items() if rx.search(text)]


def _age_days(posted_at: str, now: datetime) -> float | None:
    if not posted_at:
        return None
    try:
        dt = datetime.fromisoformat(posted_at)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt).total_seconds() / 86400


def urgency_score(signals: list[str], openings: int, freshest_age_days: float | None, salary_disclosed: bool) -> int:
    score = sum(INTENT_TRIGGERS[s][0] for s in set(signals))
    score += min(20, 5 * max(0, openings - 1))
    if freshest_age_days is not None:
        score += 15 if freshest_age_days <= 7 else 8 if freshest_age_days <= 14 else 0
    score += 5 if salary_disclosed else 0
    return max(0, min(100, score))


def _norm_company(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", re.sub(r"\b(inc|llc|ltd|gmbh|corp|co)\b\.?", "", name.lower()))


def careers_candidates(domain: str, leads: list[dict[str, Any]]) -> list[str]:
    candidates: list[str] = []
    for lead in leads:
        apply = lead.get("apply_url") or ""
        host = urlsplit(apply).netloc.lower()
        if apply and host and host not in JOB_BOARD_DOMAINS:
            candidates.append(apply)
    if domain:
        candidates += [f"https://{domain}/careers", f"https://{domain}/jobs"]
    candidates += [lead["url"] for lead in leads if lead.get("url")]
    return list(dict.fromkeys(candidates))


def build_company_records(leads: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    by_company: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for lead in leads:
        if lead.get("company"):
            by_company[_norm_company(lead["company"])].append(lead)
    records = []
    for group in by_company.values():
        text = " \n ".join(
            " ".join([g.get("title", ""), " ".join(g.get("tags") or []), g.get("description", "")]) for g in group
        )
        stack = fingerprint(text)
        signals = intent_signals(text)
        ages = [a for a in (_age_days(g.get("posted_at", ""), now) for g in group) if a is not None]
        domain = next((g["company_domain"] for g in group if g.get("company_domain")), "")
        positions = sorted({g.get("title", "") for g in group if g.get("title")})
        record = {
            "company": group[0]["company"],
            "domain": domain,
            "tech_stack": stack,
            "stack": sorted({t for names in stack.values() for t in names}),
            "cloud": stack.get("cloud", []),
            "databases": stack.get("databases", []),
            "data_platform": stack.get("data_platform", []),
            "infrastructure": stack.get("infrastructure", []),
            "open_positions": positions,
            "openings": len(positions),
            "intent_signals": signals,
            "urgency_score": urgency_score(signals, len(positions), min(ages) if ages else None,
                                           any(g.get("salary_min") for g in group)),
            "careers_candidates": careers_candidates(domain, group),
            "careers_url": "",
            "careers_url_verified": False,
            "remote_friendly": any(g.get("remote") for g in group),
            "latest_posted_at": max((g.get("posted_at") or "" for g in group), default=""),
            "sources": sorted({g.get("source", "") for g in group if g.get("source")}),
        }
        records.append(record)
    records.sort(key=lambda r: (-r["urgency_score"], -r["openings"], r["company"].lower()))
    return records


def render_tech_radar_md(niche_label: str, records: list[dict[str, Any]], generated_at: str, high_urgency: int) -> str:
    n = len(records) or 1
    lines = [
        f"# Executive Tech Radar: {niche_label}",
        "",
        f"_Generated {generated_at} from {sum(r['openings'] for r in records)} open roles at {len(records)} companies._",
        "",
        "## Headlines",
        "",
    ]
    hot = [r for r in records if r["urgency_score"] >= high_urgency]
    signals = Counter(s for r in records for s in r["intent_signals"])
    lines += [
        f"- **{len(hot)} companies** show high hiring urgency (score ≥ {high_urgency}).",
        f"- **{signals.get('migration', 0)}** are migrating platforms; **{signals.get('legacy_refactor', 0)}** are refactoring legacy systems.",
        f"- **{signals.get('new_team', 0)}** are building new teams; **{signals.get('erp_integration', 0)}** mention ERP integration.",
        f"- **{sum(1 for r in records if r['careers_url_verified'])}** careers pages verified live.",
        "",
        "## Stack adoption",
        "",
        "| Category | Technology | Companies | Share |",
        "|---|---|---|---|",
    ]
    for cat in FINGERPRINTS:
        counts = Counter(t for r in records for t in r["tech_stack"].get(cat, []))
        for tech, c in counts.most_common(5):
            lines.append(f"| {cat.replace('_', ' ')} | {tech} | {c} | {c / n:.0%} |")
    lines += ["", "## Intent signals", "", "| Signal | Companies |", "|---|---|"]
    for label, c in signals.most_common():
        lines.append(f"| {label.replace('_', ' ')} | {c} |")
    lines += ["", "## Top 10 by hiring urgency", "", "| Company | Urgency | Openings | Signals | Stack |", "|---|---|---|---|---|"]
    for r in records[:10]:
        lines.append(
            f"| {r['company'].replace('|', '/')} | {r['urgency_score']} | {r['openings']} | "
            f"{', '.join(r['intent_signals']) or '-'} | {', '.join(r['stack'][:6]) or '-'} |"
        )
    migrating = [r for r in records if "migration" in r["intent_signals"] or "legacy_refactor" in r["intent_signals"]]
    if migrating:
        lines += ["", "## Migration watchlist", ""]
        for r in migrating[:15]:
            lines.append(f"- **{r['company']}**: {', '.join(r['stack'][:5]) or 'stack not disclosed'} ({r['openings']} open roles)")
    return "\n".join(lines) + "\n"


class TechStackIntel(Strategy):
    name = "tech_stack_intel"
    tasks = ("build_intel",)

    def _verify(self, ctx: TaskContext, record: dict[str, Any], cache: dict[str, bool], budget: list[int]) -> None:
        for url in record.pop("careers_candidates"):
            if url in cache:
                ok = cache[url]
            elif budget[0] <= 0:
                continue
            else:
                budget[0] -= 1
                try:
                    ok = ctx.tools.http.get(url, attempts=1).ok
                except CircuitOpenError:
                    budget[0] = 0
                    continue
                except Exception:  # noqa: BLE001 - dead link, robots disallow, timeout...
                    ok = False
                cache[url] = ok
            if ok:
                record["careers_url"], record["careers_url_verified"] = url, True
                return

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        tools = ctx.tools
        cfg = tools.config
        leads = tools.state.leads_for_niche(ctx.niche)
        if not leads:
            return TaskResult(True, "no leads yet", {"companies": 0})
        now = tools.state.clock()
        records = build_company_records(leads, now)

        cache: dict[str, bool] = tools.state.get("careers_url_cache", {}) or {}
        budget = [0 if ctx.degraded else cfg.intel_max_url_checks]
        for record in records:
            candidates = list(record["careers_candidates"])
            self._verify(ctx, record, cache, budget)
            if not record["careers_url"] and candidates:
                record["careers_url"] = candidates[0]  # best guess, flagged unverified
        tools.state.set("careers_url_cache", dict(list(cache.items())[-2000:]))

        base = f"exports/intel/{ctx.niche}"
        tools.files.write_json(f"{base}/tech_radar.json", records)
        tools.files.write_csv(f"{base}/tech_radar.csv", records, INTEL_FIELDS)
        from strategies.digital_asset_packager import niche_title

        tools.files.write_text(
            f"{base}/EXECUTIVE_TECH_RADAR.md",
            render_tech_radar_md(niche_title(ctx.niche), records, tools.state.now(), cfg.high_urgency_threshold),
        )
        hot = sum(1 for r in records if r["urgency_score"] >= cfg.high_urgency_threshold)
        verified = sum(1 for r in records if r["careers_url_verified"])
        return TaskResult(
            True,
            f"{len(records)} company fingerprints ({hot} high-urgency, {verified} careers URLs verified)",
            {"companies": len(records), "high_urgency": hot, "verified_urls": verified},
        )
