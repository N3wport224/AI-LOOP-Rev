"""High-intent B2B intelligence: turn job postings into company-level tech-stack fingerprints.

For each hiring company in a niche this produces one record with:

* ``company``, ``domain``
* ``tech_stack`` grouped by category (languages, frameworks, databases, data platform, cloud,
  infrastructure, observability, AI/ML) plus a flat ``stack`` list
* ``open_positions`` and ``openings``
* ``intent_signals``: migration, legacy refactor, ERP integration, new team, greenfield, scaling,
  urgent hire, recent funding
* ``urgency_score`` (0-100): how hard the company is hiring
* **commercial buying intent** (``commercial_intent``): transitions that come with budget, not
  just headcount. Three families:

  - *Migration & modernization*: "moving from Snowflake to BigQuery", "legacy Oracle to
    Postgres", "Kubernetes migration", "migrating to AWS" → Cloud / Database / Kubernetes /
    Platform Migration, with the ``migration_path`` when both ends are named.
  - *Compliance & security*: SOC 2, HIPAA, FedRAMP, PCI DSS, ISO 27001, HITRUST, CMMC, zero trust
    and hardening work; stronger when the posting says they're *pursuing* the certification.
  - *Leadership & scaling*: founding engineer, first DevOps/SRE/data/security hire, head of
    infrastructure/platform: someone about to choose a stack with a fresh budget.

  Each record gets an ``intent_score`` (0-100) and an ``intent_tag`` such as
  ``Urgency: High (Cloud Migration)``, surfaced in the radar, CSV, previews and landers.
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

# Technologies the agent learned from posting tags it couldn't classify (agent/evolution/diagnostics.py).
# Only this literal changes when it evolves; entries whose name is already known above are ignored.
# <evolved:FINGERPRINTS> auto-evolution may rewrite this block (one literal assignment)
EVOLVED_FINGERPRINTS: dict[str, list[str]] = {}
# </evolved:FINGERPRINTS>
_KNOWN_TECH = {name for names in FINGERPRINTS.values() for name in names}
if EVOLVED_FINGERPRINTS:
    FINGERPRINTS["other"] = {name: pats for name, pats in EVOLVED_FINGERPRINTS.items() if name not in _KNOWN_TECH}

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
    "company", "domain", "intent_tag", "intent_score", "migration_path", "commercial_signals",
    "urgency_score", "intent_signals", "openings", "open_positions", "stack",
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


# ----------------------------------------------------------------------------- commercial intent
# Legacy and platform endpoints that aren't in FINGERPRINTS but matter as migration sources/targets.
EXTRA_TECH: dict[str, list[str]] = {
    "Oracle": [r"oracle( db| database)?"], "SQL Server": [r"sql server", r"mssql"], "DB2": [r"db2"],
    "Teradata": [r"teradata"], "Netezza": [r"netezza"], "Sybase": [r"sybase"], "Hadoop": [r"hadoop", r"hdfs"],
    "Mainframe": [r"mainframes?", r"cobol"], "Heroku": [r"heroku"], "VMware": [r"vmware", r"vsphere"],
    "On-prem": [r"on-?prem(ise|ises)?", r"(our own )?data ?cent(er|re)s?", r"bare[- ]metal", r"self-hosted"],
    "Cloud": [r"(the )?cloud", r"public cloud"], "Microservices": [r"micro-?services"], "Monolith": [r"(a |the |our )?monolith"],
    "ECS": [r"ecs"], "Serverless": [r"serverless", r"lambda"], "Informatica": [r"informatica"], "SAS": [r"sas"],
}
CLOUD_TARGETS = {"AWS", "GCP", "Azure", "Cloud", "Serverless"}
ONPREM_SOURCES = {"On-prem", "Mainframe", "VMware"}
DATA_TECH = set(FINGERPRINTS["databases"]) | set(FINGERPRINTS["data_platform"]) | {
    "Oracle", "SQL Server", "DB2", "Teradata", "Netezza", "Sybase", "Hadoop", "Informatica", "SAS"}
_EXTRA_COMPILED = {name: re.compile(r"^(" + "|".join(p) + r")$", re.I) for name, p in EXTRA_TECH.items()}
_FULL_COMPILED = {name: re.compile(r"^(" + "|".join(p) + r")$", re.I)
                  for names in FINGERPRINTS.values() for name, p in names.items()}

_VERB = r"(?:migrat\w*|mov(?:e|es|ed|ing)|transition(?:s|ed|ing)?|switch(?:es|ed|ing)?|port(?:s|ed|ing)?|replatform\w*|shift(?:s|ed|ing)?)"
_END = r"(?=\s*(?:[.;,:!?()\n]|$)|\s+(?:and|with|while|as|by|in|using|over|this|next|within|for)\b)"
_ENDPOINT = r"[\w.+#/-]+(?:\s+[\w.+#/-]+){0,3}?"
MIGRATION_PATTERNS = [
    ("from_to", re.compile(rf"\b{_VERB}\b[^.;\n]{{0,40}}?\bfrom\s+(?P<src>{_ENDPOINT})\s+(?:to|onto|into|->|→)\s+(?P<dst>{_ENDPOINT}){_END}", re.I)),
    ("from_to", re.compile(rf"\blegacy\s+(?P<src>{_ENDPOINT})\s+(?:to|->|→)\s+(?P<dst>{_ENDPOINT}){_END}", re.I)),
    ("from", re.compile(rf"\b{_VERB}\s+(?:away\s+)?(?:off(?:\s+of)?|from)\s+(?P<src>{_ENDPOINT}){_END}", re.I)),
    ("to", re.compile(rf"\b{_VERB}\s+(?:\w+\s+){{0,3}}?(?:to|onto|into)\s+(?P<dst>{_ENDPOINT}){_END}", re.I)),
    ("named", re.compile(r"\b(?P<dst>[\w.+#-]+(?:\s+[\w.+#-]+)?)\s+(?:migrations?|re-?platforming)\b", re.I)),
]
MODERNIZATION = re.compile(r"\b(legacy (?:system|code|stack|platform|application)s?|moderni[sz](?:e|es|ing|ation)|"
                           r"re-?architect\w*|strangler|break(?:ing)? (?:up|apart) (?:the |our |a )?monolith)\b", re.I)

# label, weight, regex
COMPLIANCE_PATTERNS: list[tuple[str, int, re.Pattern[str]]] = [
    ("FedRAMP Authorization", 50, re.compile(r"\bfed-?ramp\b", re.I)),
    ("SOC 2 Compliance", 45, re.compile(r"\bsoc[\s-]?2\b(?:\s+type\s+(?:ii|2|i|1)\b)?", re.I)),
    ("HIPAA Compliance", 45, re.compile(r"\bhipaa\b", re.I)),
    ("PCI DSS Compliance", 45, re.compile(r"\bpci[\s-]?dss\b|\bpci (?:compliance|certification)\b", re.I)),
    ("HITRUST Certification", 45, re.compile(r"\bhitrust\b", re.I)),
    ("CMMC Certification", 45, re.compile(r"\bcmmc\b", re.I)),
    ("ISO 27001 Certification", 40, re.compile(r"\biso[\s/-]?27001\b", re.I)),
    ("Security Hardening", 30, re.compile(r"\b(?:security|infrastructure|system|platform) hardening\b|\bzero[\s-]trust\b", re.I)),
    ("GDPR Compliance", 20, re.compile(r"\bgdpr\b", re.I)),
]
PURSUING = re.compile(r"\b(?:achiev|obtain|pursu|prepar|work(?:ing)? towards?|get(?:ting)?|pass(?:ing)?|earn|attain|lead(?:ing)?|driv(?:e|ing))\w*"
                      r"\s+(?:\w+\s+){0,3}?(?:soc|hipaa|fed-?ramp|pci|iso|hitrust|cmmc)", re.I)

_ROLE = r"devops|sre|site reliability|platform|infrastructure|infra|data|security|ml|machine learning|backend|cloud|engineering"
LEADERSHIP_PATTERNS: list[tuple[str, int, re.Pattern[str]]] = [
    ("Founding Engineer", 50, re.compile(r"\bfounding\s+(?:\w+\s+){0,2}?(?:engineer|developer|cto|architect|team member)s?\b", re.I)),
    ("First {role} Hire", 45, re.compile(rf"\b(?:our|the|a)?\s*first\s+(?:dedicated\s+|full[\s-]time\s+|in-house\s+)?(?P<role>{_ROLE})\s+"
                                         r"(?:engineer|hire|person|lead|role)\b", re.I)),
    ("Head of {role}", 40, re.compile(rf"\b(?:head|director|vp|vice president)\s+of\s+(?P<role>{_ROLE})\b", re.I)),
    ("Team Build-out", 25, re.compile(r"\bbuild(?:ing)?\s+(?:out\s+)?(?:the|a|our)\s+(?:\w+\s+)?team\b", re.I)),
]
_ROLE_LABELS = {"devops": "DevOps", "sre": "SRE", "site reliability": "SRE", "ml": "ML", "machine learning": "ML",
                "infra": "Infrastructure"}

INTENT_LEVELS = ((60, "High"), (35, "Medium"), (1, "Low"))


def resolve_tech(fragment: str) -> str | None:
    """Map a phrase like "legacy Oracle" or "the cloud" to a canonical technology, trying
    shorter suffixes/prefixes so "our on-prem data center" still resolves."""
    words = re.sub(r"[()\[\]\"']", " ", fragment or "").split()
    stop = {"our", "the", "a", "an", "legacy", "existing", "old", "current", "new", "modern", "self-managed", "managed"}
    words = [w for w in words if w.lower() not in stop] or words
    for size in range(min(3, len(words)), 0, -1):
        for start in range(0, len(words) - size + 1):
            cand = " ".join(words[start:start + size]).strip(".,")
            for table in (_FULL_COMPILED, _EXTRA_COMPILED):
                for name, rx in table.items():
                    if rx.match(cand):
                        return name
    return None


def _migration_label(src: str | None, dst: str | None) -> str:
    if dst in CLOUD_TARGETS or src in ONPREM_SOURCES:
        return "Cloud Migration"
    if dst == "Kubernetes":
        return "Kubernetes Migration"
    if (src in DATA_TECH) or (dst in DATA_TECH):
        return "Database Migration"
    if dst in FINGERPRINTS["infrastructure"] or dst in ("Microservices", "ECS", "Serverless"):
        return "Platform Migration"
    return "Stack Migration"


def commercial_intent(text: str, openings: int = 1, freshest_age_days: float | None = None) -> dict[str, Any]:
    """Score buying intent (0-100) from a company's postings. Pure function."""
    hits: list[dict[str, Any]] = []  # {category, label, weight, evidence}
    path = ""
    for kind, rx in MIGRATION_PATTERNS:
        for m in rx.finditer(text):
            src = resolve_tech(m.group("src")) if "src" in rx.groupindex else None
            dst = resolve_tech(m.group("dst")) if "dst" in rx.groupindex else None
            if kind == "named" and dst is None and m.group("dst").lower().split()[-1] in ("cloud", "database", "data", "platform"):
                dst = {"cloud": "Cloud", "platform": "Kubernetes" if "kubernetes" in m.group("dst").lower() else None}.get(
                    m.group("dst").lower().split()[-1], "PostgreSQL" if "database" in m.group("dst").lower() else None)
                label = "Cloud Migration" if dst == "Cloud" else "Database Migration" if dst else "Platform Migration"
                hits.append({"category": "migration", "label": label, "weight": 40, "evidence": m.group(0)})
                continue
            if src is None and dst is None:
                continue  # "moving from junior to senior", "switching to a new team"
            if src and dst and src == dst:
                continue
            explicit = bool(src and dst)
            if explicit and not path:
                path = f"{src} → {dst}"
            hits.append({"category": "migration", "label": _migration_label(src, dst), "weight": 55 if explicit else 45,
                         "evidence": m.group(0)})
    if not any(h["category"] == "migration" for h in hits):
        m = MODERNIZATION.search(text)
        if m:
            hits.append({"category": "migration", "label": "Legacy Modernization", "weight": 30, "evidence": m.group(0)})
    pursuing = bool(PURSUING.search(text))
    for label, weight, rx in COMPLIANCE_PATTERNS:
        m = rx.search(text)
        if m:
            boost = 10 if pursuing and label not in ("Security Hardening", "GDPR Compliance") else 0
            hits.append({"category": "compliance", "label": label, "weight": weight + boost, "evidence": m.group(0)})
    for label, weight, rx in LEADERSHIP_PATTERNS:
        m = rx.search(text)
        if m:
            role = (m.groupdict().get("role") or "").lower()
            nice = _ROLE_LABELS.get(role, role.title())
            hits.append({"category": "leadership", "label": label.format(role=nice), "weight": weight, "evidence": m.group(0)})
    if not hits:
        return {"intent_score": 0, "intent_level": "", "intent_tag": "", "intent_category": "", "commercial_signals": [],
                "migration_path": "", "intent_evidence": []}
    best_by_cat: dict[str, dict[str, Any]] = {}
    for h in hits:
        if h["weight"] > best_by_cat.get(h["category"], {"weight": -1})["weight"]:
            best_by_cat[h["category"]] = h
    ranked = sorted(best_by_cat.values(), key=lambda h: -h["weight"])
    labels = list(dict.fromkeys(h["label"] for h in sorted(hits, key=lambda h: -h["weight"])))
    score = ranked[0]["weight"] + 0.5 * sum(h["weight"] for h in ranked[1:])
    score += min(10, 3 * (len(labels) - 1))
    score += min(10, 3 * max(0, openings - 1))
    if freshest_age_days is not None:
        score += 10 if freshest_age_days <= 7 else 5 if freshest_age_days <= 14 else 0
    score = max(0, min(100, round(score)))
    level = next(name for floor, name in INTENT_LEVELS if score >= floor)
    evidence = list(dict.fromkeys(re.sub(r"\s+", " ", h["evidence"]).strip()[:60] for h in hits))[:3]
    return {
        "intent_score": score, "intent_level": level, "intent_tag": f"Urgency: {level} ({ranked[0]['label']})",
        "intent_category": ranked[0]["category"], "commercial_signals": labels, "migration_path": path,
        "intent_evidence": evidence,
    }


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
        intent = commercial_intent(text, len(positions), min(ages) if ages else None)
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
            **intent,
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
    buying = [r for r in records if r.get("intent_score", 0) > 0]
    levels = Counter(r.get("intent_level") for r in buying)
    cats = Counter(r.get("intent_category") for r in buying)
    lines += [
        f"- **{len(buying)} companies** show commercial buying intent ({levels.get('High', 0)} high, "
        f"{levels.get('Medium', 0)} medium): {cats.get('migration', 0)} migrating or modernizing, "
        f"{cats.get('compliance', 0)} facing compliance work, {cats.get('leadership', 0)} hiring a first/founding lead.",
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
    if buying:
        lines += ["", "## Commercial buying intent", "",
                  "| Company | Intent | Score | Migration path | Signals | Stack |", "|---|---|---|---|---|---|"]
        for r in sorted(buying, key=lambda r: (-r["intent_score"], r["company"].lower()))[:15]:
            lines.append(
                f"| {r['company'].replace('|', '/')} | **{r['intent_tag']}** | {r['intent_score']} | "
                f"{r.get('migration_path') or '-'} | {', '.join(r.get('commercial_signals', [])[:3])} | "
                f"{', '.join(r['stack'][:5]) or '-'} |"
            )
    lines += ["", "## Top 10 by hiring urgency", "", "| Company | Urgency | Intent | Openings | Signals | Stack |",
              "|---|---|---|---|---|---|"]
    for r in records[:10]:
        lines.append(
            f"| {r['company'].replace('|', '/')} | {r['urgency_score']} | {r.get('intent_tag') or '-'} | {r['openings']} | "
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
        from api.data import record_history

        record_history(tools.state, records, now)  # powers /v1/companies/{domain} history
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
