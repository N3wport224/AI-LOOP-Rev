"""Quality checks (Phases 315-319): ``automonetize qa`` (and weekly, in housekeeping).

Each check returns a list of problems (empty when all is well):

* **Phase 315, downloads:** every product on sale has a download that opens, lists its files in
  ``SHA256SUMS`` with matching hashes, and includes a README.
* **Phase 316, rows:** each download's main CSV parses and holds as many rows as the product says.
* **Phase 317, pages:** every built HTML page has a ``lang``, a ``<title>``, a meta description and one
  ``<h1>`` (redirect, 404 and no-index helper pages excepted).
* **Phase 318, structured data:** every JSON-LD block on the site parses and has a ``@type``.
* **Phase 319, parsers survive junk:** each job-board parser is fed random and truncated payloads; it
  must return a list (or refuse the payload with ``ValueError``), never crash in another way.

Weekly, housekeeping runs 315-318 and raises one alert listing the first problems.
"""

from __future__ import annotations

import csv
import io
import json
import random
import re
import zipfile
from datetime import datetime, timedelta
from typing import Any, Callable

KEY = "qa_last"
PRIMARY = ("leads.csv", "companies.csv", "salaries.csv")
_JSONLD = re.compile(r'<script type="application/ld\+json">\s*(.*?)\s*</script>', re.S)
SKIP_PAGES = ("404.html",)


# ------------------------------------------------------------------ Phases 315-316
def check_downloads(state: Any, files: Any) -> list[str]:
    from strategies.download_extras import verify_zip
    from strategies.product_factory import ensure

    ensure(state)
    problems = []
    rows = state._all("SELECT f.slug AS slug, f.rows AS rows, f.filters AS filters, a.path AS path FROM factory_products f "
                      "JOIN assets a ON a.id = f.asset_id WHERE f.status = 'live'")
    for r in rows:
        if not r["path"] or not files.exists(r["path"]):
            problems.append(f"{r['slug']}: download missing")
            continue
        data = files.read_bytes(r["path"])
        if not verify_zip(data):
            problems.append(f"{r['slug']}: download damaged (checksums)")
            continue
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = {n.split("/", 1)[-1]: n for n in zf.namelist()}
            if "README.md" not in names:
                problems.append(f"{r['slug']}: no README.md")
            kind = json.loads(r["filters"]).get("type", "slice")
            primary = next((names[p] for p in PRIMARY if p in names), None)
            if kind != "pack" and primary is None:
                problems.append(f"{r['slug']}: no CSV")
                continue
            if primary and kind != "salary":
                text = zf.read(primary).decode("utf-8-sig")
                parsed = list(csv.DictReader(io.StringIO(text)))
                if len(parsed) != int(r["rows"]):
                    problems.append(f"{r['slug']}: CSV has {len(parsed)} rows, the product says {r['rows']}")
    return problems


# ------------------------------------------------------------------ Phases 317-318
def check_pages(files: Any, root: str = "site") -> list[str]:
    if not files.exists(root):
        return []
    problems = []
    base = files.resolve(root)
    for path in sorted(base.rglob("*.html")):
        rel = str(path.relative_to(base))
        page = path.read_text(encoding="utf-8", errors="replace")
        noindex = 'name="robots" content="noindex"' in page
        if rel not in SKIP_PAGES and not noindex and "http-equiv=\"refresh\"" not in page:
            missing = [what for what, ok in (("lang", "<html lang=" in page), ("title", "<title>" in page),
                                             ("description", 'name="description"' in page),
                                             ("one h1", page.count("<h1") == 1)) if not ok]
            if missing:
                problems.append(f"{rel}: missing {', '.join(missing)}")
        for block in _JSONLD.findall(page):
            try:
                data = json.loads(block)
            except ValueError:
                problems.append(f"{rel}: JSON-LD doesn't parse")
                continue
            items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
            if not all(isinstance(i, dict) and i.get("@type") for i in items):
                problems.append(f"{rel}: JSON-LD without @type")
    return problems


# ------------------------------------------------------------------ Phase 319
def _junk(rng: random.Random, depth: int = 0) -> Any:
    kind = rng.choice(["str", "int", "none", "list", "dict", "float", "bool"] if depth < 3 else ["str", "int", "none"])
    if kind == "str":
        return rng.choice(["", "x", "<b>hi</b>", "2026-13-45", "€", "a" * 300, "null"])
    if kind == "int":
        return rng.choice([0, -1, 10**12])
    if kind == "float":
        return rng.choice([0.5, float("1e308")])
    if kind == "bool":
        return rng.random() < 0.5
    if kind == "none":
        return None
    if kind == "list":
        return [_junk(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    keys = ["jobs", "data", "title", "company_name", "companyName", "jobTitle", "salary", "tags", "location", "url", "id",
            "publication_date", "pubDate", "minSalary", "categories", "date", "company", "remote"]
    return {rng.choice(keys): _junk(rng, depth + 1) for _ in range(rng.randint(0, 6))}


def parsers() -> dict[str, Callable[[Any], Any]]:
    from strategies import b2b_lead_aggregator as agg
    from strategies import job_sources as js

    return {"remoteok": agg.parse_remoteok, "arbeitnow": agg.parse_arbeitnow, "remotive": js.parse_remotive,
            "jobicy": js.parse_jobicy, "himalayas": js.parse_himalayas,
            "hn_hiring": lambda p: agg.parse_hn_comment(p if isinstance(p, dict) else {}),
            "weworkremotely": lambda p: js.parse_wwr(p if isinstance(p, str) else json.dumps(p))}


def fuzz_parsers(rounds: int = 300, seed: int = 7) -> list[str]:
    rng = random.Random(seed)
    problems = []
    for name, parse in parsers().items():
        for i in range(rounds):
            payload = _junk(rng)
            if name == "weworkremotely" and i % 3 == 0:
                payload = "<rss><channel><item><title>A: B</title><link>x</link></item>"[: rng.randint(0, 60)]
            try:
                out = parse(payload)
            except ValueError:
                continue  # refusing a payload is fine (the aggregator records it and moves on)
            except Exception as exc:  # noqa: BLE001 - this is what the check is looking for
                problems.append(f"{name}: {type(exc).__name__}: {exc}"[:200])
                break
            if out is not None and not isinstance(out, list) and name != "hn_hiring":
                problems.append(f"{name}: returned {type(out).__name__}, not a list")
                break
    return problems


# ------------------------------------------------------------------ all, and weekly
def run_all(state: Any, files: Any, fuzz: bool = True) -> dict[str, list[str]]:
    out = {"downloads": check_downloads(state, files), "pages": check_pages(files)}
    if fuzz:
        out["parsers"] = fuzz_parsers()
    return out


def weekly(state: Any, files: Any) -> list[str]:
    last = state.get(KEY) or {}
    if last.get("at") and state.clock() - datetime.fromisoformat(last["at"]) < timedelta(days=7):
        return []
    results = run_all(state, files, fuzz=False)
    problems = [p for items in results.values() for p in items]
    state.set(KEY, {"at": state.now(), "problems": len(problems), "first": problems[:5]})
    if problems:
        state.log_error("qa", f"Quality check: {len(problems)} problem(s), e.g. {problems[0]}. Details: automonetize qa",
                        kind="alert")
    return problems
