"""Formulate monetization hypotheses and pick the next one to try after a pivot."""

from __future__ import annotations

import re
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
    1. configured niches, in order;
    2. niches mined from tag frequencies in the leads collected so far (data-driven pivots);
    3. revisits of deprecated niches with broadened keywords, up to ``max_hypothesis_generations``.
    """
    tried = state.hypothesis_keys()

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
