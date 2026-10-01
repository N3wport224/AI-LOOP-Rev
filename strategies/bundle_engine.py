"""All-datasets bundle: one checkout for every niche the agent sells, at a discount.

``publish_bundle`` (each cycle):

* Takes the newest live dataset of each niche (``tools.catalog.live_products``). With fewer than
  ``bundle_min_niches`` (2) there's no bundle.
* Price = ``bundle_discount`` (40%) off the sum of the parts, rounded to whole dollars.
* The bundle is an ordinary product: a zip with each niche's dataset zip inside, a Stripe Payment
  Link, and an asset of kind ``bundle``. Payment matching, email delivery, order recovery, revenue
  and the owner's sale emails all work as for any dataset.
* When a niche's dataset gets a new version, the bundle zip is rebuilt in place; the link stays.
  When the set of niches (or the price) changes, a new link replaces the old one, which is
  deactivated in Stripe so nobody can buy a stale bundle.
"""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any

from strategies.base import Strategy, TaskContext, TaskResult

KIND = "bundle"


def components(state: Any) -> dict[str, dict[str, Any]]:
    """niche -> newest live dataset asset."""
    from tools.catalog import live_products

    out: dict[str, dict[str, Any]] = {}
    for p in live_products(state):
        if p["kind"] != "lead_directory":
            continue
        asset = state.get_asset(p["id"])
        hyp = state.get_hypothesis(asset["hypothesis_id"]) if asset and asset.get("hypothesis_id") else None
        niche = ((hyp or {}).get("params") or {}).get("niche") or f"asset-{p['id']}"
        if niche not in out or asset["id"] > out[niche]["id"]:
            out[niche] = asset
    return out


def bundle_price(parts: list[dict[str, Any]], discount: float) -> int:
    total = sum(int(a["price_cents"] or 0) for a in parts)
    return max(500, int(round(total * (1 - discount) / 100.0)) * 100)


def current_bundle(state: Any) -> dict[str, Any] | None:
    return next((a for a in state.list_assets() if a["kind"] == KIND and a.get("status") == "published"), None)


def build_zip(tools: Any, parts: dict[str, dict[str, Any]], title: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        lines = [title, "", "Included datasets:"]
        for niche, asset in sorted(parts.items()):
            path = tools.files.resolve(asset["path"])
            zf.writestr(f"{niche}/{path.name}", path.read_bytes())
            lines.append(f"- {asset['title']} ({asset['lead_count']} rows): {niche}/{path.name}")
        lines += ["", "Each folder holds that niche's full dataset (unzip it), with its own README and attribution."]
        zf.writestr("README.txt", "\n".join(lines) + "\n")
    return buf.getvalue()


class BundleEngine(Strategy):
    name = "bundle_engine"
    tasks = ("publish_bundle",)

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        from strategies.subscription_engine import stripe_storefront

        tools, cfg, state = ctx.tools, ctx.tools.config, ctx.tools.state
        parts = components(state)
        if len(parts) < cfg.bundle_min_niches:
            return TaskResult(True, f"no bundle yet: {len(parts)} niche(s) on sale, need {cfg.bundle_min_niches}", {"published": False})
        sf = stripe_storefront(tools)
        if sf is None:
            return TaskResult(True, "bundle needs Stripe", {"published": False})
        if not (tools.dispatcher.can_deliver() or cfg.allow_manual_fulfillment):
            return TaskResult(True, "bundle waits until email delivery works", {"published": False, "blocked": "fulfilment"})
        if not all(tools.files.exists(a["path"]) for a in parts.values()):
            return TaskResult(True, "bundle waits: a dataset file is missing", {"published": False})
        niches = sorted(parts)
        price = bundle_price(list(parts.values()), cfg.bundle_discount)
        rows = sum(int(a["lead_count"] or 0) for a in parts.values())
        title = f"All Tech Stack Intel datasets ({len(niches)} niches)"
        meta_now = {"niches": niches, "components": sorted(int(a["id"]) for a in parts.values())}
        existing = current_bundle(state)
        old_meta = json.loads((existing or {}).get("kind_meta") or "{}")
        if existing and old_meta.get("niches") == niches and int(existing["price_cents"]) == price:
            if old_meta.get("components") != meta_now["components"]:
                tools.files.write_bytes(existing["path"], build_zip(tools, parts, title))
                state.update_asset(existing["id"], kind_meta=json.dumps(meta_now))
                return TaskResult(True, f"bundle refreshed with the newest datasets ({existing['checkout_url']})", {"published": False})
            return TaskResult(True, f"bundle live: ${price / 100:.2f} for {len(niches)} niches", {"published": False})

        hyp_id = (state.active_hypothesis() or {}).get("id") or next(iter(parts.values()))["hypothesis_id"]
        rel = f"assets/bundles/all-datasets-{'-'.join(niches)}"[:120] + ".zip"
        tools.files.write_bytes(rel, build_zip(tools, parts, title))
        aid = state.add_asset(hyp_id, KIND, title, rel, 1, rows, price)
        summary = (f"Every dataset in one download: {', '.join(niches)}. {rows} companies hiring now, with tech stacks, "
                   f"hiring urgency and buying-intent signals. {int(cfg.bundle_discount * 100)}% less than buying each.")
        link_id, url, _ = sf.create_link(title, summary, price, {"hypothesis_id": str(hyp_id), "asset_id": str(aid), "kind": KIND,
                                                                 "niches": ",".join(niches)[:450]})
        state.update_asset(aid, provider="stripe", product_ref=link_id, checkout_url=url, status="published", niche=KIND,
                           kind_meta=json.dumps(meta_now))
        if existing:
            try:
                if str(existing.get("product_ref") or "").startswith("plink_"):
                    sf.deactivate(existing["product_ref"])
            except Exception as exc:  # noqa: BLE001 - the new link is live either way; retry isn't needed
                state.log_error("bundle", f"couldn't deactivate the old bundle link: {exc!r}")
            state.update_asset(existing["id"], status="retired")
        return TaskResult(True, f"bundle live: ${price / 100:.2f} for {', '.join(niches)}: {url}", {"published": True, "url": url})
