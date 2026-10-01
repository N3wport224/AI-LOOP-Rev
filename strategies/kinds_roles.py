"""Role families (Phases 245-249): products by the kind of job, not only by technology.

* **Phase 245, role families:** a posting's title puts it in a family (``FAMILIES``): AI & machine
  learning, data, DevOps & SRE, security, mobile, frontend, backend, QA & testing, engineering
  management. Whole words only, so "Android" is mobile but "Mainland" isn't AI.
* **Phase 246, role datasets** (``role-<family>``): every current posting in a family, e.g.
  "Companies Hiring AI & Machine Learning Engineers". Postings level, priced by size.
* **Phase 247, by region** (``role-<family>-<us|europe|remote>``): the same, narrowed to the US,
  Europe or remote roles, when there are enough.
* **Phase 248, top employers by family** (``role-employers-<family>``, $9): one row per company hiring
  for the family, ranked by open roles (at least ``EMPLOYERS_MIN`` companies).
* **Phase 249, family trends:** the trends page also shows new postings per role family, week by
  week (``family_counts``).
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime
from typing import Any

from strategies.product_kinds import Kind, build_companies, build_postings, company_candidate, posting_candidate, register

FAMILIES: dict[str, tuple[str, list[str]]] = {
    "ai-ml": ("AI & Machine Learning", ["machine learning", "ml", "ai", "llm", "data scientist", "nlp", "computer vision",
                                         "deep learning", "mlops"]),
    "data": ("Data", ["data engineer", "analytics engineer", "etl", "data platform", "data warehouse", "bi engineer",
                      "data analyst"]),
    "devops": ("DevOps & SRE", ["devops", "sre", "site reliability", "platform engineer", "infrastructure engineer",
                                "cloud engineer"]),
    "security": ("Security", ["security", "appsec", "penetration", "pentester", "soc analyst", "cybersecurity"]),
    "mobile": ("Mobile", ["ios", "android", "mobile", "flutter", "react native"]),
    "frontend": ("Frontend", ["frontend", "front-end", "front end", "ui engineer"]),
    "backend": ("Backend", ["backend", "back-end", "back end", "api engineer"]),
    "qa": ("QA & Testing", ["qa", "quality assurance", "test engineer", "sdet", "test automation"]),
    "management": ("Engineering Management", ["engineering manager", "head of engineering", "vp engineering",
                                              "director of engineering", "cto"]),
}
REGION_NAMES = {"us": "in the US", "europe": "in Europe", "remote": "(Remote)"}
EMPLOYERS_MIN = 10
EMPLOYERS_PRICE = 900
_PATTERNS = {fam: re.compile(r"(?<![\w-])(" + "|".join(re.escape(w) for w in words) + r")(?![\w-])", re.I)
             for fam, (_, words) in FAMILIES.items()}


# ------------------------------------------------------------------ Phase 245
def families_of(lead: dict[str, Any]) -> set[str]:
    title = str(lead.get("title") or "")
    return {fam for fam, pat in _PATTERNS.items() if pat.search(title)}


def _by_family(tagged: list[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, list[tuple[dict[str, Any], dict[str, Any]]]]:
    out: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for lead, f in tagged:
        for fam in families_of(lead):
            out[fam].append((lead, f))
    return out


# ------------------------------------------------------------------ Phases 246-247
def role_candidates(tagged: list[tuple[dict[str, Any], dict[str, Any]]], cfg: Any, state: Any) -> list[dict[str, Any]]:
    out = []
    for fam, items in _by_family(tagged).items():
        name = FAMILIES[fam][0]
        rows = [lead for lead, _ in items]
        cand = posting_candidate("role", f"role-{fam}", f"Companies Hiring {name} Engineers", rows,
                                 f"{name} roles (by job title)", cfg, {"family": fam, "group": name})
        if cand:
            out.append(cand)
        for region, words in REGION_NAMES.items():  # Phase 247
            sub = [lead for lead, f in items if region in f["regions"]]
            title = (f"Remote {name} Jobs: Companies Hiring" if region == "remote"
                     else f"Companies Hiring {name} Engineers {words}")
            cand = posting_candidate("role", f"role-{fam}-{region}", title, sub, f"{name} roles (by job title), {words}",
                                     cfg, {"family": fam, "region": region, "group": name})
            if cand:
                cand["score"] -= 3
                out.append(cand)
    return out


# ------------------------------------------------------------------ Phase 248
def employer_candidates(tagged: list[tuple[dict[str, Any], dict[str, Any]]], cfg: Any, state: Any) -> list[dict[str, Any]]:
    out = []
    for fam, items in _by_family(tagged).items():
        name = FAMILIES[fam][0]
        cand = company_candidate("role_employers", f"role-employers-{fam}", f"Top Employers Hiring {name} Engineers",
                                 [lead for lead, _ in items], f"hiring for {name} roles, ranked by open roles",
                                 EMPLOYERS_PRICE, {"family": fam, "group": name}, min_companies=EMPLOYERS_MIN)
        if cand:
            out.append(cand)
    return out


# ------------------------------------------------------------------ Phase 249
def family_counts(leads: list[dict[str, Any]], now: datetime, weeks: int) -> dict[str, list[int]]:
    from strategies.market_trends import _when

    counts: dict[str, list[int]] = defaultdict(lambda: [0] * weeks)
    for lead in leads:
        when = _when(lead)
        if when is None or when > now:
            continue
        age = int((now - when).total_seconds() // (7 * 86400))
        if age < weeks:
            for fam in families_of(lead):
                counts[FAMILIES[fam][0]][weeks - 1 - age] += 1
    return dict(counts)


register(Kind("role", "Postings by role", role_candidates, build_postings))
register(Kind("role_employers", "Top employers by role", employer_candidates, lambda c, cfg, now: build_companies(c, cfg, now),
              files=["companies.csv", "companies.json", "companies.jsonl", "schema.sql", "README.md"]))
