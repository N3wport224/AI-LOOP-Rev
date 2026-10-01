"""Catalog hygiene (Phases 210-214): a big catalog that stays tidy, everywhere it shows.

* **Phase 210, retired pages come down:** the site publisher now also deletes pages that are no
  longer built (a retired product, a technology hub with nothing left), but only files it
  published itself and never when a build looks broken (``tools/page_builder.py``).
* **Phase 211, Stripe stays tidy:** when the factory retires a product, its Stripe product is
  archived as well as its Payment Link deactivated, so your Stripe dashboard lists what's on sale.
* **Phase 212, prices follow demand:** at a product's weekly refresh, one that sold
  ``RAISE_AFTER`` (3) or more times in 30 days moves up one price step (``PRICE_STEPS``); the new
  price gets a new checkout and the old link is closed. Prices never go down by themselves and
  never past the top step.
* **Phase 213, disk stays tidy:** files of products retired more than 30 days ago are deleted
  (unless someone bought them: order recovery re-sends exactly what was bought).
* **Phase 214, Stripe shows current numbers:** at each refresh the Stripe product's description is
  updated with the new row count, so checkout says what the download holds.
"""

from __future__ import annotations

import hashlib
import shutil
from datetime import timedelta
from typing import Any

PRICE_STEPS = (500, 700, 900, 1400, 1900, 2900)
RAISE_AFTER = 3
DISK_DAYS = 30


def _promos(tools: Any) -> Any:
    from tools.promo import Promos

    return Promos(tools.http, tools.config.stripe_secret_key)


def stripe_product(tools: Any, link_id: str) -> str | None:
    if not str(link_id).startswith("plink_") or not tools.config.stripe_secret_key:
        return None
    try:
        return _promos(tools).product_for_link(link_id)
    except Exception:  # noqa: BLE001 - tidying is best effort
        return None


# ------------------------------------------------------------------ Phase 211
def archive(tools: Any, link_id: str) -> bool:
    product = stripe_product(tools, link_id)
    if not product:
        return False
    try:
        _promos(tools).client._post(f"/products/{product}", {"active": False}, f"archive-{product}")
        return True
    except Exception as exc:  # noqa: BLE001
        tools.state.log_error("catalog_hygiene", f"couldn't archive Stripe product {product}: {exc!r}")
        return False


# ------------------------------------------------------------------ Phase 212
def next_price(state: Any, niche: str, current: int) -> int:
    since = (state.clock() - timedelta(days=30)).isoformat(timespec="seconds")
    sold = int(state._one("SELECT COUNT(*) AS n FROM orders o JOIN assets a ON a.id = o.asset_id WHERE a.niche = ? "
                          "AND o.occurred_at >= ? AND o.status NOT IN ('refunded', 'disputed')", (niche, since))["n"])
    if sold < RAISE_AFTER:
        return current
    higher = [p for p in PRICE_STEPS if p > current]
    return higher[0] if higher else current


def reprice(tools: Any, asset: dict[str, Any], title: str, summary: str, price: int) -> dict[str, Any] | None:
    """A new checkout at ``price`` (the old link is closed by the storefront). Returns the new fields."""
    from tools.storefront import Listing

    if tools.storefront.name != "stripe" or not tools.config.stripe_secret_key:
        return None
    listing = Listing(asset_id=int(asset["id"]), hypothesis_id=int(asset.get("hypothesis_id") or 0), niche=str(asset["niche"]),
                      title=title, summary=summary, description_md="", price_cents=price, zip_path=asset["path"])
    result = tools.storefront.publish(listing, asset)
    if not result.live:
        return None
    tools.state.log_action(int(tools.state.get("iteration", 0)), None, "reprice", "ok",
                           f"{title}: ${int(asset['price_cents']) / 100:.2f} → ${price / 100:.2f} (selling well)")
    return {"checkout_url": result.checkout_url, "product_ref": result.product_ref, "price_cents": price}


# ------------------------------------------------------------------ Phase 213
def prune_retired(state: Any, files: Any) -> int:
    from strategies.product_factory import ensure

    ensure(state)
    cutoff = (state.clock() - timedelta(days=DISK_DAYS)).isoformat(timespec="seconds")
    removed = 0
    for row in state._all("SELECT slug FROM factory_products WHERE status = 'retired' AND retired_at < ?", (cutoff,)):
        sold = int(state._one("SELECT COUNT(*) AS n FROM orders o JOIN assets a ON a.id = o.asset_id WHERE a.niche = ?",
                              (row["slug"],))["n"])
        folder = f"assets/{row['slug']}"
        if sold or not files.exists(folder):
            continue
        shutil.rmtree(files.resolve(folder), ignore_errors=True)
        removed += 1
    return removed


# ------------------------------------------------------------------ Phase 214
def sync_description(tools: Any, link_id: str, summary: str) -> bool:
    product = stripe_product(tools, link_id)
    if not product:
        return False
    try:
        _promos(tools).client._post(f"/products/{product}", {"description": summary[:500]},
                                    f"describe-{product}-{hashlib.sha256(summary.encode()).hexdigest()[:12]}")
        return True
    except Exception:  # noqa: BLE001 - the page is the source of truth; this is a nicety
        return False
