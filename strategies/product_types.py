"""More product types for the factory (Phases 150-154).

The factory (``strategies/product_factory.py``) rotates between these, so the catalog grows in
several directions instead of only narrower slices:

* **Phase 150, salary benchmarks** (``salary-<tech>``): postings that state a salary, with the
  median and middle half by seniority and region. Salaries are as posted (yearly figures between
  10k and 1M only, so hourly rates don't skew them; currencies aren't converted, which the
  README says). Needs ``SALARY_MIN_ROWS`` postings with a salary.
* **Phase 151, top companies** (``top-companies-<tech>``): one row per company, ranked by open
  roles, with titles, locations, remote share and stack. Needs ``TOP_MIN_COMPANIES`` companies.
* **Phase 152, remote-first employers** (``remote-first-employers[-<tech>]``): companies with at
  least 3 postings, 70%+ of them remote.
* **Phase 153, starter packs** (``pack-<tech>``): three or four live products about the same
  technology in one download at 30% off their total.
* **Phase 154, weekly refresh:** every live factory product is rebuilt from fresh postings once a
  week (a few per tick). The new version replaces the old one behind the same checkout, so buyers
  always get current data; a product whose data has dried up is left for retirement.
"""

from __future__ import annotations

import csv
import io
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from statistics import median, quantiles
from typing import Any

from strategies.product_kinds import KINDS  # noqa: E402 - Phases 245-269: more kinds, registered there

TYPES = ("slice", "salary", "top", "remote_first", "fast_hiring", "pack") + tuple(KINDS)
TURN = "factory_turn"
SALARY_MIN_ROWS = 15
TOP_MIN_COMPANIES = 25
REMOTE_MIN_COMPANIES = 12
PRICES = {"salary": 700, "top": 900, "remote_first": 900}
REFRESH_DAYS = 7
REFRESH_PER_TICK = 3


def _label(tech: str) -> str:
    from strategies.product_factory import label

    return label(tech)


def _slug(text: str) -> str:
    from strategies.product_factory import _slug as slug

    return slug(text)


def _csv(rows: list[dict[str, Any]], fields: list[str]) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({f: ", ".join(map(str, r[f])) if isinstance(r.get(f), list) else r.get(f) for f in fields})
    return buf.getvalue().encode()


def _company(r: dict[str, Any]) -> str:
    return " ".join(str(r.get("company") or "").split())


def _is_remote(r: dict[str, Any]) -> bool:
    return str(r.get("remote")).lower() in ("true", "1", "yes") or "remote" in str(r.get("location") or "").lower()


def _by_tech(tagged: list[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for lead, f in tagged:
        for t in f["techs"]:
            out[t].append(lead)
    return out


# ------------------------------------------------------------------ Phase 150: salaries
def _yearly(v: Any) -> int | None:
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return None
    return n if 10_000 <= n <= 1_000_000 else None


def salary_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        lo, hi = _yearly(r.get("salary_min")), _yearly(r.get("salary_max"))
        if lo or hi:
            out.append({**r, "salary_mid": round(((lo or hi) + (hi or lo)) / 2)})
    return out


def _band(values: list[int]) -> str:
    if len(values) < 4:
        return f"median {median(values):,.0f} ({len(values)} postings)" if values else "-"
    q = quantiles(values, n=4)
    return f"median {median(values):,.0f}, middle half {q[0]:,.0f}-{q[2]:,.0f} ({len(values)} postings)"


def salary_md(title: str, rows: list[dict[str, Any]]) -> str:
    from strategies.dataset_extras import region_of

    lines = [f"# {title}", "", "Salaries as posted: yearly figures only, in the posting's currency (not converted).", "",
             f"**All postings:** {_band([r['salary_mid'] for r in rows])}", "", "## By seniority", ""]
    levels: dict[str, list[int]] = defaultdict(list)
    regions: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        levels[str(r.get("seniority") or "mid")].append(r["salary_mid"])
        for g in region_of(r) or {"other"}:
            regions[g].append(r["salary_mid"])
    lines += [f"- {k}: {_band(v)}" for k, v in sorted(levels.items())]
    lines += ["", "## By region", ""] + [f"- {k}: {_band(v)}" for k, v in sorted(regions.items())]
    return "\n".join(lines) + "\n"


def salary_candidates(tagged: list[tuple[dict[str, Any], dict[str, Any]]]) -> list[dict[str, Any]]:
    out = []
    for tech, rows in _by_tech(tagged).items():
        paid = salary_rows(rows)
        if len(paid) < SALARY_MIN_ROWS:
            continue
        out.append({"type": "salary", "slug": f"salary-{_slug(tech)}", "title": f"{_label(tech)} Engineer Salary Benchmarks",
                    "filters": {"tech": tech}, "rows": paid, "companies": len({_company(r) for r in paid}),
                    "keys": {f"salary:{r.get('dedupe_key')}" for r in paid}, "score": len(paid)})
    return out


def build_salary(cand: dict[str, Any], cfg: Any, now: datetime) -> dict[str, Any]:
    rows = sorted(cand["rows"], key=lambda r: -r["salary_mid"])
    fields = ["company", "title", "seniority", "location", "remote", "salary_min", "salary_max", "salary_mid", "url", "posted_at"]
    readme = (f"# {cand['title']}\n\n{len(rows)} current postings that state a salary, from {cand['companies']} companies. "
              f"Built {now:%Y-%m-%d}. `SALARIES.md` has the benchmarks; `salaries.csv` every posting behind them.\n")
    return {"files": {"README.md": readme.encode(), "SALARIES.md": salary_md(cand["title"], rows).encode(),
                      "salaries.csv": _csv(rows, fields)},
            "readme": readme, "rows": len(rows), "price_cents": PRICES["salary"], "insight": _insight(rows),
            "summary": f"Salary benchmarks from {len(rows)} current {_label(cand['filters']['tech'])} postings with a stated "
                       "salary: median and range by seniority and region, plus every posting.",
            "preview_fields": ["company", "title", "seniority", "salary_min", "salary_max"],
            "preview": [{f: r.get(f) for f in ("company", "title", "seniority", "salary_min", "salary_max")} for r in rows[:5]]}


# ------------------------------------------------------------------ Phase 151-152: company lists
def companies(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per employer: name variants merged (Phase 231), staffing agencies left out (Phase 232)."""
    from strategies.posting_quality import company_key, is_agency

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if _company(r) and not is_agency(_company(r)):
            groups[company_key(_company(r))].append(r)
    out = []
    for _key, posts in groups.items():
        name = Counter(_company(p) for p in posts).most_common(1)[0][0]
        stack = Counter(t for p in posts for t in (p.get("stack") or []))
        out.append({"company": name, "open_roles": len(posts), "titles": sorted({str(p.get("title") or "") for p in posts})[:5],
                    "locations": sorted({str(p.get("location") or "") for p in posts if p.get("location")})[:5],
                    "remote_share": round(sum(_is_remote(p) for p in posts) / len(posts), 2),
                    "stack": [t for t, _ in stack.most_common(8)],
                    "latest_posting": max(str(p.get("posted_at") or "") for p in posts)[:10],
                    "example_url": next((p.get("url") for p in posts if p.get("url")), "")})
    return sorted(out, key=lambda c: (-c["open_roles"], c["company"]))


COMPANY_FIELDS = ["company", "open_roles", "titles", "locations", "remote_share", "stack", "latest_posting", "example_url"]


def top_candidates(tagged: list[tuple[dict[str, Any], dict[str, Any]]]) -> list[dict[str, Any]]:
    out = []
    for tech, rows in _by_tech(tagged).items():
        cos = companies(rows)
        if len(cos) < TOP_MIN_COMPANIES:
            continue
        n = min(50, len(cos))
        out.append({"type": "top", "slug": f"top-companies-{_slug(tech)}", "title": f"Top {n} Companies Hiring {_label(tech)} Engineers",
                    "filters": {"tech": tech}, "rows": cos[:n], "companies": n,
                    "keys": {f"top:{c['company'].lower()}" for c in cos[:n]}, "score": n})
    return out


def remote_first_candidates(tagged: list[tuple[dict[str, Any], dict[str, Any]]]) -> list[dict[str, Any]]:
    out = []
    groups = [("", [lead for lead, _ in tagged])] + sorted(_by_tech(tagged).items())
    for tech, rows in groups:
        cos = [c for c in companies(rows) if c["open_roles"] >= 3 and c["remote_share"] >= 0.7]
        if len(cos) < REMOTE_MIN_COMPANIES:
            continue
        slug = "remote-first-employers" + (f"-{_slug(tech)}" if tech else "")
        title = "Remote-First Employers Hiring Now" if not tech else f"Remote-First Employers Hiring {_label(tech)} Engineers"
        out.append({"type": "remote_first", "slug": slug, "title": title, "filters": {"tech": tech}, "rows": cos,
                    "companies": len(cos), "keys": {f"remote:{tech}:{c['company'].lower()}" for c in cos}, "score": len(cos)})
    return out


def build_company_list(cand: dict[str, Any], cfg: Any, now: datetime) -> dict[str, Any]:
    from strategies.dataset_extras import jsonl, schema_sql

    rows = cand["rows"]
    kind = "remote-first employers (3+ open roles, 70%+ remote)" if cand["type"] == "remote_first" else "companies ranked by open roles"
    readme = (f"# {cand['title']}\n\n{len(rows)} {kind}, from current public job postings. Built {now:%Y-%m-%d}. One row "
              "per company: open roles, example titles and locations, remote share, stack, latest posting and a link.\n")
    return {"files": {"README.md": readme.encode(), "companies.csv": _csv(rows, COMPANY_FIELDS),
                      "companies.json": json.dumps(rows, indent=2).encode(), "companies.jsonl": jsonl(rows, COMPANY_FIELDS).encode(),
                      "schema.sql": schema_sql(rows, COMPANY_FIELDS, table="companies").encode()},
            "readme": readme, "rows": len(rows), "price_cents": PRICES[cand["type"]],
            "insight": {"rows": len(rows), "companies": len(rows), "remote_pct": round(100 * sum(r["remote_share"] for r in rows)
                                                                                      / len(rows)) if rows else 0,
                        "top_companies": [(r["company"], r["open_roles"]) for r in rows[:5]], "top_locations": []},
            "summary": f"{len(rows)} {kind}: open roles, titles, locations, remote share and stack. CSV, JSON and SQL.",
            "preview_fields": ["company", "open_roles", "remote_share", "stack"],
            "preview": [{f: (r[f][:4] if isinstance(r[f], list) else r[f]) for f in ("company", "open_roles", "remote_share", "stack")}
                        for r in rows[:5]]}


# ------------------------------------------------------------------ Phase 153: starter packs
def pack_candidates(state: Any) -> list[dict[str, Any]]:
    from strategies.product_factory import ensure

    ensure(state)
    live = state._all("SELECT slug, title, filters, asset_id FROM factory_products WHERE status = 'live'")
    by_tech: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in live:
        f = json.loads(row["filters"])
        if f.get("type", "slice") != "pack" and f.get("tech"):
            by_tech[f["tech"]].append(row)
    out = []
    for tech, rows in by_tech.items():
        if len(rows) < 3:
            continue
        members = sorted(rows, key=lambda r: r["slug"])[:4]
        out.append({"type": "pack", "slug": f"pack-{_slug(tech)}", "title": f"{_label(tech)} Hiring Starter Pack",
                    "filters": {"tech": tech, "members": [m["slug"] for m in members]}, "rows": [], "members": members,
                    "companies": 0, "keys": {f"pack:{m['slug']}" for m in members}, "score": len(members)})
    return out


def build_pack(cand: dict[str, Any], cfg: Any, now: datetime, state: Any = None, files: Any = None) -> dict[str, Any]:
    import zipfile

    content, total, titles = {}, 0, []
    for m in cand["members"]:
        asset = state.get_asset(int(m["asset_id"])) or {}
        total += int(asset.get("price_cents") or 0)
        titles.append(m["title"])
        with zipfile.ZipFile(io.BytesIO(files.read_bytes(asset["path"]))) as zf:
            for name in zf.namelist():
                content[name] = zf.read(name)
    price = max(500, int(total * 0.7) // 100 * 100)
    readme = (f"# {cand['title']}\n\n{len(titles)} datasets in one download, 30% off buying them separately:\n\n"
              + "\n".join(f"- {t}" for t in titles) + f"\n\nBuilt {now:%Y-%m-%d}. Each folder is one dataset.\n")
    content["README.md"] = readme.encode()
    return {"files": content, "readme": readme, "rows": len(titles), "price_cents": price,
            "summary": f"{len(titles)} {_label(cand['filters']['tech'])} datasets in one download, 30% off: " + "; ".join(titles),
            "preview_fields": ["dataset"], "preview": [{"dataset": t} for t in titles]}


def _insight(rows: list[dict[str, Any]]) -> dict[str, Any]:
    from tools.seo_scale import insight

    return insight(rows)


def _build_fast(cand: dict[str, Any], cfg: Any, now: datetime) -> dict[str, Any]:
    from strategies.market_trends import build_fast_hiring

    return build_fast_hiring(cand, cfg, now)


BUILDERS = {"salary": build_salary, "top": build_company_list, "remote_first": build_company_list, "fast_hiring": _build_fast,
            **{name: kind.build for name, kind in KINDS.items()}}


# ------------------------------------------------------------------ rotation
def all_candidates(state: Any, cfg: Any) -> dict[str, list[dict[str, Any]]]:
    from strategies.product_factory import candidates, facets, fresh_leads

    leads = fresh_leads(state, int(cfg.factory_max_age_days))
    tagged = [(lead, facets(lead)) for lead in leads]
    from strategies.market_trends import fast_hiring_candidates, favour_rising, rising_techs

    by_type = {"slice": candidates(state, cfg, leads), "salary": salary_candidates(tagged), "top": top_candidates(tagged),
               "remote_first": remote_first_candidates(tagged), "fast_hiring": fast_hiring_candidates(tagged, state.clock()),
               "pack": pack_candidates(state)}
    for kind in ("salary", "top", "remote_first", "fast_hiring", "pack"):
        by_type[kind].sort(key=lambda c: (-c["score"], c["slug"]))
    from strategies.product_kinds import all_candidates as kind_candidates

    by_type.update(kind_candidates(tagged, cfg, state))
    rising = rising_techs(state)  # Phase 223: within each type, rising technologies first (slices: a score bonus)
    return {kind: cands if kind == "slice" else favour_rising(cands, rising) for kind, cands in by_type.items()}


def rotation(state: Any) -> list[str]:
    turn = int(state.get(TURN) or 0) % len(TYPES)
    return list(TYPES[turn:] + TYPES[:turn])


def advance(state: Any, kind: str) -> None:
    state.set(TURN, (TYPES.index(kind) + 1) % len(TYPES))


# ------------------------------------------------------------------ Phase 154: refresh
def refresh_due(tools: Any, limit: int = REFRESH_PER_TICK) -> list[str]:
    from strategies import product_factory as pf

    state, cfg = tools.state, tools.config
    pf.ensure(state)
    cutoff = (state.clock() - timedelta(days=REFRESH_DAYS)).isoformat(timespec="seconds")
    due = state._all("SELECT * FROM factory_products WHERE status = 'live' AND COALESCE(refreshed_at, published_at) < ? "
                     "ORDER BY COALESCE(refreshed_at, published_at) LIMIT ?", (cutoff, limit))
    if not due:
        return []
    fresh = all_candidates(state, cfg)
    done = []
    for row in due:
        filters = json.loads(row["filters"])
        kind = filters.get("type", "slice")
        cand = next((c for c in fresh.get(kind, []) if c["slug"] == row["slug"]), None)
        if cand is None:
            state._exec("UPDATE factory_products SET refreshed_at = ? WHERE slug = ?", (state.now(), row["slug"]))
            continue  # the data dried up: nothing to refresh; retirement decides later
        asset = state.get_asset(int(row["asset_id"])) or {}
        version = int(asset.get("version") or 1) + 1
        if kind == "pack":
            made = build_pack(cand, cfg, state.clock(), state, tools.files)
        else:
            made = pf.content_for(cand, cfg, state.clock())
        from strategies import catalog_hygiene as ch

        current = int(asset.get("price_cents") or made["price_cents"])
        made["price_cents"] = current  # same checkout, same price...
        checkout = {"checkout_url": asset.get("checkout_url") or "", "product_ref": asset.get("product_ref")}
        from strategies.product_controls import pinned

        higher = current if pinned(state, row["slug"]) else ch.next_price(state, row["slug"], current)  # Phase 280
        if higher > current and asset.get("checkout_url"):  # ...unless it's selling well (Phase 212)
            new = ch.reprice(tools, asset, row["title"], made["summary"], higher)
            if new:
                checkout = {"checkout_url": new["checkout_url"], "product_ref": new["product_ref"]}
                made["price_cents"] = higher
        from strategies.version_diffs import on_refresh

        on_refresh(tools, row["slug"], row["title"], str(asset.get("path") or ""), made, version)  # Phases 275-277
        zip_rel = pf.write_files(tools, row["slug"], version, made, row["title"], filters)
        aid = state.add_asset(asset.get("hypothesis_id"), pf.KIND, row["title"], zip_rel, version, made["rows"], made["price_cents"],
                              product_ref=checkout["product_ref"])
        state.update_asset(aid, niche=row["slug"], status="published", provider=asset.get("provider") or "stripe",
                           checkout_url=checkout["checkout_url"])
        if checkout["product_ref"]:
            ch.sync_description(tools, str(checkout["product_ref"]), made["summary"])  # Phase 214
        state.update_asset(int(asset["id"]), status="superseded")
        state._exec("UPDATE factory_products SET asset_id = ?, rows = ?, refreshed_at = ? WHERE slug = ?",
                    (aid, made["rows"], state.now(), row["slug"]))
        pf.record_freshness(state, row["slug"], made.get("freshness") or {})
        done.append(row["slug"])
    return done
