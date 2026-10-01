"""More product kinds for the factory (Phases 245-269), on one small framework.

Each kind is registered here with how to find its products (``candidates``), how to build one
(``build``), the files in its download and its display name. ``product_types.TYPES`` includes every
registered kind, so the factory rotates through them, refreshes them weekly and retires them like
the original six. Two shared builders cover most kinds:

* ``posting_candidate`` / ``build_postings``: a posting-level dataset (the same files as a
  technology slice) for any set of postings, e.g. "AI & Machine Learning roles in Europe".
* ``company_candidate`` / ``build_companies``: one row per employer (the same files as the top
  companies list), e.g. "employers that sponsor visas".

The kinds themselves live in ``strategies/kinds_*.py`` (one module per batch of phases).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module
from datetime import datetime
from typing import Any, Callable

POSTING_FILES = ["leads.csv", "leads-excel.csv", "leads.json", "leads.jsonl", "schema.sql", "QUALITY.md", "FIELDS.md",
                 "TOP20.md", "README.md"]
COMPANY_FILES = ["companies.csv", "companies.json", "companies.jsonl", "schema.sql", "README.md"]


@dataclass
class Kind:
    name: str
    display: str
    candidates: Callable[[list[tuple[dict[str, Any], dict[str, Any]]], Any, Any], list[dict[str, Any]]]
    build: Callable[[dict[str, Any], Any, datetime], dict[str, Any]]
    files: list[str] = field(default_factory=lambda: list(POSTING_FILES))


KINDS: dict[str, Kind] = {}


def register(kind: Kind) -> Kind:
    KINDS[kind.name] = kind
    return kind


# ------------------------------------------------------------------ shared builders
def _companies(rows: list[dict[str, Any]]) -> set[str]:
    from strategies.posting_quality import company_key

    return {company_key(r.get("company")) for r in rows if r.get("company")}


def posting_candidate(kind: str, slug: str, title: str, rows: list[dict[str, Any]], describe: str, cfg: Any,
                      filters: dict[str, Any] | None = None, min_rows: int | None = None,
                      min_companies: int | None = None) -> dict[str, Any] | None:
    """A posting-level product if it clears the quality floor (``factory_min_rows`` postings from
    ``factory_min_companies`` companies, unless a kind sets its own)."""
    cos = _companies(rows)
    if len(rows) < (min_rows or int(cfg.factory_min_rows)) or len(cos) < (min_companies or int(cfg.factory_min_companies)):
        return None
    return {"type": kind, "slug": slug, "title": title, "rows": rows, "companies": len(cos),
            "filters": {**(filters or {}), "describe": describe}, "score": len(cos)}


def build_postings(cand: dict[str, Any], cfg: Any, now: datetime) -> dict[str, Any]:
    from strategies.product_factory import slice_content

    return slice_content(cand, cfg, now)


def company_candidate(kind: str, slug: str, title: str, rows: list[dict[str, Any]], what: str, price: int,
                      filters: dict[str, Any] | None = None, min_companies: int = 10,
                      keep: Callable[[dict[str, Any]], bool] | None = None) -> dict[str, Any] | None:
    """One row per employer (agencies left out, names merged). ``what`` completes "N companies ...". """
    from strategies.product_types import companies

    cos = [c for c in companies(rows) if keep is None or keep(c)][:100]
    if len(cos) < min_companies:
        return None
    return {"type": kind, "slug": slug, "title": title, "rows": cos, "companies": len(cos),
            "filters": {**(filters or {}), "what": what, "price": price},
            "keys": {f"{kind}:{slug}:{c['company'].lower()}" for c in cos}, "score": len(cos)}


def build_companies(cand: dict[str, Any], cfg: Any, now: datetime, extra_md: dict[str, str] | None = None) -> dict[str, Any]:
    from strategies.product_types import build_company_list

    made = build_company_list({**cand, "type": "top"}, cfg, now)
    rows, what = cand["rows"], cand["filters"]["what"]
    readme = (f"# {cand['title']}\n\n{len(rows)} companies {what}, from current public job postings. Built {now:%Y-%m-%d}. "
              "One row per company: open roles, example titles and locations, remote share, stack, latest posting and a "
              "link.\n")
    made["files"]["README.md"] = readme.encode()
    for name, text in (extra_md or {}).items():
        made["files"][name] = text.encode()
    made.update(readme=readme, price_cents=int(cand["filters"]["price"]),
                summary=f"{len(rows)} companies {what}. CSV, JSON and SQL, delivered instantly.")
    return made


def all_candidates(tagged: list[tuple[dict[str, Any], dict[str, Any]]], cfg: Any, state: Any) -> dict[str, list[dict[str, Any]]]:
    out = {}
    for name, kind in KINDS.items():
        try:
            out[name] = sorted(kind.candidates(tagged, cfg, state), key=lambda c: (-c["score"], c["slug"]))
        except Exception as exc:  # noqa: BLE001 - one broken kind must not stop the factory
            state.log_error("product_factory", f"product kind {name} failed: {exc!r}")
            out[name] = []
    return out


# The kinds (each module registers its own on import).
KIND_MODULES = ("strategies.kinds_roles", "strategies.kinds_countries", "strategies.kinds_attributes", "strategies.kinds_stacks",
                "strategies.kinds_new_employers")
for _module in KIND_MODULES:
    import_module(_module)
