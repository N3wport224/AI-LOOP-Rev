"""Package a niche's lead dataset into a sellable digital asset plus a public showcase page.

Produces, per version:

* ``assets/<niche>/v<N>/``: README guide, markdown directory, CSV + JSON data, attribution note
* ``assets/<niche>/<slug>-v<N>.zip``: the downloadable bundle
* ``assets/<niche>/v<N>/listing.json``: storefront listing draft (title, description, price)
* ``site/<niche>/index.html`` + ``index.md``: GitHub Pages showcase with a free sample

Storefront upload stays manual: Gumroad's public API does not create products or upload files.
Once a product exists whose name equals the listing title, the packager links it automatically
(via the Gumroad products API) so its sales are attributed to this hypothesis.
"""

from __future__ import annotations

import html
import io
import json
import zipfile
from collections import Counter
from typing import Any

from strategies.b2b_lead_aggregator import EXPORT_FIELDS
from strategies.base import Strategy, TaskContext, TaskResult

ASSET_KIND = "lead_directory"
SAMPLE_SIZE = 5


def niche_title(niche: str) -> str:
    return " ".join(w.upper() if len(w) <= 3 else w.capitalize() for w in niche.replace("_", "-").split("-"))


def listing_title(niche: str) -> str:
    return f"{niche_title(niche)} Hiring Directory"


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


def render_showcase_html(niche: str, stats: dict[str, Any], sample: list[dict[str, Any]], storefront_url: str, price_cents: int) -> str:
    rows = "\n".join(
        f"<tr><td>{html.escape(str(d.get('company', '')))}</td><td>{html.escape(str(d.get('title', '')))}</td>"
        f"<td>{html.escape(str(d.get('location', '')))}</td><td>{html.escape(', '.join((d.get('stack') or [])[:4]))}</td></tr>"
        for d in sample
    )
    cta = (
        f'<p><a class="cta" href="{html.escape(storefront_url)}">Get the full directory (${price_cents / 100:.2f})</a></p>'
        if storefront_url
        else "<p><em>Full directory coming soon.</em></p>"
    )
    title = html.escape(listing_title(niche))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:860px;margin:2rem auto;padding:0 1rem;line-height:1.5}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ddd;padding:.4rem;text-align:left}}
.cta{{display:inline-block;background:#111;color:#fff;padding:.6rem 1rem;border-radius:6px;text-decoration:none}}</style>
</head><body>
<h1>{title}</h1>
<p>{stats['count']} roles across {stats['companies']} companies, {stats['remote_share']:.0%} remote. Validated, enriched and deduplicated.</p>
<h2>Free sample</h2>
<table><thead><tr><th>Company</th><th>Role</th><th>Location</th><th>Stack</th></tr></thead><tbody>
{rows}
</tbody></table>
{cta}
</body></html>
"""


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
        generated = tools.state.now()
        base = f"assets/{niche}/v{version}"
        title = listing_title(niche)
        files = {
            "README.md": render_guide_md(niche, stats, version, generated),
            "directory.md": render_directory_md(niche, leads),
            "ATTRIBUTION.md": render_attribution_md(stats),
            "leads.json": json.dumps(leads, indent=2, sort_keys=True, default=str) + "\n",
        }
        for name, content in files.items():
            tools.files.write_text(f"{base}/{name}", content)
        tools.files.write_csv(f"{base}/leads.csv", leads, EXPORT_FIELDS)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in [*files, "leads.csv"]:
                zf.writestr(f"{niche}-directory/{name}", tools.files.read_bytes(f"{base}/{name}"))
        zip_rel = f"assets/{niche}/{niche}-directory-v{version}.zip"
        tools.files.write_bytes(zip_rel, buf.getvalue())

        listing = {
            "name": title,
            "price_cents": cfg.asset_price_cents,
            "summary": f"{stats['count']} {niche_title(niche)} roles across {stats['companies']} companies, "
            f"validated, enriched and deduplicated. CSV + JSON + readable directory.",
            "description_markdown": files["README.md"],
            "tags": [niche, "jobs", "directory", "dataset"],
            "file": zip_rel,
            "version": version,
        }
        tools.files.write_json(f"{base}/listing.json", listing)

        sample = leads[:SAMPLE_SIZE]
        tools.files.write_text(
            f"site/{niche}/index.html",
            render_showcase_html(niche, stats, sample, cfg.storefront_url, cfg.asset_price_cents),
        )
        tools.files.write_text(f"site/{niche}/index.md", render_directory_md(niche, sample))

        asset_id = tools.state.add_asset(
            ctx.hypothesis["id"], ASSET_KIND, title, zip_rel, version, len(leads), cfg.asset_price_cents,
            product_ref=latest["product_ref"] if latest else None,
        )
        asset = {"id": asset_id, "title": title, "product_ref": latest["product_ref"] if latest else None}
        linked = self._link_product(ctx, asset)
        return TaskResult(
            ok=True,
            summary=f"built {title} v{version} ({len(leads)} leads) -> {zip_rel}",
            metrics={"built": True, "version": version, "leads": len(leads), "zip": zip_rel, "product_linked": linked},
        )

    def _link_product(self, ctx: TaskContext, asset: dict[str, Any]) -> bool:
        """Attach a storefront product id to the asset so sales can be attributed."""
        if asset.get("product_ref"):
            if asset.get("id"):
                ctx.tools.state.link_product(asset["id"], asset["product_ref"])
            return True
        revenue = ctx.tools.revenue
        if not revenue.gumroad_enabled():
            return False
        for product in revenue.list_gumroad_products():
            if str(product.get("name", "")).strip().lower() == asset["title"].lower():
                ctx.tools.state.link_product(asset["id"], str(product["id"]))
                return True
        return False
