"""Package a niche's data into a sellable, versioned digital asset.

Produces, per version:

* ``assets/<niche>/v<N>/``: README guide, Executive Tech Radar, company-level ``tech_radar``
  CSV/JSON (when intel exists), the role directory and ``leads`` CSV/JSON, attribution note
* ``assets/<niche>/<niche>-intel-v<N>.zip``: the downloadable bundle
* ``assets/<niche>/v<N>/listing.json``: listing (title, summary, tiered price)
* ``assets/<niche>/v<N>/sample.json``: 5 sanitized records for the public showcase

Publishing (checkout + lander + showcase) is handled by ``distribution_engine``. For the Gumroad
fallback, a product whose name equals the listing title is linked automatically.
"""

from __future__ import annotations

import io
import json
import zipfile
from collections import Counter
from typing import Any

from strategies.b2b_lead_aggregator import EXPORT_FIELDS
from strategies.base import Strategy, TaskContext, TaskResult
from tools.storefront import price_for

SAMPLE_FIELDS_INTEL = ["company", "domain", "intent_tag", "urgency_score", "intent_signals", "stack", "open_positions"]
SAMPLE_FIELDS_LEADS = ["company", "title", "location", "remote", "stack"]

ASSET_KIND = "lead_directory"
SAMPLE_SIZE = 5


def niche_title(niche: str) -> str:
    return " ".join(w.upper() if len(w) <= 3 else w.capitalize() for w in niche.replace("_", "-").split("-"))


def listing_title(niche: str) -> str:
    return f"{niche_title(niche)} Tech Stack Intel"


def sanitized_sample(intel: list[dict[str, Any]], leads: list[dict[str, Any]], size: int = SAMPLE_SIZE) -> tuple[list[dict[str, Any]], list[str]]:
    """A public preview: no contact details, no descriptions, lists trimmed."""
    if intel:
        fields, rows = SAMPLE_FIELDS_INTEL, intel[:size]
    else:
        fields, rows = SAMPLE_FIELDS_LEADS, leads[:size]
    out = []
    for r in rows:
        clean = {}
        for f in fields:
            v = r.get(f)
            clean[f] = v[:4] if isinstance(v, list) else v
        out.append(clean)
    return out, fields


def _stats(leads: list[dict[str, Any]]) -> dict[str, Any]:
    stacks = Counter(s for lead in leads for s in lead.get("stack") or [])
    companies = Counter(lead.get("company", "") for lead in leads)
    seniority = Counter(lead.get("seniority") or "mid" for lead in leads)
    salaries = [lead["salary_min"] for lead in leads if lead.get("salary_min")]
    return {
        "count": len(leads),
        "companies": len(companies),
        "remote_share": (sum(1 for lead in leads if lead.get("remote")) / len(leads)) if leads else 0.0,
        "top_stack": stacks.most_common(10),
        "top_companies": companies.most_common(10),
        "seniority": dict(seniority.most_common()),
        "with_contact": sum(1 for lead in leads if lead.get("contact_email")),
        "salary_min_median": sorted(salaries)[len(salaries) // 2] if salaries else None,
        "sources": sorted({lead.get("source", "") for lead in leads}),
    }


def _md_cell(value: Any) -> str:
    if isinstance(value, list):
        value = ", ".join(str(v) for v in value)
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def render_directory_md(niche: str, leads: list[dict[str, Any]]) -> str:
    lines = [f"# {listing_title(niche)}", "", "| Company | Role | Location | Remote | Stack | Link |", "|---|---|---|---|---|---|"]
    for lead in sorted(leads, key=lambda d: (d.get("company", "").lower(), d.get("title", ""))):
        lines.append(
            "| "
            + " | ".join(
                [
                    _md_cell(lead.get("company")),
                    _md_cell(lead.get("title")),
                    _md_cell(lead.get("location")),
                    "yes" if lead.get("remote") else "no",
                    _md_cell((lead.get("stack") or [])[:5]),
                    f"[listing]({lead.get('url', '')})",
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def render_guide_md(niche: str, stats: dict[str, Any], version: int, generated_at: str) -> str:
    stack = ", ".join(f"{name} ({n})" for name, n in stats["top_stack"]) or "n/a"
    companies = ", ".join(name for name, _ in stats["top_companies"]) or "n/a"
    seniority = ", ".join(f"{k}: {v}" for k, v in stats["seniority"].items()) or "n/a"
    salary = f"${stats['salary_min_median']:,}" if stats["salary_min_median"] else "not enough data"
    return f"""# {listing_title(niche)} (v{version})

A curated, deduplicated and enriched directory of companies currently hiring for
**{niche_title(niche)}** roles. Generated {generated_at}.

## What's inside

- `directory.md`: every role in a readable table, grouped by company
- `leads.csv` / `leads.json`: the same data, ready for a spreadsheet or CRM
- {stats['count']} roles across {stats['companies']} companies ({stats['remote_share']:.0%} remote)
- {stats['with_contact']} listings include a publicly posted contact address

## Market snapshot

- **Most requested stack:** {stack}
- **Most active companies:** {companies}
- **Seniority mix:** {seniority}
- **Median advertised minimum salary:** {salary}

## Methodology

Listings come from public job-board APIs ({', '.join(stats['sources'])}). Each record is validated
(company, title and URL must be present and well-formed), enriched (tech stack, seniority,
remote flag, company domain), and deduplicated across sources by company + role + location.

## How to use it

1. Filter `leads.csv` by the stack and seniority you care about.
2. Apply through the original listing link; each row keeps its source URL.
3. Re-download the latest version for fresh roles.

See `ATTRIBUTION.md` for data sources and terms.
"""


def render_attribution_md(stats: dict[str, Any]) -> str:
    return (
        "# Data sources & attribution\n\n"
        "Listings were collected from public APIs and link back to the original posting:\n\n"
        + "\n".join(f"- {s}" for s in stats["sources"])
        + "\n\nJob listing data sourced from Remote OK (https://remoteok.com) where marked `remoteok`.\n"
        "Contact addresses are included only where the employer published them in the listing.\n"
    )


class DigitalAssetPackager(Strategy):
    name = "digital_asset_packager"
    tasks = ("package_asset",)

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        tools = ctx.tools
        cfg = tools.config
        niche = ctx.niche
        leads = tools.state.leads_for_niche(niche)
        if len(leads) < cfg.min_leads_for_asset:
            return TaskResult(
                ok=True,
                summary=f"skipped: {len(leads)} leads < minimum {cfg.min_leads_for_asset}",
                metrics={"built": False, "leads": len(leads)},
            )

        latest = tools.state.latest_asset(ctx.hypothesis["id"], ASSET_KIND)
        if latest and latest["lead_count"] == len(leads):
            linked = self._link_product(ctx, latest)
            return TaskResult(
                ok=True,
                summary=f"v{latest['version']} is current ({len(leads)} leads)",
                metrics={"built": False, "version": latest["version"], "product_linked": linked},
            )

        version = (latest["version"] + 1) if latest else 1
        stats = _stats(leads)
        intel_path = f"exports/intel/{niche}/tech_radar.json"
        intel = tools.files.read_json(intel_path) if tools.files.exists(intel_path) else []
        generated = tools.state.now()
        base = f"assets/{niche}/v{version}"
        title = listing_title(niche)
        files = {
            "README.md": render_guide_md(niche, stats, version, generated),
            "directory.md": render_directory_md(niche, leads),
            "ATTRIBUTION.md": render_attribution_md(stats),
            "leads.json": json.dumps(leads, indent=2, sort_keys=True, default=str) + "\n",
        }
        intel_dir = f"exports/intel/{niche}"
        for name in ("EXECUTIVE_TECH_RADAR.md", "tech_radar.json", "tech_radar.csv"):
            if intel and tools.files.exists(f"{intel_dir}/{name}"):
                files[name] = tools.files.read_text(f"{intel_dir}/{name}")
        for name, content in files.items():
            tools.files.write_text(f"{base}/{name}", content)
        tools.files.write_csv(f"{base}/leads.csv", leads, EXPORT_FIELDS)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in [*files, "leads.csv"]:
                zf.writestr(f"{niche}-intel/{name}", tools.files.read_bytes(f"{base}/{name}"))
        zip_rel = f"assets/{niche}/{niche}-intel-v{version}.zip"
        tools.files.write_bytes(zip_rel, buf.getvalue())

        companies = len(intel) or stats["companies"]
        # Tiers only pick the *starting* price. Once a version is live, the pricing engine owns the
        # price: re-deriving it here would silently undo experiments and put a different price on
        # the lander than the Payment Link charges.
        price = latest["price_cents"] if latest and latest.get("checkout_url") else price_for(companies, cfg.price_tiers)
        hot = sum(1 for r in intel if r.get("urgency_score", 0) >= cfg.high_urgency_threshold)
        summary = (
            f"{companies} companies hiring for {niche_title(niche)} roles, fingerprinted by tech stack "
            f"(cloud, databases, data platform, infra) and scored for hiring urgency"
            + (f"; {hot} show strong buying signals" if intel else "")
            + f". {stats['count']} open roles. CSV + JSON + Executive Tech Radar."
        )
        listing = {
            "name": title,
            "price_cents": price,
            "summary": summary,
            "description_markdown": files.get("EXECUTIVE_TECH_RADAR.md") or files["README.md"],
            "tags": [niche, "tech stack", "b2b", "dataset"],
            "file": zip_rel,
            "version": version,
        }
        tools.files.write_json(f"{base}/listing.json", listing)
        sample, fields = sanitized_sample(intel, leads)
        tools.files.write_json(f"{base}/sample.json", {"fields": fields, "rows": sample})

        inherited = {k: latest[k] for k in ("product_ref",) if latest and latest.get(k)}
        asset_id = tools.state.add_asset(
            ctx.hypothesis["id"], ASSET_KIND, title, zip_rel, version, len(leads), price,
            product_ref=inherited.get("product_ref"),
        )
        tools.state.update_asset(asset_id, niche=niche)
        if latest:
            # Carry the live checkout forward; the publish step decides whether it can be reused.
            tools.state.update_asset(
                asset_id, **{k: latest[k] for k in ("provider", "checkout_url", "showcase_url", "lander_url") if latest.get(k)}
            )
        asset = {"id": asset_id, "title": title, "product_ref": inherited.get("product_ref")}
        linked = self._link_product(ctx, asset)
        return TaskResult(
            ok=True,
            summary=f"built {title} v{version} ({companies} companies, ${price / 100:.2f}) -> {zip_rel}",
            metrics={"built": True, "version": version, "leads": len(leads), "companies": companies,
                     "price_cents": price, "zip": zip_rel, "product_linked": linked},
        )

    def _link_product(self, ctx: TaskContext, asset: dict[str, Any]) -> bool:
        """Attach a storefront product id to the asset so sales can be attributed."""
        if asset.get("product_ref"):
            return True  # already linked (or published by a storefront): leave its status alone
        revenue = ctx.tools.revenue
        if not revenue.gumroad_enabled():
            return False
        for product in revenue.list_gumroad_products():
            if str(product.get("name", "")).strip().lower() == asset["title"].lower():
                ctx.tools.state.link_product(asset["id"], str(product["id"]))
                return True
        return False
