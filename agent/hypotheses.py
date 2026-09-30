"""Formulate monetization hypotheses and pick the next one to try after a pivot."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from agent.config import Config
from agent.state import StateStore

STRATEGY = "lead_directory"

# Tags too generic to define a sellable niche on their own.
GENERIC_TAGS = {
    "remote", "full-time", "full time", "part-time", "contract", "engineer", "engineering", "developer",
    "software", "senior", "junior", "dev", "tech", "digital", "it", "exec", "non tech", "support",
    "mid", "lead", "internship", "english", "freelance", "startup",
}


# Keyword clusters the agent can pivot into, and which clusters sit next to which.
CLUSTERS: dict[str, list[str]] = {
    "ai-infrastructure": ["mlops", "llm", "gpu", "inference", "vector database", "pytorch", "ml platform"],
    "data-engineering": ["snowflake", "dbt", "airflow", "spark", "kafka", "databricks", "data engineer"],
    "cloud-security": ["security engineer", "appsec", "devsecops", "iam", "soc 2", "cloud security", "zero trust"],
    "platform-kubernetes": ["kubernetes", "platform engineer", "helm", "terraform", "argo", "k8s"],
    "rust-systems": ["rust", "systems engineer", "embedded", "low latency", "golang"],
    "fullstack-typescript": ["typescript", "node", "next.js", "full stack", "fullstack"],
}
ADJACENT: dict[str, list[str]] = {
    "python-remote": ["ai-infrastructure", "data-engineering", "cloud-security"],
    "ml-ai": ["ai-infrastructure", "data-engineering"],
    "devops-sre": ["platform-kubernetes", "cloud-security", "ai-infrastructure"],
    "frontend-react": ["fullstack-typescript"],
    "rust-go-systems": ["rust-systems", "platform-kubernetes", "cloud-security"],
    "ai-infrastructure": ["data-engineering", "platform-kubernetes"],
    "data-engineering": ["ai-infrastructure", "cloud-security"],
    "cloud-security": ["platform-kubernetes", "ai-infrastructure"],
    "platform-kubernetes": ["cloud-security", "ai-infrastructure"],
    "rust-systems": ["platform-kubernetes", "cloud-security"],
    "fullstack-typescript": ["ai-infrastructure"],
}
DEMAND_WINDOW_DAYS = 7


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def hypothesis_key(niche: str, generation: int) -> str:
    return f"{STRATEGY}:{niche}:g{generation}"


def describe(niche: str, keywords: list[str], generation: int) -> str:
    kw = ", ".join(keywords[:5])
    suffix = f" (revisit #{generation - 1}, broadened)" if generation > 1 else ""
    return (
        f"A curated hiring directory for '{niche}' roles ({kw}) sells at least one copy per day, "
        f"supported by staged value-first outreach{suffix}."
    )


def formulate_next(state: StateStore, config: Config) -> dict[str, Any] | None:
    """Return params for the next untried hypothesis, or None when the space is exhausted.

    Order of exploration:
    1. clusters adjacent to the most recently deprecated niche, highest recent hiring demand first;
    2. configured niches, in order;
    3. niches mined from tag frequencies in the leads collected so far (data-driven variants);
    4. revisits of deprecated niches with broadened keywords, up to ``max_hypothesis_generations``.
    """
    tried = state.hypothesis_keys()

    ranked = demand_ranked(state, config, tried)
    if ranked:
        return ranked

    # No market evidence yet (cold start, or sources down): fall back to the fixed exploration order.
    adjacent = adjacent_candidates(state, tried)
    if adjacent:
        return adjacent

    for niche in config.niches:
        name = slugify(niche["name"])
        if hypothesis_key(name, 1) not in tried:
            return _params(name, list(niche["keywords"]), 1, origin="configured")

    configured_keywords = {k.lower() for n in config.niches for k in n["keywords"]}
    for tag, count in state.tag_frequencies(limit=40):
        if tag in GENERIC_TAGS or tag in configured_keywords or len(tag) < 2 or count < config.min_leads_for_asset:
            continue
        name = f"tag-{slugify(tag)}"
        if hypothesis_key(name, 1) not in tried:
            return _params(name, [tag], 1, origin=f"mined from {count} collected leads")

    deprecated = [h for h in state.list_hypotheses() if h["status"] == "deprecated"]
    by_niche: dict[str, dict[str, Any]] = {}
    for h in deprecated:
        niche = h["params"]["niche"]
        if niche not in by_niche or h["params"].get("generation", 1) > by_niche[niche]["params"].get("generation", 1):
            by_niche[niche] = h
    for niche, h in sorted(by_niche.items(), key=lambda kv: kv[1]["id"]):
        generation = int(h["params"].get("generation", 1)) + 1
        if generation > config.max_hypothesis_generations or hypothesis_key(niche, generation) in tried:
            continue
        keywords = _broaden(h["params"]["keywords"], state)
        return _params(niche, keywords, generation, origin=f"revisit of #{h['id']}")
    return None


ADJACENCY_BONUS = 1.25


def demand_ranked(state: StateStore, config: Config, tried: set[str]) -> dict[str, Any] | None:
    """Pick the untried niche with the most hiring demand in the last ``DEMAND_WINDOW_DAYS``.

    Candidates come from every source at once: clusters adjacent to the last deprecated niche
    (small bonus), configured niches, all known clusters, and tags mined from live postings.
    Only candidates backed by at least ``min_leads_for_asset`` recent roles count as evidence.
    """
    since = state.clock() - timedelta(days=DEMAND_WINDOW_DAYS)
    texts = state.recent_lead_texts(since)
    if not texts:
        return None
    deprecated = [h for h in state.list_hypotheses() if h["status"] == "deprecated"]
    last = max(deprecated, key=lambda h: (h["updated_at"], h["id"]))["params"]["niche"] if deprecated else None
    adjacent = set(ADJACENT.get(last or "", []))

    candidates: dict[str, tuple[int, list[str], str]] = {}  # niche -> (priority, keywords, kind)
    for c in ADJACENT.get(last or "", []):
        candidates.setdefault(c, (0, CLUSTERS[c], "adjacent"))
    for n in config.niches:
        candidates.setdefault(slugify(n["name"]), (1, list(n["keywords"]), "configured"))
    for c, kws in CLUSTERS.items():
        candidates.setdefault(c, (2, kws, "cluster"))
    configured_keywords = {k.lower() for n in config.niches for k in n["keywords"]}
    for tag, _ in state.tag_frequencies(limit=40):
        if tag in GENERIC_TAGS or tag in configured_keywords or len(tag) < 2:
            continue
        candidates.setdefault(f"tag-{slugify(tag)}", (3, [tag], "mined"))

    best = None
    for niche, (priority, keywords, kind) in candidates.items():
        if hypothesis_key(niche, 1) in tried:
            continue
        demand = state.lead_demand(keywords, since, texts)
        if demand < config.min_leads_for_asset:
            continue
        score = demand * (ADJACENCY_BONUS if niche in adjacent else 1.0)
        if best is None or (score, -priority) > (best[0], -best[1]):
            best = (score, priority, niche, keywords, kind, demand)
    if best is None:
        return None
    _, _, niche, keywords, kind, demand = best
    evidence = f"{demand} matching roles in {DEMAND_WINDOW_DAYS}d"
    origin = {
        "adjacent": f"adjacent to {last} ({evidence})",
        "mined": f"mined from market demand ({evidence})",
    }.get(kind, f"{kind}, market demand ({evidence})")
    return _params(niche, list(keywords), 1, origin=origin)


def adjacent_candidates(state: StateStore, tried: set[str]) -> dict[str, Any] | None:
    deprecated = [h for h in state.list_hypotheses() if h["status"] == "deprecated"]
    if not deprecated:
        return None
    last = max(deprecated, key=lambda h: (h["updated_at"], h["id"]))
    source = last["params"]["niche"]
    since = state.clock() - timedelta(days=DEMAND_WINDOW_DAYS)
    ranked = []
    for i, cluster in enumerate(ADJACENT.get(source, [])):
        if hypothesis_key(cluster, 1) in tried:
            continue
        demand = state.lead_demand(CLUSTERS[cluster], since)
        ranked.append((-demand, i, cluster, demand))
    if not ranked:
        return None
    _, _, cluster, demand = min(ranked)
    return _params(cluster, list(CLUSTERS[cluster]), 1, origin=f"adjacent to {source} ({demand} matching roles in {DEMAND_WINDOW_DAYS}d)")


def score_hypothesis(state: StateStore, hyp: dict[str, Any]) -> dict[str, Any]:
    """Funnel metrics and a single comparable score for a hypothesis."""
    m = state.metrics_for_hypothesis(hyp["id"])
    views, impressions = m.get("views", 0), m.get("impressions", 0)
    purchases = max(m.get("purchases", 0), state.purchases_for_hypothesis(hyp["id"]))
    revenue = state.revenue_for_hypothesis(hyp["id"])
    cycles = max(1, hyp["iterations"])
    return {
        "impressions": impressions,
        "views": views,
        "purchases": purchases,
        "revenue_cents": revenue,
        "view_rate": round(views / impressions, 4) if impressions else 0.0,
        "conversion": round(purchases / views, 4) if views else 0.0,
        "velocity": round(purchases / cycles, 4),  # purchases per cycle
        "score": round(revenue / 100 * 10 + purchases * 25 + views * 1 + impressions * 0.1, 2),
    }


def pivot_reason(state: StateStore, config: Config, hyp: dict[str, Any]) -> str | None:
    """Why this hypothesis should be deprecated now, or None to keep going."""
    s = score_hypothesis(state, hyp)
    n = hyp["iterations"]
    age = state.clock() - datetime.fromisoformat(hyp["created_at"])
    if age < timedelta(days=config.min_hypothesis_days):
        return None  # too young to judge; hard evidence (no data at all) still pivots via the engine
    if s["revenue_cents"] > 0 or s["purchases"] > 0:
        # Traction must be current, not historical: one early sale shouldn't pin the agent to a
        # niche that stopped selling. Active subscribers count as current traction.
        days = config.stale_revenue_days
        since = state.clock() - timedelta(days=days)
        niche = hyp["params"].get("niche")
        subscribed = niche and state.list_subscribers(("active", "trialing"), niche=niche)
        recent = state.revenue_for_hypothesis_since(hyp["id"], since) > 0 or state.orders_since(hyp["id"], since) > 0
        if n >= config.pivot_after_iterations and not subscribed and not recent:
            return f"traction faded: no verified revenue in {days} days"
        return None
    if state.get("view_tracking") and n >= config.signal_window_iterations and s["views"] == 0:
        return f"no views or sales after {n} iterations"
    if n >= config.pivot_after_iterations:
        return f"zero verified revenue after {n} iterations"
    return None


def _broaden(keywords: list[str], state: StateStore) -> list[str]:
    """Add the tags that most often co-occur in the data to widen the net on a revisit."""
    extra = [t for t, _ in state.tag_frequencies(limit=10) if t not in GENERIC_TAGS and t not in keywords]
    return list(dict.fromkeys(keywords + extra[:3]))


def _params(niche: str, keywords: list[str], generation: int, origin: str) -> dict[str, Any]:
    return {
        "key": hypothesis_key(niche, generation),
        "strategy": STRATEGY,
        "description": describe(niche, keywords, generation),
        "params": {"niche": niche, "keywords": keywords, "generation": generation, "origin": origin},
    }
