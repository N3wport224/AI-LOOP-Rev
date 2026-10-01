"""Countries (Phases 250-254): where the jobs are.

* **Phase 250, the country of a posting:** read from its location (``country_of``): country names
  and common spellings, US states, and the big tech cities. "Remote" alone has no country; "Remote,
  Germany" does. Whole words only, so "Indiana" isn't India.
* **Phase 251, country datasets** (``country-<code>``): every current posting in a country, e.g.
  "Companies Hiring Software Engineers in Germany".
* **Phase 252, technology by country** (``country-<code>-<tech>``): e.g. "Rust Jobs in Germany:
  Companies Hiring", when there are enough postings.
* **Phase 253, where companies are hiring:** the trends page lists the countries with the most new
  postings in the last four weeks.
* **Phase 254, a country column:** posting-level datasets now carry a ``country`` column (blank
  when the posting doesn't say).
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any

from strategies.product_kinds import Kind, build_postings, posting_candidate, register

COUNTRIES: dict[str, tuple[str, list[str]]] = {
    "us": ("the US", ["united states", "usa", "u.s.", "us", "new york", "san francisco", "seattle", "boston", "chicago",
                      "austin", "los angeles", "denver", "atlanta", "miami"]),
    "gb": ("the UK", ["united kingdom", "uk", "england", "scotland", "london", "manchester", "edinburgh", "bristol"]),
    "de": ("Germany", ["germany", "deutschland", "berlin", "munich", "münchen", "hamburg", "frankfurt", "cologne"]),
    "fr": ("France", ["france", "paris", "lyon"]),
    "nl": ("the Netherlands", ["netherlands", "amsterdam", "rotterdam", "utrecht"]),
    "es": ("Spain", ["spain", "madrid", "barcelona", "valencia"]),
    "pt": ("Portugal", ["portugal", "lisbon", "porto"]),
    "ie": ("Ireland", ["ireland", "dublin", "cork"]),
    "se": ("Sweden", ["sweden", "stockholm", "gothenburg"]),
    "pl": ("Poland", ["poland", "warsaw", "krakow", "kraków", "wroclaw"]),
    "ch": ("Switzerland", ["switzerland", "zurich", "zürich", "geneva"]),
    "it": ("Italy", ["italy", "milan", "rome"]),
    "at": ("Austria", ["austria", "vienna"]),
    "dk": ("Denmark", ["denmark", "copenhagen"]),
    "fi": ("Finland", ["finland", "helsinki"]),
    "no": ("Norway", ["norway", "oslo"]),
    "be": ("Belgium", ["belgium", "brussels"]),
    "ca": ("Canada", ["canada", "toronto", "vancouver", "montreal", "ottawa"]),
    "mx": ("Mexico", ["mexico", "méxico", "mexico city"]),
    "br": ("Brazil", ["brazil", "brasil", "são paulo", "sao paulo", "rio de janeiro"]),
    "ar": ("Argentina", ["argentina", "buenos aires"]),
    "in": ("India", ["india", "bangalore", "bengaluru", "hyderabad", "pune", "mumbai", "delhi", "chennai"]),
    "sg": ("Singapore", ["singapore"]),
    "jp": ("Japan", ["japan", "tokyo"]),
    "au": ("Australia", ["australia", "sydney", "melbourne", "brisbane"]),
    "nz": ("New Zealand", ["new zealand", "auckland", "wellington"]),
    "il": ("Israel", ["israel", "tel aviv"]),
    "ae": ("the UAE", ["united arab emirates", "uae", "dubai", "abu dhabi"]),
}
US_STATES = ("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA "
             "RI SC SD TN TX UT VT VA WA WV WI WY DC").split()
_PATTERNS = {code: re.compile(r"(?<![\w])(" + "|".join(re.escape(w) for w in words) + r")(?![\w])", re.I)
             for code, (_, words) in COUNTRIES.items() if code != "us"}
_US = re.compile(r"(?<![\w])(united states|usa|u\.s\.|new york|san francisco|seattle|boston|chicago|austin|los angeles|denver|"
                 r"atlanta|miami)(?![\w])|(?<![\w])US(?![\w])|,\s*(" + "|".join(US_STATES) + r")\b")
SUBJECT = "Software Engineers"
NEAR_DAYS = 28


# ------------------------------------------------------------------ Phase 250
def country_of(lead: dict[str, Any]) -> set[str]:
    loc = str(lead.get("location") or "")
    out = {code for code, pat in _PATTERNS.items() if pat.search(loc)}
    if _US.search(loc):
        out.add("us")
    return out


def name(code: str) -> str:
    return COUNTRIES[code][0]


def _plain(code: str) -> str:
    return name(code).removeprefix("the ")


# ------------------------------------------------------------------ Phases 251-252
def country_candidates(tagged: list[tuple[dict[str, Any], dict[str, Any]]], cfg: Any, state: Any) -> list[dict[str, Any]]:
    from strategies.product_factory import _slug, label

    by_country: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for lead, f in tagged:
        for code in country_of(lead):
            by_country[code].append((lead, f))
    out = []
    for code, items in by_country.items():
        rows = [lead for lead, _ in items]
        cand = posting_candidate("country", f"country-{code}", f"Companies Hiring {SUBJECT} in {name(code)}", rows,
                                 f"roles located in {name(code)}", cfg, {"country": code, "group": _plain(code)})
        if cand:
            out.append(cand)
        techs = Counter(t for _, f in items for t in f["techs"])
        for tech, n in techs.items():  # Phase 252
            if n < int(cfg.factory_min_rows):
                continue
            sub = [lead for lead, f in items if tech in f["techs"]]
            cand = posting_candidate("country", f"country-{code}-{_slug(tech)}",
                                     f"{label(tech)} Jobs in {name(code)}: Companies Hiring", sub,
                                     f"roles mentioning {label(tech)}, located in {name(code)}", cfg,
                                     {"country": code, "tech": tech, "group": _plain(code)})
            if cand:
                cand["score"] -= 2
                out.append(cand)
    return out


# ------------------------------------------------------------------ Phase 253
def hiring_by_country(leads: list[dict[str, Any]], now: datetime, limit: int = 15) -> list[tuple[str, int]]:
    from strategies.market_trends import _when

    since = now - timedelta(days=NEAR_DAYS)
    counts: Counter = Counter()
    for lead in leads:
        when = _when(lead)
        if when is not None and since <= when <= now:
            counts.update(country_of(lead))
    return [(_plain(code), n) for code, n in counts.most_common(limit)]


# ------------------------------------------------------------------ Phase 254
def country_column(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**r, "country": ", ".join(sorted(_plain(c) for c in country_of(r)))} for r in rows]


register(Kind("country", "Postings by country", country_candidates, build_postings))
