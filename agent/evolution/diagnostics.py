"""Self-diagnosis: find code-level bottlenecks in recent telemetry and propose patches.

Three detectors read ``data/agent_state.db``:

* **Failing parsers** (``parser_health:<source>``, written by the lead aggregator every run): a
  job-board source that produced 0 leads three runs in a row. If the feed still returns objects
  but the fields the parser expects are gone (a renamed ``position`` → ``title``), the payload's
  key names and value kinds identify the new field, and the patch teaches the parser the alias
  (``FIELD_ALIASES`` in ``strategies/b2b_lead_aggregator.py``). If the feed is unreachable, no code
  change can help: the finding is reported without a patch.
* **Unknown tags**: tags on recent postings that the tech fingerprinting doesn't recognise, but
  that behave like technologies (seen on ≥ ``evolution_min_tag_postings`` postings from ≥ 3
  companies, and mostly on postings that name other technologies, which filters out
  "marketing" or "customer support"). The patch adds them to ``EVOLVED_FINGERPRINTS`` in
  ``strategies/tech_stack_intel.py``, so radars, the API and dossiers start counting them.
* **Revenue plateau**: ``evolution_plateau_cycles`` (5) engine cycles in a row with no checkout
  while something is on sale. The patch adds one new, factual landing-page headline to
  ``seeds/copy_variants.json``; the copy bandit then tests it against the control and retires it
  if it loses. At most one new headline a week, so each gets enough traffic to be judged.

Each patch is an ``EvolutionHypothesis``: target files, the full new content (and its diff), the
metric it should move, and the evidence. Nothing is applied here; ``evolver.Evolver`` does that.
"""

from __future__ import annotations

import ast
import json
import pprint
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from agent.evolution.evolver import EvolutionHypothesis, FileChange, regions, rewrite_region

AGGREGATOR = "strategies/b2b_lead_aggregator.py"
INTEL = "strategies/tech_stack_intel.py"
COPY_SEEDS = "seeds/copy_variants.json"


@dataclass
class Finding:
    kind: str                  # parser_failure | unknown_tags | revenue_plateau
    severity: str              # info | warn | critical
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)
    hypothesis: EvolutionHypothesis | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "severity": self.severity, "summary": self.summary, "evidence": self.evidence,
                "hypothesis": self.hypothesis.to_dict() if self.hypothesis else None}


# ----------------------------------------------------------------------------- evolvable blocks
def block_value(text: str, block: str) -> Any:
    node = ast.parse(regions(text)[block]).body[0]
    return ast.literal_eval(node.value)  # type: ignore[attr-defined]


def render_literal(value: Any) -> str:
    """Python source for ``value`` in the house style (double quotes, sorted keys)."""
    def jsonish(v: Any) -> bool:
        if isinstance(v, dict):
            return all(isinstance(k, str) and jsonish(x) for k, x in v.items())
        if isinstance(v, list):
            return all(jsonish(x) for x in v)
        return isinstance(v, str) or (isinstance(v, (int, float)) and not isinstance(v, bool))

    if jsonish(value):  # then JSON is also a valid Python literal
        return json.dumps(value, indent=4, sort_keys=True, ensure_ascii=False) if value else json.dumps(value)
    return pprint.pformat(value, width=110, sort_dicts=True)


def with_block(text: str, block: str, value: Any) -> str:
    """``text`` with the evolvable block's literal replaced by ``value`` (name and annotation kept)."""
    body = regions(text)[block]
    node = ast.parse(body).body[0]
    head = ast.get_source_segment(body, node.target if isinstance(node, ast.AnnAssign) else node.targets[0])  # type: ignore[attr-defined]
    if isinstance(node, ast.AnnAssign):
        head += ": " + ast.get_source_segment(body, node.annotation)
    return rewrite_region(text, block, f"{head} = {render_literal(value)}\n")


def read(repo: Path, path: str) -> str | None:
    p = repo / path
    return p.read_text() if p.is_file() else None


# ----------------------------------------------------------------------------- parsers
# The field names each parser reads, and the role each one plays.
PARSER_FIELDS: dict[str, dict[str, str]] = {
    "remoteok": {"position": "title", "company": "company", "url": "url", "apply_url": "url", "description": "description",
                 "tags": "tags", "date": "date", "epoch": "date", "location": "location", "id": "id"},
    "arbeitnow": {"title": "title", "company_name": "company", "url": "url", "description": "description", "tags": "tags",
                  "created_at": "date", "location": "location", "slug": "id"},
}
REQUIRED_FIELDS = {"remoteok": ("position", "company"), "arbeitnow": ("title", "company_name")}
SYNONYMS: dict[str, list[str]] = {
    "title": ["title", "position", "job_title", "jobTitle", "role", "job_name", "name", "headline"],
    "company": ["company", "company_name", "companyName", "employer", "organization", "organisation", "company_title", "hiring_company"],
    "url": ["url", "link", "job_url", "jobUrl", "href", "permalink", "apply_url", "listing_url"],
    "description": ["description", "body", "content", "summary", "text", "details", "job_description", "description_html"],
    "tags": ["tags", "skills", "keywords", "categories", "technologies", "stack", "labels"],
    "date": ["date", "epoch", "created_at", "posted_at", "published_at", "publication_date", "createdAt", "postedAt", "published"],
    "location": ["location", "locations", "candidate_required_location", "region", "place", "city"],
    "id": ["id", "slug", "uuid", "job_id", "jobId", "guid"],
}
KIND_OK = {"title": {"short_text"}, "company": {"short_text"}, "url": {"url"}, "description": {"text", "short_text"},
           "tags": {"list"}, "date": {"date", "epoch", "number"}, "location": {"short_text", "text", "list"},
           "id": {"short_text", "number", "epoch"}}
FAILING_RUNS = 3


def diagnose_parsers(state: Any, repo: Path) -> list[Finding]:
    out: list[Finding] = []
    text = read(repo, AGGREGATOR)
    aliases: dict[str, dict[str, list[str]]] = block_value(text, "FIELD_ALIASES") if text and "FIELD_ALIASES" in regions(text) else {}
    for source in sorted(PARSER_FIELDS):
        runs = state.get(f"parser_health:{source}") or []
        recent = runs[-FAILING_RUNS:]
        if len(recent) < FAILING_RUNS or any(int(r.get("leads") or 0) > 0 for r in recent):
            continue
        last = recent[-1]
        keys: dict[str, str] = last.get("keys") or {}
        evidence = {"source": source, "runs": recent, "keys": keys}
        if not keys or not last.get("items"):
            why = last.get("error") or "the feed returned no items"
            out.append(Finding("parser_failure", "warn", f"{source}: 0 leads in {FAILING_RUNS} runs ({why}). No code change can fix an "
                                                         "unreachable feed; it's retried every cycle.", evidence))
            continue
        fields = PARSER_FIELDS[source]
        known = dict(aliases.get(source, {}))
        added: dict[str, str] = {}
        for fname, role in fields.items():
            if keys.get(fname) not in (None, "empty") or any(a in keys for a in known.get(fname, [])):
                continue
            taken = set(fields) | {a for al in known.values() for a in al} | set(added.values())
            cand = [k for k in SYNONYMS[role] if k in keys and k not in taken and keys[k] in KIND_OK[role]]
            if cand:
                added[fname] = cand[0]
        missing = [f for f in REQUIRED_FIELDS[source] if keys.get(f) in (None, "empty") and f not in added
                   and not any(a in keys for a in known.get(f, []))]
        if not added or missing:
            out.append(Finding("parser_failure", "critical",
                               f"{source}: {last['items']} items but 0 leads; can't map required field(s) "
                               f"{', '.join(missing) or '?'} from keys {', '.join(sorted(keys)[:15])}", evidence))
            continue
        new_aliases = json.loads(json.dumps(aliases))
        for fname, alias in added.items():
            new_aliases.setdefault(source, {}).setdefault(fname, []).append(alias)
        hyp = EvolutionHypothesis(
            "parser_heuristics", f"Improved parser heuristics for {source}",
            f"{source} returned {last['items']} items but the parser produced 0 leads for {FAILING_RUNS} runs: the feed renamed "
            + ", ".join(f"{f} → {a}" for f, a in added.items()) + ". Teach the parser the new field names.",
            f"{source} leads parsed per fetch", 0.0, float(last["items"]),
            [FileChange(AGGREGATOR, text, with_block(text, "FIELD_ALIASES", new_aliases))], {**evidence, "aliases": added})
        out.append(Finding("parser_failure", "critical", f"{source}: fields renamed ({', '.join(f'{f}→{a}' for f, a in added.items())})",
                           evidence, hyp))
    return out


# ----------------------------------------------------------------------------- unknown tags
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9.+#-]{1,23}( [a-z0-9.+#-]{2,15})?$")
STOP = {
    "senior", "junior", "lead", "staff", "principal", "engineer", "engineering", "developer", "dev", "development", "software",
    "backend", "back end", "back-end", "frontend", "front end", "front-end", "fullstack", "full stack", "full-stack", "remote",
    "worldwide", "manager", "management", "design", "designer", "marketing", "sales", "support", "customer support", "finance",
    "exec", "executive", "hr", "recruiter", "admin", "analyst", "data", "web", "mobile", "cloud", "saas", "startup", "technical",
    "tech", "non tech", "digital nomad", "content", "writing", "writer", "legal", "operations", "ops", "product", "qa", "testing",
    "security", "devops", "sre", "api", "apis", "architecture", "architect", "consultant", "contract", "freelance", "part time",
    "full time", "full-time", "part-time", "internship", "intern", "entry level", "english", "german", "europe", "usa", "us",
    "uk", "teaching", "education", "health", "healthcare", "crypto", "web3", "blockchain", "ai", "ml", "machine learning",
    "game", "games", "gaming", "video", "ecommerce", "e-commerce", "fintech", "infrastructure", "platform", "systems", "network",
    "networking", "embedded", "hardware", "research", "scientist", "science", "mathematics", "consulting", "growth", "seo",
    "community", "social media", "video editing", "copywriting", "accounting", "recruiting", "hiring", "junior developer",
    "senior developer", "engineering manager", "technical support", "it", "code", "coding", "programming", "open source",
    "agile", "scrum", "teamlead", "team lead", "mid level", "mid-level", "medior", "voluntary", "volunteer", "no degree",
}
CASING = {
    "graphql": "GraphQL", "ios": "iOS", "nestjs": "NestJS", "nodejs": "Node.js", "sql": "SQL", "html": "HTML", "css": "CSS",
    "sveltekit": "SvelteKit", "tailwind": "Tailwind CSS", "tailwindcss": "Tailwind CSS", "react native": "React Native",
    "openai": "OpenAI", "langchain": "LangChain", "clickhouse": "ClickHouse", "mariadb": "MariaDB", "rabbitmq": "RabbitMQ",
    "jquery": "jQuery", "wordpress": "WordPress", "webgl": "WebGL", "webrtc": "WebRTC", "opentelemetry": "OpenTelemetry",
    "github actions": "GitHub Actions", "gitlab": "GitLab", "argocd": "Argo CD", "graphql api": "GraphQL", "sqlite": "SQLite",
    "nuxt": "Nuxt", "nuxtjs": "Nuxt", "deno": "Deno", "bun": "Bun", "unity": "Unity", "linux": "Linux", "macos": "macOS",
}
# Words that make a tag a role, field or topic rather than a technology ("web dev", "product
# manager", "software engineering"): any tag containing one is never learned.
NON_TECH_WORDS = {
    "dev", "devs", "developer", "developers", "development", "engineer", "engineers", "engineering", "manager", "management",
    "designer", "design", "analyst", "analytics", "lead", "leader", "head", "director", "product", "products", "math", "maths",
    "mathematics", "marketing", "sales", "support", "recruiter", "recruiting", "operations", "ops", "admin", "assistant",
    "consultant", "consulting", "intern", "junior", "senior", "staff", "principal", "remote", "web", "software", "mobile",
    "game", "games", "vfx", "art", "artist", "animation", "writer", "writing", "editor", "editing", "teacher", "tutor",
    "finance", "accounting", "legal", "health", "medical", "science", "scientist", "research", "researcher", "business",
    "customer", "success", "community", "content", "social", "media", "video", "audio", "music", "photo", "photography",
    "hr", "people", "talent", "copywriter", "copywriting", "seo", "growth", "strategy", "startup", "saas", "ecommerce",
}
MIN_COMPANIES = 3
MIN_TECH_RATIO = 0.6
MAX_NEW_TAGS = 8
TAG_WINDOW_DAYS = 14
MIN_POSTINGS = 20
MAX_FALSE_HIT_RATIO = 0.25  # a learned pattern may match at most this share of postings without the tag


def display_name(tag: str) -> str:
    return CASING.get(tag) or (" ".join(w.capitalize() for w in tag.split()) if re.fullmatch(r"[a-z ]+", tag) else tag)


def tag_pattern(tag: str) -> str:
    return re.escape(tag).replace(r"\ ", "[ -]?")


def diagnose_unknown_tags(state: Any, config: Any, repo: Path, now: datetime) -> list[Finding]:
    from strategies.b2b_lead_aggregator import POOL_NICHE
    from strategies.tech_stack_intel import FINGERPRINTS, fingerprint, resolve_tech

    builtin = {name for names in FINGERPRINTS.values() for name in names}

    since = (now - timedelta(days=TAG_WINDOW_DAYS)).isoformat(timespec="seconds")
    rows = state._all("SELECT data FROM leads WHERE niche = ? AND last_seen >= ?", (POOL_NICHE, since))
    if len(rows) < MIN_POSTINGS:
        return []
    text = read(repo, INTEL)
    if not text or "FINGERPRINTS" not in regions(text):
        return []
    learned: dict[str, list[str]] = block_value(text, "FINGERPRINTS")
    learned_patterns = {p for pats in learned.values() for p in pats}
    postings: dict[str, int] = {}
    companies: dict[str, set[str]] = {}
    with_tech: dict[str, int] = {}
    per_posting: list[set[str]] = []
    bodies: list[str] = []
    for row in rows:
        try:
            lead = json.loads(row["data"])
        except (TypeError, ValueError):
            continue
        tags = {str(t).strip().lower() for t in lead.get("tags") or [] if isinstance(t, str)}
        body = " ".join([lead.get("title") or "", " ".join(tags), (lead.get("description") or "")[:1500]])
        has_tech = bool(fingerprint(body))
        unknown = {t for t in tags if TAG_RE.match(t) and t not in STOP and not t.isdigit()
                   and not set(re.split(r"[ -]", t)) & NON_TECH_WORDS
                   and not fingerprint(t) and not resolve_tech(t) and tag_pattern(t) not in learned_patterns}
        per_posting.append(unknown)
        bodies.append(body)
        for t in unknown:
            postings[t] = postings.get(t, 0) + 1
            companies.setdefault(t, set()).add((lead.get("company") or "").lower())
            with_tech[t] = with_tech.get(t, 0) + int(has_tech)
    minimum = int(getattr(config, "evolution_min_tag_postings", 5))

    def precise(t: str) -> bool:
        # A tag that is also an everyday word ("go", "rest") would match prose everywhere once learned.
        rx = re.compile(r"(?<![\w+#.])(" + tag_pattern(t) + r")(?![\w+#])", re.I)
        others = [b for b, u in zip(bodies, per_posting) if t not in u]
        return sum(1 for b in others if rx.search(b)) <= MAX_FALSE_HIT_RATIO * max(1, len(others))

    chosen = sorted((t for t in postings if postings[t] >= minimum and len(companies[t]) >= MIN_COMPANIES
                     and with_tech[t] / postings[t] >= MIN_TECH_RATIO and display_name(t) not in learned
                     and display_name(t) not in builtin and precise(t)),
                    key=lambda t: (-postings[t], t))[:MAX_NEW_TAGS]
    if not chosen:
        return []
    total = len(per_posting)
    affected = sum(1 for u in per_posting if u & set(chosen))
    evidence = {t: {"postings": postings[t], "companies": len(companies[t]), "tech_ratio": round(with_tech[t] / postings[t], 2)}
                for t in chosen}
    new = dict(learned)
    for t in chosen:
        new[display_name(t)] = [tag_pattern(t)]
    names = ", ".join(display_name(t) for t in chosen)
    hyp = EvolutionHypothesis(
        "intent_vocabulary", f"Improved tech extraction: learned {names}"[:120],
        f"{affected} of {total} postings in the last {TAG_WINDOW_DAYS} days carry technology tags the fingerprinting doesn't "
        f"recognise ({names}); each appears on ≥ {minimum} postings from ≥ {MIN_COMPANIES} companies, mostly next to known "
        "technologies. Add them so stacks, radars, the API and dossiers include them.",
        "% of recent postings with a frequent unrecognised tech tag", round(100 * affected / total, 1), 0.0,
        [FileChange(INTEL, text, with_block(text, "FINGERPRINTS", new))], {"tags": evidence, "postings": total})
    return [Finding("unknown_tags", "warn", f"{len(chosen)} unrecognised technology tag(s) on {affected}/{total} recent postings: {names}",
                    {"tags": evidence}, hyp)]


# ----------------------------------------------------------------------------- revenue plateau
# Factual headline formulas: every number is a page fact, and ``requires`` names the facts that
# must be non-zero for the claim to be shown (enforced again by tools/copy_bandit.py).
HEADLINE_POOL: list[tuple[str, str, list[str]]] = [
    ("scored_intent", "{companies} {label} companies, each scored for buying intent", []),
    ("urgent_ranked", "{hot} {label} companies with urgent hiring signals, ranked", ["hot"]),
    ("weekly_changes", "Which {label} companies changed their stack this week", []),
    ("careers_checked", "{verified} {label} careers pages checked live, with the stack behind each", ["verified"]),
    ("who_is_moving", "Who is migrating, and to what: {companies} {label} companies", []),
    ("one_file", "Every {label} company hiring now, in one file for {price}", []),
]
COPY_SPACING_DAYS = 7


def diagnose_revenue_plateau(state: Any, config: Any, repo: Path, now: datetime, log: Any = None) -> list[Finding]:
    n = int(getattr(config, "evolution_plateau_cycles", 5))
    cycles = state._all("SELECT cycle, MIN(created_at) AS started FROM actions WHERE name != 'cycle' GROUP BY cycle "
                        "ORDER BY cycle DESC LIMIT ?", (n,))
    if len(cycles) < n:
        return []
    since = min(c["started"] for c in cycles)
    if state._one("SELECT COUNT(*) AS n FROM orders WHERE occurred_at >= ?", (since,))["n"]:
        return []
    evidence = {"cycles": [c["cycle"] for c in cycles], "since": since}
    live = [a for a in state.list_assets() if a.get("status") == "published" and a.get("checkout_url")]
    if not live:
        return [Finding("revenue_plateau", "info", f"{n} cycles without a checkout, but nothing is on sale yet: copy isn't the "
                                                   "bottleneck (finish Stripe and tunnel setup)", evidence)]
    if log is not None:
        recent = [a for a in log.recent(50) if a["kind"] == "copy_variants" and a["status"] in ("merged", "rolled_back", "running")
                  and a["created_at"] >= (now - timedelta(days=COPY_SPACING_DAYS)).isoformat(timespec="seconds")]
        if recent:
            return [Finding("revenue_plateau", "info", f"{n} cycles without a checkout; the headline added on "
                                                       f"{recent[0]['created_at'][:10]} is still collecting data", evidence)]
    from tools.copy_bandit import SLOTS

    text = read(repo, COPY_SEEDS)
    try:
        data = json.loads(text) if text else {}
    except ValueError:
        return [Finding("revenue_plateau", "warn", f"{COPY_SEEDS} is not valid JSON; not adding copy", evidence)]
    arms = dict((data.get("headline") or {}) if isinstance(data, dict) else {})
    used_codes = {a["code"] for a in SLOTS["headline"].values()} | {str(a.get("code")) for a in arms.values() if isinstance(a, dict)}
    pick = next((p for p in HEADLINE_POOL if p[0] not in arms and p[0] not in SLOTS["headline"]), None)
    code = next((c for c in "abcdefgijklnopqrstuwxyz" if c not in used_codes), None)
    if pick is None or code is None:
        return [Finding("revenue_plateau", "warn", f"{n} cycles without a checkout and every prepared headline has been tried",
                        evidence)]
    name, headline, requires = pick
    arms[name] = {"code": code, "text": headline, "requires": requires}
    after = json.dumps({**(data if isinstance(data, dict) else {}), "headline": arms}, indent=2, sort_keys=True) + "\n"
    hyp = EvolutionHypothesis(
        "copy_variants", f"New landing-page headline to test: {name}",
        f"No checkout in the last {n} engine cycles (since {since[:16]}) while {len(live)} product(s) are on sale. Add one new "
        f"factual headline arm (\"{headline}\") for the copy bandit to test against the control; it's retired automatically "
        "if it converts worse.",
        f"checkouts per {n} cycles", 0.0, 1.0, [FileChange(COPY_SEEDS, text, after)], {**evidence, "variant": name})
    return [Finding("revenue_plateau", "warn", f"{n} cycles without a checkout: proposing headline '{name}'", evidence, hyp)]


# ----------------------------------------------------------------------------- entry point
def diagnose(state: Any, config: Any, repo: str | Path, now: datetime | None = None, log: Any = None) -> list[Finding]:
    """Every finding, most actionable first (findings with a patch before those without)."""
    repo = Path(repo)
    now = now or state.clock()
    findings: list[Finding] = []
    for detector in (lambda: diagnose_parsers(state, repo), lambda: diagnose_unknown_tags(state, config, repo, now),
                     lambda: diagnose_revenue_plateau(state, config, repo, now, log)):
        try:
            findings.extend(detector())
        except Exception as exc:  # noqa: BLE001 - one broken detector must not hide the others
            findings.append(Finding("diagnostics_error", "warn", f"detector failed: {exc!r}"))
    rank = {"critical": 0, "warn": 1, "info": 2}
    return sorted(findings, key=lambda f: (f.hypothesis is None, rank.get(f.severity, 3)))
