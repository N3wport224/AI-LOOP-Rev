"""Lemon Squeezy publisher over its JSON:API REST interface (https://api.lemonsqueezy.com/v1).

What the API allows, and therefore what this does:

* ``/v1/products``, ``/v1/variants`` and ``/v1/files`` are **read-only**. Products, variants and
  their downloadable files can only be created in the dashboard. So you create one "dataset"
  product once, either with a single shared variant (``lemonsqueezy_variant_id``) or with one
  variant per niche (``lemonsqueezy_variant_map``). The publisher verifies the variant and checks
  whether a file is attached (``/v1/files?filter[variant_id]=``).
* ``POST /v1/checkouts`` is writable: each dataset gets its own checkout URL with a custom price
  in the $5-$15 band and its own product name/description, via ``custom_price`` and
  ``product_options``.
* ``GET /v1/orders`` gives paid orders with the buyer's email. Checkout ``custom`` data is only
  delivered through webhooks, not the orders API, so orders are matched to datasets by variant
  (dedicated variants) or by product name (best effort). Unmatched orders are flagged for manual
  delivery.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from tools.storefront import MAX_PRICE_CENTS, MIN_PRICE_CENTS, Listing, Order, PublishResult

if TYPE_CHECKING:  # pragma: no cover
    from agent.config import Config
    from tools.http_client import HttpClient

API = "https://api.lemonsqueezy.com/v1"
JSONAPI = "application/vnd.api+json"


def validate_price(cents: int) -> int:
    if not MIN_PRICE_CENTS <= cents <= MAX_PRICE_CENTS:
        raise ValueError(f"price {cents} outside the ${MIN_PRICE_CENTS / 100:.0f}-${MAX_PRICE_CENTS / 100:.0f} band")
    return cents


def build_checkout_payload(
    store_id: str, variant_id: str, listing: Listing, redirect_url: str = "", test_mode: bool = False
) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        "custom_price": validate_price(listing.price_cents),
        "product_options": {
            "name": listing.title,
            "description": listing.summary,
            "enabled_variants": [int(variant_id)],
            "receipt_thank_you_note": "Your dataset will be emailed to you shortly.",
        },
        "checkout_data": {
            "custom": {"asset_id": str(listing.asset_id), "niche": listing.niche, "hypothesis_id": str(listing.hypothesis_id)}
        },
        "test_mode": test_mode,
    }
    if redirect_url:
        attributes["product_options"]["redirect_url"] = redirect_url
    return {
        "data": {
            "type": "checkouts",
            "attributes": attributes,
            "relationships": {
                "store": {"data": {"type": "stores", "id": str(store_id)}},
                "variant": {"data": {"type": "variants", "id": str(variant_id)}},
            },
        }
    }


class LemonSqueezyClient:
    def __init__(self, http: "HttpClient", api_key: str):
        self.http = http
        self.api_key = api_key

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Accept": JSONAPI, "Content-Type": JSONAPI}

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.http.get_json(API + path, params=params, headers=self._headers(), check_robots=False)

    def get_variant(self, variant_id: str) -> dict[str, Any]:
        return self._get(f"/variants/{variant_id}")["data"]

    def list_products(self, store_id: str) -> list[dict[str, Any]]:
        return self._get("/products", {"filter[store_id]": store_id}).get("data", [])

    def list_variant_files(self, variant_id: str) -> list[dict[str, Any]]:
        return self._get("/files", {"filter[variant_id]": variant_id}).get("data", [])

    def create_checkout(self, payload: dict[str, Any]) -> dict[str, Any]:
        resp = self.http.post(API + "/checkouts", json_body=payload, headers=self._headers(), check_robots=False)
        return resp.json()["data"]

    def list_orders(self, store_id: str, max_pages: int = 5) -> list[dict[str, Any]]:
        orders: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            body = self._get("/orders", {"filter[store_id]": store_id, "page[number]": page, "page[size]": 100})
            orders.extend(body.get("data", []))
            last = (body.get("meta") or {}).get("page", {}).get("lastPage", page)
            if page >= int(last or page):
                break
        return orders


class LemonSqueezyStorefront:
    name = "lemonsqueezy"

    def __init__(self, config: "Config", http: "HttpClient"):
        self.config = config
        self.client = LemonSqueezyClient(http, config.lemonsqueezy_api_key)
        self.fee_pct = config.lemonsqueezy_fee_pct
        self.fee_fixed_cents = config.lemonsqueezy_fee_fixed_cents
        self._verified: dict[str, bool] = {}

    def configured(self) -> bool:
        c = self.config
        return bool(c.lemonsqueezy_api_key and c.lemonsqueezy_store_id and (c.lemonsqueezy_variant_id or c.lemonsqueezy_variant_map))

    def variant_for(self, niche: str) -> tuple[str, bool]:
        """Returns (variant_id, dedicated)."""
        dedicated = self.config.lemonsqueezy_variant_map.get(niche)
        if dedicated:
            return str(dedicated), True
        if not self.config.lemonsqueezy_variant_id:
            raise LookupError(f"no Lemon Squeezy variant for niche {niche!r} and no shared variant configured")
        return str(self.config.lemonsqueezy_variant_id), False

    def publish(self, listing: Listing, previous: dict[str, Any] | None) -> PublishResult:
        variant_id, dedicated = self.variant_for(listing.niche)
        variant = self.client.get_variant(variant_id)  # raises on a wrong id: fail loudly, not silently
        has_file = bool(self.client.list_variant_files(variant_id))
        checkout = self.client.create_checkout(
            build_checkout_payload(self.config.lemonsqueezy_store_id, variant_id, listing, self.config.pages_base_url)
        )
        url = checkout["attributes"]["url"]
        # Shared variants can't identify the dataset from an order, so their ref never matches one.
        ref = variant_id if dedicated else f"ls-shared:{variant_id}:{listing.asset_id}"
        status = (variant.get("attributes") or {}).get("status", "")
        return PublishResult(
            self.name, True, url, ref,
            f"checkout on variant {variant_id} ({'dedicated' if dedicated else 'shared'}, status {status or 'unknown'}"
            f"{', file attached' if has_file else ''})",
            provider_delivers=dedicated and has_file,
        )

    def fetch_orders(self, product_refs: list[str], since: datetime) -> list[Order]:
        if not self.configured():
            return []
        out = []
        for o in self.client.list_orders(self.config.lemonsqueezy_store_id):
            a = o.get("attributes", {})
            created = str(a.get("created_at") or "")
            try:
                if created and datetime.fromisoformat(created.replace("Z", "+00:00")) < since:
                    continue
            except ValueError:
                pass
            if a.get("status") not in ("paid", "refunded", "partial_refund"):
                continue
            item = a.get("first_order_item") or {}
            out.append(
                Order(
                    provider=self.name,
                    order_id=str(o.get("id") or a.get("identifier")),
                    email=a.get("user_email"),
                    gross_cents=int(a.get("total") or 0) - int(a.get("tax") or 0),
                    product_ref=str(item.get("variant_id")) if item.get("variant_id") else None,
                    occurred_at=created.replace("Z", "+00:00") if created else "",
                    refunded=bool(a.get("refunded")) or a.get("status") == "refunded",
                    product_name=str(item.get("product_name") or ""),
                )
            )
        return out
