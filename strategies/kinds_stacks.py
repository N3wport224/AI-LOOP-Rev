"""Stack maps (Phases 260-264): which technologies go together.

* **Phase 260, co-occurrence:** for each technology, how often each other technology appears in the
  same postings (``cooccurrence``), as a share of that technology's postings.
* **Phase 261, stack maps** (``stack-map-<tech>``, $9): one row per company hiring for a technology,
  with its full stack, plus ``STACK.md`` (what the technology is used with, and how often) and
  ``stack.json``. Needs ``STACK_MIN_COMPANIES`` companies.
* **Phase 262, technology pairs** (``pair-<a>-<b>``): postings that mention both, e.g. "Companies
  Using Rust and AWS Together", for pairs with enough postings.
* **Phase 263, on the trends page:** the pairs seen together most often in recent postings.
* **Phase 264, "often used with" in READMEs:** a technology dataset's README lists what else its
  postings mention most.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from typing import Any

from strategies.product_kinds import COMPANY_FILES, Kind, build_companies, build_postings, company_candidate, posting_candidate, register

STACK_MIN_COMPANIES = 10
STACK_PRICE = 900
TOP_WITH = 8


# ------------------------------------------------------------------ Phase 260
def cooccurrence(techs_per_posting: list[set[str]]) -> dict[str, dict[str, float]]:
    totals: Counter = Counter()
    pairs: dict[str, Counter] = defaultdict(Counter)
    for techs in techs_per_posting:
        totals.update(techs)
        for a, b in combinations(sorted(techs), 2):
            pairs[a][b] += 1
            pairs[b][a] += 1
    return {t: {o: round(n / totals[t], 2) for o, n in pairs[t].most_common(TOP_WITH)} for t in totals}


def with_line(rows: list[dict[str, Any]], tech: str) -> str:
    """Phase 264: "Also mentioned: AWS (60%), Postgres (40%)"."""
    from strategies.product_factory import facets, label

    shares = cooccurrence([facets(r)["techs"] for r in rows]).get(tech) or {}
    shares = {t: s for t, s in shares.items() if s >= 0.1}
    if not shares:
        return ""
    return "Also mentioned in these postings: " + ", ".join(f"{label(t)} ({round(s * 100)}%)" for t, s in shares.items()) + "."


# ------------------------------------------------------------------ Phase 261
def stack_candidates(tagged: list[tuple[dict[str, Any], dict[str, Any]]], cfg: Any, state: Any) -> list[dict[str, Any]]:
    from strategies.product_factory import _slug, label

    by_tech: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for lead, f in tagged:
        for t in f["techs"]:
            by_tech[t].append(lead)
    out = []
    for tech, rows in by_tech.items():
        cand = company_candidate("stack_map", f"stack-map-{_slug(tech)}", f"{label(tech)} Stack Map: What Hiring Companies Use",
                                 rows, f"hiring for {label(tech)}, with the full stack each one uses", STACK_PRICE,
                                 {"tech": tech}, min_companies=STACK_MIN_COMPANIES)
        if cand:
            cand["postings"] = rows
            out.append(cand)
    return out


def stack_md(tech: str, rows: list[dict[str, Any]], now: datetime) -> tuple[str, dict[str, Any]]:
    from strategies.product_factory import facets, label

    shares = cooccurrence([facets(r)["techs"] for r in rows]).get(tech) or {}
    lines = [f"# What {label(tech)} is used with\n", f"From {len(rows)} current postings mentioning {label(tech)}, "
             f"{now:%Y-%m-%d}. Share of those postings that also mention:\n"]
    lines += [f"- {label(t)}: {round(s * 100)}%" for t, s in shares.items()] or ["- (no other technology stands out)"]
    return "\n".join(lines) + "\n", {"technology": tech, "postings": len(rows), "also_mentioned": shares}


def build_stack_map(cand: dict[str, Any], cfg: Any, now: datetime) -> dict[str, Any]:
    tech = cand["filters"]["tech"]
    md, data = stack_md(tech, cand.get("postings") or [], now)
    return build_companies(cand, cfg, now, {"STACK.md": md, "stack.json": json.dumps(data, indent=2)})


# ------------------------------------------------------------------ Phase 262
def pair_candidates(tagged: list[tuple[dict[str, Any], dict[str, Any]]], cfg: Any, state: Any) -> list[dict[str, Any]]:
    from strategies.product_factory import _slug, label

    by_pair: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for lead, f in tagged:
        for a, b in combinations(sorted(f["techs"]), 2):
            by_pair[(a, b)].append(lead)
    out = []
    for (a, b), rows in by_pair.items():
        if len(rows) < int(cfg.factory_min_rows):
            continue
        cand = posting_candidate("pair", f"pair-{_slug(a)}-{_slug(b)}", f"Companies Using {label(a)} and {label(b)} Together",
                                 rows, f"roles mentioning both {label(a)} and {label(b)}", cfg, {"tech": a, "pair": [a, b]})
        if cand:
            cand["score"] -= 4
            out.append(cand)
    return out


# ------------------------------------------------------------------ Phase 263
def top_pairs(leads: list[dict[str, Any]], limit: int = 10) -> list[tuple[str, int]]:
    from strategies.product_factory import facets, label

    counts: Counter = Counter()
    for lead in leads:
        counts.update(combinations(sorted(facets(lead)["techs"]), 2))
    return [(f"{label(a)} + {label(b)}", n) for (a, b), n in counts.most_common(limit)]


register(Kind("stack_map", "Stack maps", stack_candidates, build_stack_map, files=COMPANY_FILES + ["STACK.md", "stack.json"]))
register(Kind("pair", "Technology pairs", pair_candidates, build_postings))
