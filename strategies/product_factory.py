"""Product factory (Phases 145-149): a new product every ``factory_interval_seconds`` (10 minutes).

Instead of one dataset per niche, the factory slices everything collected (every valid job
posting from every board, the ``__all__`` pool) into narrower datasets people search for:

* **Phase 145, candidates:** every technology the postings mention, alone and crossed with a region
  (US, Europe), remote-only, and seniority (senior, junior): "Companies hiring Rust engineers in
  Europe", "Remote senior Kubernetes roles" and so on. Each slice is scored by its size and by
  what customers asked for (``strategies/customer_requests.py``).
* **Phase 146, build:** the slice becomes a zip like the main datasets (CSV, Excel CSV, JSON,
  JSONL, SQL, QUALITY.md, FIELDS.md, TOP20.md, README), plus a 5-row public preview.
* **Phase 147, publish:** a Stripe product and Payment Link (priced by size, ``factory_prices``),
  a product page on the site, and an entry in the catalog (kind ``micro``). Orders are delivered
  like any other dataset.
* **Phase 148, cadence:** under ``automonetize supervise`` a ``factory`` worker makes one product
  every ``factory_interval_seconds``; the engine's ``run_factory`` task catches up if that worker
  isn't running (``automonetize run``). ``automonetize factory`` shows the catalog and can make
  one now.
* **Phase 149, quality floor:** a slice needs ``factory_min_rows`` postings from
  ``factory_min_companies`` companies, posted in the last ``factory_max_age_days``; one that is
  nearly the same as an existing product (85% Jaccard overlap of postings) is skipped; at most
  ``factory_max_live`` products are on sale; a product with no sale after ``factory_retire_days``
  is retired (link deactivated, page removed) to keep the catalog worth browsing. When no slice
  qualifies the factory simply waits for more data: it never pads.

State: table ``factory_products``.
"""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from strategies.b2b_lead_aggregator import EXPORT_FIELDS, POOL_NICHE, TECH_KEYWORDS
from strategies.base import Strategy, TaskContext, TaskResult

KIND = "micro"
OVERLAP_MAX = 0.85
REGIONS = {"us": "in the US", "europe": "in Europe", "remote": "(remote)"}
LEVELS = {"senior": "senior", "junior": "junior"}
TECH_LABEL = {"next.js": "Next.js", "c++": "C++", "c#": "C#", ".net": ".NET", "node": "Node.js", "golang": "Go",
              "gcp": "GCP", "aws": "AWS", "llm": "LLM", "sre": "SRE", "ios": "iOS", "php": "PHP", "devops": "DevOps",
              "graphql": "GraphQL", "postgres": "Postgres", "mongodb": "MongoDB", "javascript": "JavaScript",
              "typescript": "TypeScript", "fastapi": "FastAPI", "pytorch": "PyTorch", "tensorflow": "TensorFlow",
              "machine learning": "Machine Learning"}
_SCHEMA = """CREATE TABLE IF NOT EXISTS factory_products (
    slug TEXT PRIMARY KEY,
    asset_id INTEGER,
    title TEXT NOT NULL,
    filters TEXT NOT NULL,
    rows INTEGER NOT NULL,
    companies INTEGER NOT NULL,
    keys_sample TEXT NOT NULL,
    status TEXT NOT NULL,              -- staged | live | retired
    created_at TEXT NOT NULL,
    published_at TEXT,
    retired_at TEXT
)"""


def ensure(state: Any) -> None:
    state._exec(_SCHEMA)
    cols = {r["name"] for r in state._all("PRAGMA table_info(factory_products)")}
    if "refreshed_at" not in cols:  # Phase 154
        state._exec("ALTER TABLE factory_products ADD COLUMN refreshed_at TEXT")
    state._exec("CREATE INDEX IF NOT EXISTS idx_factory_status ON factory_products (status, published_at)")  # Phase 216


def label(tech: str) -> str:
    return TECH_LABEL.get(tech, tech.title() if len(tech) > 3 else tech.upper())


def _slug(text: str) -> str:
    t = text.lower().replace("+", "plus").replace("#", "sharp").replace(".", "dot")
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")


# ------------------------------------------------------------------ Phase 145: candidates
def facets(lead: dict[str, Any]) -> dict[str, Any]:
    from strategies.dataset_extras import region_of

    return {"techs": {str(t).lower() for t in (lead.get("stack") or []) if str(t).lower() in TECH_KEYWORDS},
            "regions": region_of(lead), "level": str(lead.get("seniority") or "")}


def fresh_leads(state: Any, max_age_days: int) -> list[dict[str, Any]]:
    cutoff = state.clock() - timedelta(days=max_age_days)
    out = []
    for lead in state.leads_for_niche(POOL_NICHE):
        try:
            posted = datetime.fromisoformat(str(lead.get("posted_at") or "").replace("Z", "+00:00"))
            if posted.tzinfo is None:
                posted = posted.replace(tzinfo=timezone.utc)
            if posted < cutoff:
                continue
        except ValueError:
            pass  # no date: keep (the board didn't say)
        out.append(lead)
    from strategies.posting_quality import drop_unlisted

    from strategies.opt_out import drop_excluded

    return drop_excluded(state, drop_unlisted(out))  # Phases 230, 287


def slice_spec(tech: str, region: str = "", level: str = "") -> dict[str, Any]:
    title = f"Companies Hiring {'Senior ' if level == 'senior' else 'Junior ' if level == 'junior' else ''}{label(tech)} Engineers"
    if region in ("us", "europe"):
        title += f" {REGIONS[region]}"
    elif region == "remote":
        title = "Remote " + title.replace("Companies Hiring", "Companies Hiring for")
    slug = "hiring-" + "-".join(_slug(p) for p in [tech, region, level] if p)
    return {"slug": slug, "title": title, "filters": {"tech": tech, "region": region, "level": level}}


def matches(lead_facets: dict[str, Any], filters: dict[str, str]) -> bool:
    if filters["tech"] not in lead_facets["techs"]:
        return False
    if filters.get("region") and filters["region"] not in lead_facets["regions"]:
        return False
    return not filters.get("level") or lead_facets["level"] == filters["level"]


def candidates(state: Any, cfg: Any, leads: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Every slice that clears the quality floor, best first (not yet filtered for what exists)."""
    leads = leads if leads is not None else fresh_leads(state, int(cfg.factory_max_age_days))
    tagged = [(lead, facets(lead)) for lead in leads]
    tech_counts = Counter(t for _, f in tagged for t in f["techs"])
    asked = _asked(state)
    from strategies.posting_quality import company_key

    from strategies.upsells import selling_techs

    selling = selling_techs(state, cfg)  # Phase 162: technologies that sell get more products
    from strategies.market_trends import RISING_BONUS, rising_techs

    rising = rising_techs(state)  # Phase 223
    out = []
    for tech, n in tech_counts.items():
        if n < int(cfg.factory_min_rows):
            continue
        for region in ("", "us", "europe", "remote"):
            for level in ("", "senior", "junior"):
                spec = slice_spec(tech, region, level)
                rows = [lead for lead, f in tagged if matches(f, spec["filters"])]
                companies = {company_key(r.get("company")) for r in rows if r.get("company")}  # Phase 231
                if len(rows) < int(cfg.factory_min_rows) or len(companies) < int(cfg.factory_min_companies):
                    continue
                score = (len(companies) + (50 if tech in asked else 0) + 20 * selling.get(tech, 0)
                         + (RISING_BONUS if tech in rising else 0) - (5 if level else 0) - (3 if region else 0))
                out.append({**spec, "rows": rows, "companies": len(companies), "score": score})
    return sorted(out, key=lambda c: (-c["score"], c["slug"]))


def _asked(state: Any) -> set[str]:
    from strategies.customer_requests import TOPICS

    return {str(t).lower() for t, n in (state.get(TOPICS) or {}).items() if int(n) >= 1}


# ------------------------------------------------------------------ Phase 149: quality floor
def existing(state: Any) -> list[dict[str, Any]]:
    ensure(state)
    return state._all("SELECT * FROM factory_products ORDER BY created_at")


def key_sets(made: list[dict[str, Any]]) -> list[tuple[str, set[str]]]:
    """Each live or staged product's postings, parsed once per tick (not once per candidate)."""
    return [(m["slug"], set(json.loads(m["keys_sample"]))) for m in made if m["status"] != "retired"]


def overlaps(keys: set[str], made: list[dict[str, Any]] | list[tuple[str, set[str]]]) -> str | None:
    pairs = key_sets(made) if made and isinstance(made[0], dict) else made
    for slug, other in pairs:
        # Jaccard: near-identical sets only. A sub-slice ("Rust in Europe" inside "Rust") is a
        # different product for a different buyer, so containment alone doesn't count.
        if other and keys and len(keys & other) / len(keys | other) >= OVERLAP_MAX:
            return slug
    return None


def next_candidate(state: Any, cfg: Any, peek: bool = False) -> dict[str, Any] | None:
    """The best new product, taking product types in turn (slices, salaries, top companies,
    remote-first employers, starter packs) so the catalog grows in every direction."""
    from strategies import product_types

    made = existing(state)
    taken = {m["slug"] for m in made}
    live = sum(1 for m in made if m["status"] in ("live", "staged"))
    if live >= int(cfg.factory_max_live):
        return None
    by_type = product_types.all_candidates(state, cfg)
    made_keys = key_sets(made)
    from strategies.catalog_insight import enabled_types

    allowed = set(enabled_types(cfg))  # Phase 239
    for kind in product_types.rotation(state):
        if kind not in allowed:
            continue
        for cand in by_type.get(kind, []):
            if cand["slug"] in taken:
                continue
            keys = cand.get("keys") or {str(r.get("dedupe_key")) for r in cand["rows"]}
            if overlaps(set(keys), made_keys):
                continue
            cand["keys"] = set(keys)
            cand.setdefault("type", kind)
            if not peek:  # a preview mustn't move the rotation, or it would show one product and make another
                product_types.advance(state, kind)
            return cand
    return None


# ------------------------------------------------------------------ Phase 146: build
def price_for_rows(cfg: Any, rows: int) -> int:
    small, medium, large = (list(cfg.factory_prices) + [900, 1400, 1900])[:3]
    return int(small if rows < 60 else medium if rows < 200 else large)


def _csv(rows: list[dict[str, Any]], fields: list[str], bom: bool = False) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({f: ", ".join(map(str, r[f])) if isinstance(r.get(f), list) else r.get(f) for f in fields})
    data = buf.getvalue().encode()
    return ("﻿".encode() + data) if bom else data


def slice_content(cand: dict[str, Any], cfg: Any, now: datetime) -> dict[str, Any]:
    """Files, summary, price and preview of a technology slice (the other product types are in
    ``strategies/product_types.py`` and return the same shape)."""
    from strategies.dataset_extras import fields_md, jsonl, quality_md, schema_sql, top20_md

    from strategies.kinds_countries import country_column

    rows = country_column(sorted(cand["rows"], key=lambda r: str(r.get("posted_at") or ""), reverse=True))  # Phase 254
    fields = list(EXPORT_FIELDS) + ["country"]
    title = cand["title"]
    readme = (f"# {title}\n\n{len(rows)} job postings from {cand['companies']} companies, collected from public job boards "
              f"and filtered to: {_describe(cand['filters'])}.\n\nBuilt {now:%Y-%m-%d}. Every row links to its public "
              "posting. Files: `leads.csv` (UTF-8), `leads-excel.csv` (opens in Excel), `leads.json`, `leads.jsonl`, "
              "`schema.sql` (SQLite/Postgres), `QUALITY.md`, `FIELDS.md`, `TOP20.md`.\n")
    from strategies.posting_quality import listed_line, sources_line

    from strategies.kinds_stacks import with_line

    tech = str(cand["filters"].get("tech") or "")
    extra = " ".join(x for x in (sources_line(rows), listed_line(rows, now),
                                 with_line(rows, tech) if tech else "") if x)  # Phases 233-234, 264
    if extra:
        readme += f"\n{extra}\n"
    content = {
        "README.md": readme.encode(), "leads.csv": _csv(rows, fields), "leads-excel.csv": _csv(rows, fields, bom=True),
        "leads.json": json.dumps([{f: r.get(f) for f in fields} for r in rows], indent=2, default=str).encode(),
        "leads.jsonl": jsonl(rows, fields).encode(), "schema.sql": schema_sql(rows, fields).encode(),
        "QUALITY.md": quality_md(title, rows, [], now).encode(), "FIELDS.md": fields_md(False).encode(),
        "TOP20.md": top20_md(title, rows, []).encode(),
    }
    preview_fields = ["company", "title", "location", "remote", "stack"]
    from tools.seo_scale import insight

    return {"files": content, "readme": readme, "rows": len(rows), "price_cents": price_for_rows(cfg, len(rows)),
            "insight": insight(rows),
            "summary": (f"{len(rows)} current job postings from {cand['companies']} companies: {_describe(cand['filters'])}. "
                        "CSV, Excel, JSON and SQL, delivered instantly."),
            "preview_fields": preview_fields,
            "preview": [{f: (r.get(f)[:4] if isinstance(r.get(f), list) else r.get(f)) for f in preview_fields} for r in rows[:5]]}


def content_for(cand: dict[str, Any], cfg: Any, now: datetime) -> dict[str, Any]:
    kind = cand.get("type", "slice")
    if kind == "slice":
        return slice_content(cand, cfg, now)
    from strategies import product_types

    return product_types.BUILDERS[kind](cand, cfg, now)


def _content(tools: Any, cand: dict[str, Any]) -> dict[str, Any]:
    if cand.get("type") == "pack":
        from strategies.product_types import build_pack

        return build_pack(cand, tools.config, tools.state.clock(), tools.state, tools.files)
    return content_for(cand, tools.config, tools.state.clock())


def write_files(tools: Any, slug: str, version: int, made: dict[str, Any], title: str, filters: dict[str, Any]) -> str:
    """Zip + listing + preview for one version. Returns the zip's path."""
    from strategies.download_extras import enrich

    files = enrich(made, tools.config, title, slug, version, str(filters.get("type") or "slice"), tools.state.clock())  # 270-274
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, body in files.items():
            zf.writestr(f"{slug}/{name}", body)
    base = f"assets/{slug}/{KIND}-v{version}"
    zip_rel = f"assets/{slug}/{slug}-v{version}.zip"
    tools.files.write_bytes(zip_rel, data.getvalue())
    tools.files.write_json(f"{base}/listing.json", {"name": title, "summary": made["summary"], "price_cents": made["price_cents"],
                                                    "description_markdown": made["readme"], "filters": filters,
                                                    "insight": made.get("insight") or {}})
    tools.files.write_json(f"{base}/sample.json", {"fields": made["preview_fields"], "rows": made["preview"]})
    return zip_rel


def build(tools: Any, cand: dict[str, Any]) -> dict[str, Any]:
    state = tools.state
    slug, title = cand["slug"], cand["title"]
    made = _content(tools, cand)
    filters = {**cand["filters"], "type": cand.get("type", "slice")}
    zip_rel = write_files(tools, slug, 1, made, title, filters)
    hid = _hypothesis(state, slug, title)
    aid = state.add_asset(hid, KIND, title, zip_rel, 1, made["rows"], made["price_cents"])
    state.update_asset(aid, niche=slug, status="staged")
    ensure(state)
    keys = sorted(cand.get("keys") or {str(r.get("dedupe_key")) for r in cand.get("rows", [])})
    state._exec("INSERT OR REPLACE INTO factory_products (slug, asset_id, title, filters, rows, companies, keys_sample, status, "
                "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (slug, aid, title, json.dumps(filters), made["rows"], cand["companies"], json.dumps(keys[:2000]),
                 "staged", state.now()))
    return {"slug": slug, "asset_id": aid, "price_cents": made["price_cents"], "rows": made["rows"],
            "summary": made["summary"], "title": title}


def _describe(filters: dict[str, str]) -> str:
    if filters.get("describe"):  # Phases 245-269: kinds describe their own postings
        return str(filters["describe"])
    bits = [f"roles mentioning {label(filters['tech'])}"]
    if filters.get("level"):
        bits.append(f"{filters['level']} level")
    if filters.get("region") == "remote":
        bits.append("remote")
    elif filters.get("region"):
        bits.append({"us": "located in the US", "europe": "located in Europe"}[filters["region"]])
    return ", ".join(bits)


def _hypothesis(state: Any, slug: str, title: str) -> int:
    key = f"{KIND}:{slug}"
    row = state._one("SELECT id FROM hypotheses WHERE key = ?", (key,))
    if row:
        return int(row["id"])
    # Inserted straight as 'product': never 'active' even for a moment, or the engine (another thread)
    # could pick it up as its main niche.
    now = state.now()
    cur = state._exec("INSERT INTO hypotheses (key, strategy, description, params, status, created_at, updated_at) "
                      "VALUES (?,?,?,?,?,?,?)", (key, KIND, title, json.dumps({"niche": slug, "factory": True}), "product",
                                                 now, now))
    return int(cur.lastrowid)


# ------------------------------------------------------------------ Phase 147: publish
def publish(tools: Any, slug: str) -> str:
    """Open the checkout for a staged product. Returns live | staged (why in the log) ."""
    from tools.storefront import Listing

    state, cfg = tools.state, tools.config
    row = state._one("SELECT * FROM factory_products WHERE slug = ?", (slug,))
    asset = state.get_asset(int(row["asset_id"])) if row else None
    if asset is None:
        return "missing"
    if not (tools.dispatcher.can_deliver() or cfg.allow_manual_fulfillment):
        return "staged"
    if tools.storefront.name != "stripe" or not cfg.stripe_secret_key:
        return "staged"
    meta = tools.files.read_json(f"assets/{slug}/{KIND}-v1/listing.json")
    sample = tools.files.read_json(f"assets/{slug}/{KIND}-v1/sample.json")
    listing = Listing(asset_id=asset["id"], hypothesis_id=asset["hypothesis_id"], niche=slug, title=meta["name"],
                      summary=meta["summary"], description_md=meta.get("description_markdown", ""),
                      price_cents=int(meta["price_cents"]), zip_path=asset["path"], sample_rows=sample.get("rows", []),
                      sample_columns=sample.get("fields", []))
    result = tools.storefront.publish(listing, None)
    if not result.live:
        return "staged"
    state.update_asset(asset["id"], provider=result.provider, checkout_url=result.checkout_url, status="published",
                       **({"product_ref": result.product_ref} if result.product_ref else {}))
    state._exec("UPDATE factory_products SET status = 'live', published_at = ? WHERE slug = ?", (state.now(), slug))
    return "live"


def retire_unsold(tools: Any) -> list[str]:
    state, cfg = tools.state, tools.config
    cutoff = (state.clock() - timedelta(days=int(cfg.factory_retire_days))).isoformat(timespec="seconds")
    retired = []
    for row in state._all("SELECT * FROM factory_products WHERE status = 'live' AND published_at < ?", (cutoff,)):
        sold = state._one("SELECT COUNT(*) AS n FROM orders o JOIN assets a ON a.id = o.asset_id WHERE a.niche = ? "
                          "AND o.status NOT IN ('refunded', 'disputed')", (row["slug"],))["n"]  # any version counts
        from strategies.product_controls import pinned

        if sold or pinned(state, row["slug"]):  # Phase 280: pinned products stay
            continue
        asset = state.get_asset(int(row["asset_id"])) or {}
        ref = str(asset.get("product_ref") or "")
        if ref.startswith("plink_") and hasattr(tools.storefront, "deactivate"):
            try:
                tools.storefront.deactivate(ref)
            except Exception as exc:  # noqa: BLE001 - retried next time
                state.log_error("product_factory", f"couldn't retire {row['slug']}: {exc!r}")
                continue
            from strategies.catalog_hygiene import archive

            archive(tools, ref)  # Phase 211: the Stripe product too, best effort
        state.update_asset(int(row["asset_id"]), status="retired")
        state._exec("UPDATE factory_products SET status = 'retired', retired_at = ? WHERE slug = ?", (state.now(), row["slug"]))
        retired.append(row["slug"])
    return retired


# ------------------------------------------------------------------ Phase 148: cadence
LAST = "factory_last_product_at"
MADE = "factory_last_made_at"  # Phase 215: LAST also moves when nothing could be made


def tick(tools: Any, force: bool = False) -> dict[str, Any]:
    """One factory step: publish anything staged, retire stale products, then make one new product
    if the interval has passed (or ``force``)."""
    state, cfg = tools.state, tools.config
    if not cfg.product_factory:
        return {"made": None, "why": "product_factory = false"}
    ensure(state)
    published = [r["slug"] for r in state._all("SELECT slug FROM factory_products WHERE status = 'staged'")
                 if publish(tools, r["slug"]) == "live"]
    retired = retire_unsold(tools)
    from strategies.catalog_reliability import check_integrity, too_many_waiting
    from strategies.product_types import refresh_due

    check_integrity(tools)  # Phase 243: a missing download is rebuilt by the refresh just below
    refreshed = refresh_due(tools)
    last = state.get(LAST)
    if not force and last and state.clock() - datetime.fromisoformat(last) < timedelta(seconds=int(cfg.factory_interval_seconds)):
        return {"made": None, "why": "not due yet", "published": published, "retired": retired, "refreshed": refreshed}
    from agent.disk_guard import builds_paused

    if builds_paused(state):
        return {"made": None, "why": "disk almost full", "published": published, "retired": retired, "refreshed": refreshed}
    waiting = too_many_waiting(state)
    if waiting:  # Phase 242
        return {"made": None, "why": f"{waiting} product(s) are waiting for a checkout (payments not set up, or Stripe refusing)",
                "published": published, "retired": retired, "refreshed": refreshed}
    cand = next_candidate(state, cfg)
    if cand is None:
        state.set(LAST, state.now())
        return {"made": None, "why": "no new slice clears the quality floor yet (waiting for more postings)",
                "published": published, "retired": retired, "refreshed": refreshed}
    made = build(tools, cand)
    status = publish(tools, made["slug"])
    state.set(LAST, state.now())
    state.set(MADE, state.now())
    state.log_action(int(state.get("iteration", 0)), None, "product_factory", "ok",
                     f"new product: {made['title']} ({made['rows']} rows, ${made['price_cents'] / 100:.2f}, {status})")
    return {"made": made, "status": status, "published": published, "retired": retired, "refreshed": refreshed}


def catalog(state: Any) -> dict[str, int]:
    ensure(state)
    rows = state._all("SELECT status, COUNT(*) AS n FROM factory_products GROUP BY status")
    return {r["status"]: int(r["n"]) for r in rows}


def run_worker(tools: Any, stop: Any, is_stopped: Any = None) -> None:
    """The supervisor's ``factory`` worker: one tick every ``factory_interval_seconds``."""
    interval = max(60, int(tools.config.factory_interval_seconds))
    from agent.scale_checks import record_factory_tick

    while not stop.is_set():
        try:
            if not (is_stopped and is_stopped()[0]):
                tools.breaker.begin_cycle()
                out = tick(tools)
                record_factory_tick(tools.state, True, "made one" if out.get("made") else str(out.get("why") or ""))
        except Exception as exc:  # noqa: BLE001 - the factory must never take the agent down
            tools.state.log_error("product_factory", f"factory tick failed: {exc!r}")
            record_factory_tick(tools.state, False, repr(exc)[:200])
        stop.wait(interval)


class ProductFactory(Strategy):
    name = "product_factory"
    tasks = ("run_factory",)

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        """Catch-up when no factory worker runs: one product per interval elapsed, at most 6 per cycle."""
        tools = ctx.tools
        state, cfg = tools.state, tools.config
        if not cfg.product_factory:
            return TaskResult(True, "product factory off", {"made": 0})
        last = state.get(LAST)
        due = 1 if not last else int((state.clock() - datetime.fromisoformat(last)).total_seconds()
                                     // max(60, int(cfg.factory_interval_seconds)))
        made, why = [], ""
        for _ in range(min(6, due)):
            out = tick(tools, force=True)
            if not out.get("made"):
                why = out.get("why", "")
                break
            made.append(out["made"]["title"])
        counts = catalog(state)
        summary = (f"factory: {len(made)} new product(s)" + (f" ({', '.join(made[:3])})" if made else "")
                   + (f"; {why}" if why and not made else "") + f"; catalog {counts}")
        return TaskResult(True, summary[:400], {"made": len(made), **{f"catalog_{k}": v for k, v in counts.items()}})
