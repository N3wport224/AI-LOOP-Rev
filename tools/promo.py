"""Stripe promotion codes for Payment Links: launch discounts and single-use refresh offers.

A code is a Stripe Coupon (percent off, restricted to one product, ``redeem_by``) plus a
Promotion Code (the text the buyer uses, with ``expires_at`` and optionally ``max_redemptions``).
Payment Links accept codes once ``allow_promotion_codes`` is on, and a link opened with
``?prefilled_promo_code=CODE`` applies it without the buyer typing anything.
"""

from __future__ import annotations

import re
import secrets
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from tools.storefront.stripe_pages_publisher import STRIPE_API, StripeClient


def code_text(prefix: str, seed: str = "", random_suffix: bool = False) -> str:
    """Upper-case letters and digits only, which every Stripe account accepts."""
    core = re.sub(r"[^A-Z0-9]", "", seed.upper())[:10]
    tail = secrets.token_hex(3).upper() if random_suffix else ""
    return f"{prefix}{core}{tail}"[:30]


def promo_link(url: str, code: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query["prefilled_promo_code"] = code
    return urlunsplit(parts._replace(query=urlencode(query)))


class Promos:
    def __init__(self, http: Any, secret_key: str):
        self.client = StripeClient(http, secret_key)
        self.http = http

    def product_for_link(self, link_id: str) -> str | None:
        page = self.http.get_json(f"{STRIPE_API}/payment_links/{link_id}/line_items", params={"limit": 1},
                                  headers=self.client._headers(), check_robots=False)
        items = page.get("data") or []
        if not items:
            return None
        product = (items[0].get("price") or {}).get("product")
        return product.get("id") if isinstance(product, dict) else product

    def create(self, link_id: str, code: str, percent_off: int, expires_at: int, max_redemptions: int | None = None,
               name: str = "") -> dict[str, Any]:
        """Allow codes on the link, then coupon + promotion code limited to the link's product."""
        product = self.product_for_link(link_id)
        if not product:
            raise ValueError(f"no product found for {link_id}")
        self.client._post(f"/payment_links/{link_id}", {"allow_promotion_codes": True}, f"allow-codes-{link_id}")
        coupon = self.client._post("/coupons", {
            "percent_off": percent_off, "duration": "once", "redeem_by": expires_at, "name": (name or code)[:40],
            "applies_to": {"products": [product]},
        }, f"coupon-{code}")
        data: dict[str, Any] = {"coupon": coupon["id"], "code": code, "expires_at": expires_at}
        if max_redemptions:
            data["max_redemptions"] = max_redemptions
        promo = self.client._post("/promotion_codes", data, f"promo-{code}")
        return {"code": code, "coupon": coupon["id"], "promotion_code": promo.get("id"), "percent_off": percent_off,
                "expires_at": expires_at, "product": product}
