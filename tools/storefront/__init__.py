"""Storefront automation: publish packaged datasets as buyable listings and poll their orders.

Providers, chosen by ``config.storefront_provider`` (``auto`` picks the first one with credentials):

* ``stripe``: creates Product → Price → Payment Link through the Stripe API and polls completed
  Checkout Sessions per link. Fully closed loop: the agent emails the file to each buyer.
* ``lemonsqueezy``: creates a checkout (custom price + product name/description) on a variant you
  created once in the dashboard, and polls orders. Lemon Squeezy's API cannot create products or
  upload files, so that one-time setup stays manual.
* ``gumroad``: the original staging flow (listing draft for manual upload, sales sync by token).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:  # pragma: no cover
    from agent.config import Config
    from tools.http_client import HttpClient

MIN_PRICE_CENTS = 500
MAX_PRICE_CENTS = 1900


@dataclass
class Listing:
    asset_id: int
    hypothesis_id: int
    niche: str
    title: str
    summary: str
    description_md: str
    price_cents: int
    zip_path: str  # relative to data_dir
    sample_rows: list[dict[str, Any]] = field(default_factory=list)
    sample_columns: list[str] = field(default_factory=list)


@dataclass
class PublishResult:
    provider: str
    live: bool  # a buyer can pay right now
    checkout_url: str = ""
    product_ref: str | None = None  # the id orders carry, used for attribution
    detail: str = ""
    provider_delivers: bool = False  # the storefront itself hands over the file


@dataclass
class Order:
    provider: str
    order_id: str
    email: str | None
    gross_cents: int
    product_ref: str | None
    occurred_at: str
    refunded: bool = False
    product_name: str = ""
    asset_id: int | None = None  # explicit attribution hint (e.g. from checkout metadata)
    channel: str | None = None   # acquisition channel decoded from client_reference_id
    campaign: str | None = None
    kind: str = "one_off"        # one_off | subscription (an invoice)
    status: str | None = None    # override the fulfilment status (subscription invoices aren't "delivered")
    meta: dict | None = None     # product-specific details, e.g. {"kind": "dossier", "company_id": "acme"}


class Storefront(Protocol):
    name: str
    fee_pct: float
    fee_fixed_cents: int

    def configured(self) -> bool: ...

    def publish(self, listing: Listing, previous: dict[str, Any] | None) -> PublishResult: ...

    def fetch_orders(self, product_refs: list[str], since: datetime) -> list[Order]: ...


def price_for(count: int, tiers: list[list[int]]) -> int:
    """Pick a starting price tier by dataset size, clamped to the $5-$19 band."""
    price = MIN_PRICE_CENTS
    for threshold, cents in sorted(tiers):
        if count >= threshold:
            price = cents
    return max(MIN_PRICE_CENTS, min(MAX_PRICE_CENTS, int(price)))


class GumroadStaging:
    """Fallback: no API publishing. Buyers use ``storefront_url`` if you set one."""

    name = "gumroad"

    def __init__(self, config: "Config"):
        self.config = config
        self.fee_pct = config.platform_fee_pct
        self.fee_fixed_cents = config.platform_fee_fixed_cents

    def configured(self) -> bool:
        return True

    def publish(self, listing: Listing, previous: dict[str, Any] | None) -> PublishResult:
        url = self.config.storefront_url
        return PublishResult(
            provider=self.name,
            live=bool(url),
            checkout_url=url,
            product_ref=(previous or {}).get("product_ref"),
            detail="staged for manual Gumroad upload" + ("" if url else "; set storefront_url once the product exists"),
            provider_delivers=True,  # Gumroad delivers the file you uploaded
        )

    def fetch_orders(self, product_refs: list[str], since: datetime) -> list[Order]:
        return []  # handled by RevenueTracker.sync_gumroad


def build_storefronts(config: "Config", http: "HttpClient") -> list[Storefront]:
    """Every storefront with credentials, active one first. Orders are polled from all of them."""
    from tools.storefront.lemon_squeezy import LemonSqueezyStorefront
    from tools.storefront.stripe_pages_publisher import StripeStorefront

    candidates: dict[str, Storefront] = {
        "stripe": StripeStorefront(config, http),
        "lemonsqueezy": LemonSqueezyStorefront(config, http),
        "gumroad": GumroadStaging(config),
    }
    wanted = config.storefront_provider.lower()
    if wanted != "auto":
        if wanted not in candidates:
            raise ValueError(f"unknown storefront_provider {config.storefront_provider!r}")
        if not candidates[wanted].configured():
            wanted = "auto"  # fall back rather than publish nowhere
    order = ["stripe", "lemonsqueezy", "gumroad"]
    if wanted != "auto":
        order.remove(wanted)
        order.insert(0, wanted)
    return [candidates[n] for n in order if candidates[n].configured()]


__all__ = [
    "GumroadStaging", "Listing", "MAX_PRICE_CENTS", "MIN_PRICE_CENTS", "Order", "PublishResult", "Storefront",
    "build_storefronts", "price_for",
]
