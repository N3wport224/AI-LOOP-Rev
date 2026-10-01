"""Your hands on single products (Phases 280-284).

``automonetize product <slug> <action>`` and the control panel (``POST /api/products/action``):

* **Phase 280, pin / unpin:** a pinned product is never retired for not selling and never repriced
  automatically. For the products you know are worth keeping as they are.
* **Phase 281, retire:** close the checkout and take the product off the site now.
* **Phase 282, price <cents or $dollars>:** a new checkout at that price (the old link is closed); the change
  shows in the price history. Between $1 and $500.
* **Phase 283, hide / show:** hide takes the product off the site (pages, catalog, search) but keeps
  its checkout working, for links you've already shared. Show brings it back.
* **Phase 373, release:** promote a product the refund guard held back (``strategies/refund_guard.py``).
* **Phase 284, rebuild:** rebuild the download from the latest postings now instead of waiting for
  the weekly refresh.
"""

from __future__ import annotations

from typing import Any

PINS = "product_pins"
HIDDEN = "product_hidden"
ACTIONS = ("pin", "unpin", "retire", "price", "hide", "show", "rebuild", "release")
MIN_PRICE, MAX_PRICE = 100, 50000


def _set(state: Any, key: str) -> set[str]:
    return set(state.get(key) or [])


def pinned(state: Any, slug: str) -> bool:
    return slug in _set(state, PINS)


def hidden(state: Any) -> set[str]:
    return _set(state, HIDDEN)


def _row(state: Any, slug: str) -> dict[str, Any]:
    from strategies.product_factory import ensure

    ensure(state)
    row = state._one("SELECT * FROM factory_products WHERE slug = ?", (slug,))
    if not row:
        raise ValueError(f"no product {slug!r} (see automonetize factory)")
    return row


def apply(tools: Any, slug: str, action: str, value: str = "") -> str:
    """Do one action; returns a sentence for you. ValueError when it can't be done."""
    state = tools.state
    row = _row(state, slug)
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}: use {', '.join(ACTIONS)}")
    if action in ("pin", "unpin", "hide", "show"):
        key = PINS if action in ("pin", "unpin") else HIDDEN
        items = _set(state, key)
        (items.add if action in ("pin", "hide") else items.discard)(slug)
        state.set(key, sorted(items))
        return {"pin": f"Pinned {row['title']}: it won't be retired or repriced automatically.",
                "unpin": f"Unpinned {row['title']}.",
                "hide": f"Hidden {row['title']} from the site (after the next build); its checkout still works.",
                "show": f"{row['title']} is back on the site (after the next build)."}[action]
    if action == "release":  # Phase 373
        from strategies.refund_guard import release

        return (f"{row['title']} is promoted again." if release(state, slug)
                else f"{row['title']} wasn't held back.")
    if row["status"] != "live":
        raise ValueError(f"{row['title']} isn't on sale ({row['status']})")
    if action == "retire":
        from strategies.catalog_reliability import take_off_sale

        take_off_sale(tools, slug, reason="retired by you")
        return f"Retired {row['title']}: checkout closed, off the site after the next build."
    if action == "rebuild":
        from strategies.product_types import refresh_due

        state._exec("UPDATE factory_products SET refreshed_at = '1970-01-01T00:00:00+00:00' WHERE slug = ?", (slug,))
        done = refresh_due(tools)
        return f"Rebuilt {row['title']} from the latest postings." if slug in done else \
            f"{row['title']} couldn't be rebuilt now (its postings may be gone); it keeps the current version."
    # price
    raw = str(value or "").strip()
    try:  # "$9", "9.00" are dollars; "900" is cents
        cents = round(float(raw.lstrip("$")) * 100) if ("$" in raw or "." in raw) else int(raw)
    except ValueError:
        raise ValueError("price needs an amount: 900 (cents) or $9") from None
    if not MIN_PRICE <= cents <= MAX_PRICE:
        raise ValueError(f"price must be between {MIN_PRICE} and {MAX_PRICE} cents")
    from strategies.catalog_hygiene import reprice

    asset = state.get_asset(int(row["asset_id"])) or {}
    meta = tools.files.read_json(f"assets/{slug}/micro-v1/listing.json") if tools.files.exists(
        f"assets/{slug}/micro-v1/listing.json") else {}
    new = reprice(tools, asset, row["title"], str(meta.get("summary") or row["title"]), cents)
    if not new:
        raise ValueError("Stripe isn't connected, so the price can't change now")
    state.update_asset(int(asset["id"]), price_cents=cents, checkout_url=new["checkout_url"], product_ref=new["product_ref"])
    return f"{row['title']} now costs ${cents / 100:.2f} (new checkout; the old link is closed)."
