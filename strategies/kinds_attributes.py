"""What the job offers (Phases 255-259): products from what postings say, not only their stack.

* **Phase 255, reading the posting:** ``attributes_of`` finds, in the title, tags, location and
  description: visa sponsorship or relocation help, contract or freelance work, junior-friendly
  roles, work from anywhere, and a four-day week. A negation ("we can't sponsor visas", "no
  relocation") cancels a match.
* **Phase 256, visa sponsorship** (``visa-sponsorship[-<tech>]``): roles offering a visa or help
  relocating.
* **Phase 257, contract and freelance** (``contract-roles[-<tech>]``).
* **Phase 258, junior-friendly** (``junior-friendly-roles[-<tech>]``): junior, graduate, entry-level,
  intern and apprentice roles.
* **Phase 259, work from anywhere and four-day weeks** (``work-from-anywhere[-<tech>]``,
  ``four-day-week``): the four-day week list is rarer, so it needs ``FOUR_DAY_MIN_ROWS`` postings
  from ``FOUR_DAY_MIN_COMPANIES`` companies.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

from strategies.product_kinds import Kind, build_postings, posting_candidate, register

FOUR_DAY_MIN_ROWS = 10
FOUR_DAY_MIN_COMPANIES = 5
_VISA = re.compile(r"visa sponsorship|sponsor(?:s|ing)? (?:a |your |work )?visas?|relocation (?:package|assistance|support|help)|"
                   r"help(?: you)? relocate|we (?:will )?relocate you|\bh-?1b\b|blue card", re.I)
_NO_VISA = re.compile(r"(?:no|not|unable to|cannot|can't|can not|won't|will not|do not|don't)\s+(?:\w+\s+){0,3}"
                      r"(?:visa|sponsor|relocat)", re.I)
_CONTRACT = re.compile(r"\b(?:contract|contractor|freelance|freelancer|fixed[- ]term|b2b)\b", re.I)
_CONTRACT_DESC = re.compile(r"\b(?:contract|freelance) (?:role|position|basis|engagement)\b|\bemployment type:\s*contract", re.I)
_JUNIOR = re.compile(r"\b(?:junior|jr\.?|graduate|entry[- ]level|intern(?:ship)?|trainee|apprentice(?:ship)?)\b", re.I)
_ANYWHERE = re.compile(r"\b(?:anywhere|worldwide|work from anywhere|globally remote|global remote|fully distributed)\b", re.I)
_FOUR_DAY = re.compile(r"\b(?:4|four)[- ]day (?:work ?)?week\b|\b32[- ]hour (?:work ?)?week\b", re.I)
ATTRS = {
    "visa": ("visa-sponsorship", "Developer Jobs With Visa Sponsorship or Relocation", "roles offering visa sponsorship or relocation help"),
    "contract": ("contract-roles", "Contract & Freelance Developer Roles", "contract and freelance roles"),
    "junior": ("junior-friendly-roles", "Junior-Friendly Developer Jobs", "junior, graduate and entry-level roles"),
    "anywhere": ("work-from-anywhere", "Work-From-Anywhere Developer Jobs", "remote roles open worldwide"),
    "four_day": ("four-day-week", "Developer Jobs With a Four-Day Week", "roles offering a four-day week"),
}


# ------------------------------------------------------------------ Phase 255
def attributes_of(lead: dict[str, Any]) -> set[str]:
    title = str(lead.get("title") or "")
    tags = " ".join(map(str, lead.get("tags") or []))
    desc = str(lead.get("description") or "")
    loc = str(lead.get("location") or "")
    text = f"{title} {tags} {desc}"
    out = set()
    if _VISA.search(text) and not _NO_VISA.search(text):
        out.add("visa")
    if _CONTRACT.search(f"{title} {tags}") or _CONTRACT_DESC.search(desc):
        out.add("contract")
    if _JUNIOR.search(title) or str(lead.get("seniority") or "") == "junior":
        out.add("junior")
    remote = str(lead.get("remote")).lower() in ("true", "1", "yes") or "remote" in loc.lower()
    if remote and (_ANYWHERE.search(loc) or _ANYWHERE.search(desc)):
        out.add("anywhere")
    if _FOUR_DAY.search(text):
        out.add("four_day")
    return out


# ------------------------------------------------------------------ Phases 256-259
def attribute_candidates(tagged: list[tuple[dict[str, Any], dict[str, Any]]], cfg: Any, state: Any) -> list[dict[str, Any]]:
    from strategies.product_factory import _slug, label

    by_attr: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for lead, f in tagged:
        for attr in attributes_of(lead):
            by_attr[attr].append((lead, f))
    out = []
    for attr, items in by_attr.items():
        slug, title, describe = ATTRS[attr]
        floors = {"min_rows": FOUR_DAY_MIN_ROWS, "min_companies": FOUR_DAY_MIN_COMPANIES} if attr == "four_day" else {}
        cand = posting_candidate("attribute", slug, title, [lead for lead, _ in items], describe, cfg,
                                 {"attribute": attr, "group": title.split(" Developer")[0].split(" Jobs")[0]}, **floors)
        if cand:
            out.append(cand)
        if attr == "four_day":
            continue
        for tech, n in Counter(t for _, f in items for t in f["techs"]).items():
            if n < int(cfg.factory_min_rows):
                continue
            sub = [lead for lead, f in items if tech in f["techs"]]
            cand = posting_candidate("attribute", f"{slug}-{_slug(tech)}", title.replace("Developer", label(tech), 1), sub,
                                     f"{describe}, mentioning {label(tech)}", cfg, {"attribute": attr, "tech": tech})
            if cand:
                cand["score"] -= 2
                out.append(cand)
    return out


register(Kind("attribute", "What the job offers", attribute_candidates, build_postings))
