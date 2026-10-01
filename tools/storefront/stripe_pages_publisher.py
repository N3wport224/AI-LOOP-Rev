"""Stripe Payment Links + static sales landers (GitHub Pages or local hosting).

Stripe flow per dataset: ``POST /v1/products`` → ``POST /v1/prices`` → ``POST /v1/payment_links``.
Orders are the link's completed Checkout Sessions (``GET /v1/checkout/sessions?payment_link=…``),
which carry the buyer's email so the agent can deliver the file itself.

Without a secret key you can still pre-create Payment Links in the Stripe dashboard and map them
per niche in ``stripe_payment_links``; landers will use them, but orders can't be polled.
"""

from __future__ import annotations

import http.server
import socketserver
import threading
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tools.attribution import decode_ref
from tools.storefront import Listing, Order, PublishResult
from tools.storefront.github import GitHubClient

if TYPE_CHECKING:  # pragma: no cover
    from agent.config import Config
    from tools.page_builder import ProductPage
    from tools.http_client import HttpClient

STRIPE_API = "https://api.stripe.com/v1"


def flatten_form(data: dict[str, Any], prefix: str = "") -> dict[str, str]:
    """Encode nested dicts/lists the way Stripe expects: ``line_items[0][price]=…``."""
    out: dict[str, str] = {}
    for key, value in data.items():
        name = f"{prefix}[{key}]" if prefix else str(key)
        if isinstance(value, dict):
            out.update(flatten_form(value, name))
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    out.update(flatten_form(item, f"{name}[{i}]"))
                else:
                    out[f"{name}[{i}]"] = str(item)
        elif isinstance(value, bool):
            out[name] = "true" if value else "false"
        elif value is not None:
            out[name] = str(value)
    return out


class StripeClient:
    def __init__(self, http: "HttpClient", secret_key: str):
        self.http = http
        self.secret_key = secret_key

    def _headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.secret_key}"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def _post(self, path: str, data: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        return self.http.post(
            STRIPE_API + path, data=flatten_form(data), headers=self._headers(idempotency_key), check_robots=False
        ).json()

    def create_product(self, name: str, description: str, metadata: dict[str, str], key: str) -> dict[str, Any]:
        return self._post("/products", {"name": name, "description": description[:500], "metadata": metadata}, key)

    def create_price(self, product_id: str, unit_amount: int, currency: str, key: str,
                     recurring: dict[str, str] | None = None) -> dict[str, Any]:
        data: dict[str, Any] = {"product": product_id, "unit_amount": unit_amount, "currency": currency}
        if recurring:
            data["recurring"] = recurring  # e.g. {"interval": "month"}
        return self._post("/prices", data, key)

    def create_payment_link(self, price_id: str, metadata: dict[str, str], message: str, key: str,
                            subscription_metadata: dict[str, str] | None = None,
                            options: dict[str, Any] | None = None) -> dict[str, Any]:
        data: dict[str, Any] = {
            "line_items": [{"price": price_id, "quantity": 1}],
            "metadata": metadata,
            "after_completion": {"type": "hosted_confirmation", "hosted_confirmation": {"custom_message": message}},
            **(options or {}),  # e.g. automatic tax, a thank-you page redirect
        }
        if subscription_metadata:
            # Copied onto the Subscription object, so subscription events carry the niche/asset.
            data["subscription_data"] = {"metadata": subscription_metadata}
        return self._post("/payment_links", data, key)

    def create_checkout_session(self, price_id: str, success_url: str, cancel_url: str, metadata: dict[str, str],
                                key: str, client_reference_id: str = "", customer_email: str = "") -> dict[str, Any]:
        """One-off Checkout Session (used for per-company dossiers: the company rides in metadata)."""
        data: dict[str, Any] = {
            "mode": "payment", "line_items": [{"price": price_id, "quantity": 1}], "success_url": success_url,
            "cancel_url": cancel_url, "metadata": metadata, "payment_intent_data": {"metadata": metadata},
        }
        if client_reference_id:
            data["client_reference_id"] = client_reference_id
        if customer_email:
            data["customer_email"] = customer_email
        return self._post("/checkout/sessions", data, key)

    def create_portal_session(self, customer_id: str, return_url: str, key: str) -> dict[str, Any]:
        """Stripe-hosted billing portal (update card, see invoices, cancel). Needs the portal
        activated once in Dashboard → Settings → Billing → Customer portal."""
        return self._post("/billing_portal/sessions", {"customer": customer_id, "return_url": return_url}, key)

    def get_subscription(self, subscription_id: str) -> dict[str, Any]:
        return self.http.get_json(f"{STRIPE_API}/subscriptions/{subscription_id}", headers=self._headers(), check_robots=False)

    def paid_invoices(self, subscription_id: str, max_pages: int = 5) -> list[dict[str, Any]]:
        invoices: list[dict[str, Any]] = []
        params: dict[str, Any] = {"subscription": subscription_id, "status": "paid", "limit": 100}
        for _ in range(max_pages):
            page = self.http.get_json(f"{STRIPE_API}/invoices", params=params, headers=self._headers(), check_robots=False)
            data = page.get("data", [])
            invoices.extend(data)
            if not page.get("has_more") or not data:
                break
            params = {**params, "starting_after": data[-1]["id"]}
        return invoices

    def deactivate_payment_link(self, link_id: str) -> None:
        self._post(f"/payment_links/{link_id}", {"active": False}, f"deactivate-{link_id}")

    def completed_sessions(self, payment_link: str, created_gte: int, max_pages: int = 10) -> list[dict[str, Any]]:
        return self.sessions(payment_link, created_gte, status="complete", max_pages=max_pages)

    def sessions(self, payment_link: str, created_gte: int, status: str | None = None, max_pages: int = 10) -> list[dict[str, Any]]:
        sessions: list[dict[str, Any]] = []
        params: dict[str, Any] = {"payment_link": payment_link, "limit": 100, "created[gte]": created_gte}
        if status:
            params["status"] = status
        for _ in range(max_pages):
            page = self.http.get_json(
                STRIPE_API + "/checkout/sessions", params=params, headers=self._headers(), check_robots=False
            )
            data = page.get("data", [])
            sessions.extend(data)
            if not page.get("has_more") or not data:
                break
            params = {**params, "starting_after": data[-1]["id"]}
        return sessions


class StripeStorefront:
    name = "stripe"

    def __init__(self, config: "Config", http: "HttpClient"):
        self.config = config
        self.client = StripeClient(http, config.stripe_secret_key)
        self.fee_pct = config.stripe_fee_pct
        self.fee_fixed_cents = config.stripe_fee_fixed_cents

    def configured(self) -> bool:
        return bool(self.config.stripe_secret_key or self.config.stripe_payment_links)

    def publish(self, listing: Listing, previous: dict[str, Any] | None) -> PublishResult:
        cfg = self.config
        if not cfg.stripe_secret_key:
            url = cfg.stripe_payment_links.get(listing.niche, "")
            return PublishResult(
                self.name, live=bool(url), checkout_url=url, product_ref=None,
                detail="pre-configured Payment Link (no secret key: orders are not polled)" if url
                else f"no Payment Link configured for niche {listing.niche!r}",
            )
        # Reuse the live link while the price is unchanged: new versions don't need a new checkout.
        if (
            previous and previous.get("provider") == self.name and previous.get("product_ref")
            and previous.get("checkout_url") and previous.get("price_cents") == listing.price_cents
        ):
            return PublishResult(self.name, True, previous["checkout_url"], previous["product_ref"], "reused Payment Link")

        link_id, url, product_id = self.create_link(
            listing.title, listing.summary, listing.price_cents,
            {"niche": listing.niche, "hypothesis_id": str(listing.hypothesis_id), "asset_id": str(listing.asset_id)},
        )
        if previous and previous.get("provider") == self.name and (previous.get("product_ref") or "").startswith("plink_"):
            self.deactivate(previous["product_ref"])
        return PublishResult(self.name, True, url, link_id, f"created {product_id} / {link_id}")

    def create_link(self, title: str, summary: str, price_cents: int, meta: dict[str, str]) -> tuple[str, str, str]:
        """Product → Price → Payment Link. Returns (payment_link_id, url, product_id)."""
        key = f"automonetize-{meta.get('hypothesis_id', '')}-{meta.get('asset_id', '')}-{price_cents}"
        if meta.get("experiment"):
            # A later return to the same price must mint a fresh link, not replay a deactivated one.
            key += f"-x{meta['experiment']}"
        product = self.client.create_product(title, summary, meta, key + "-product")
        price = self.client.create_price(product["id"], price_cents, self.config.currency, key + "-price")
        link = self.client.create_payment_link(
            price["id"], {**meta, "price_cents": str(price_cents)},
            "Thanks for your purchase! The full dataset will be emailed to you shortly.",
            key + "-link", options=link_options(self.config, meta),
        )
        return link["id"], link["url"], product["id"]

    def create_subscription_link(self, title: str, summary: str, price_cents: int, interval: str,
                                 meta: dict[str, str]) -> tuple[str, str, str]:
        """Product → recurring Price → Payment Link. Returns (payment_link_id, url, price_id)."""
        if interval not in ("day", "week", "month", "year"):
            raise ValueError(f"invalid Stripe recurring interval {interval!r}")
        key = f"automonetize-sub-{meta.get('niche', '')}-{price_cents}-{interval}-{meta.get('version', '1')}"
        product = self.client.create_product(title, summary, {**meta, "kind": "subscription"}, key + "-product")
        price = self.client.create_price(product["id"], price_cents, self.config.currency, key + "-price",
                                         recurring={"interval": interval})
        link = self.client.create_payment_link(
            price["id"], {**meta, "kind": "subscription"},
            f"You're subscribed! The current dataset arrives by email now, then fresh updates every {interval}.",
            key + "-link", subscription_metadata={**meta, "kind": "subscription"},
            options=link_options(self.config, meta),
        )
        return link["id"], link["url"], price["id"]

    def deactivate(self, link_id: str) -> None:
        try:
            self.client.deactivate_payment_link(link_id)
        except Exception:  # noqa: BLE001 - an old link staying active is harmless
            pass

    def session_statuses(self, link_id: str, created_gte: int) -> list[dict[str, Any]]:
        """All Checkout Sessions (open, complete, expired) for a link: each one is a checkout initiation."""
        if not self.config.stripe_secret_key:
            return []
        return self.client.sessions(link_id, created_gte)

    def fetch_orders(self, product_refs: list[str], since: datetime) -> list[Order]:
        if not self.config.stripe_secret_key:
            return []
        orders = []
        for ref in sorted({r for r in product_refs if r and r.startswith("plink_")}):
            for s in self.client.completed_sessions(ref, int(since.timestamp())):
                if s.get("payment_status") != "paid" or s.get("mode") == "subscription":
                    continue  # subscription revenue is recorded per paid invoice, not per session
                channel, campaign = decode_ref(s.get("client_reference_id"))
                orders.append(
                    Order(
                        provider=self.name,
                        order_id=s["id"],
                        email=(s.get("customer_details") or {}).get("email") or s.get("customer_email"),
                        gross_cents=int(s.get("amount_total") or 0),
                        product_ref=ref,
                        occurred_at=datetime.fromtimestamp(int(s.get("created", 0)), tz=timezone.utc).isoformat(timespec="seconds"),
                        channel=channel, campaign=campaign,
                    )
                )
        return orders


# ---------------------------------------------------------------------------- landers
def link_options(config: Any, meta: dict[str, str] | None = None) -> dict[str, Any]:
    """Extra Payment Link settings every new link gets (Stripe Tax when it's active on the account)."""
    opts: dict[str, Any] = {"allow_promotion_codes": True}  # launch codes, sales and gift cards (Phase 159)
    if getattr(config, "stripe_automatic_tax", False):
        opts["automatic_tax"] = {"enabled": True}
    if getattr(config, "checkout_thank_you_live", False):  # set once the thanks page is confirmed online
        opts["after_completion"] = thank_you_redirect(config)
    return opts


def thank_you_redirect(config: Any) -> dict[str, Any]:
    base = str(config.pages_base_url).rstrip("/")
    return {"type": "redirect", "redirect": {"url": f"{base}/thanks/?session_id={{CHECKOUT_SESSION_ID}}"}}


def _cell(value: Any) -> str:
    if isinstance(value, list):
        value = ", ".join(str(v) for v in value[:6])
    return str(value if value is not None else "")


def listing_page(listing: Listing, checkout_url: str, currency: str = "usd", metrics: dict[str, Any] | None = None) -> "ProductPage":
    from tools.page_builder import ProductPage

    return ProductPage(
        niche=listing.niche, title=listing.title, summary=listing.summary, price_cents=listing.price_cents,
        currency=currency, checkout_url=checkout_url, sample_columns=listing.sample_columns,
        sample_rows=listing.sample_rows, metrics=metrics or {}, sku=f"asset-{listing.asset_id}",
    )


def render_lander_html(listing: Listing, checkout_url: str, base_url: str = "") -> str:
    from tools.page_builder import render_product_page

    return render_product_page(listing_page(listing, checkout_url), base_url)


def render_lander_md(listing: Listing, checkout_url: str) -> str:
    cols = listing.sample_columns
    lines = [f"# {listing.title}", "", listing.summary, ""]
    if checkout_url:
        lines += [f"**[Buy the full dataset (${listing.price_cents / 100:.2f})]({checkout_url})**", ""]
    lines += ["## Free 5-record preview", "", "| " + " | ".join(c.replace("_", " ") for c in cols) + " |",
              "|" + "---|" * len(cols)]
    for r in listing.sample_rows:
        lines.append("| " + " | ".join(_cell(r.get(c)).replace("|", "\\|") for c in cols) + " |")
    return "\n".join(lines) + "\n"


class PagesDeployer:
    """Writes landers locally (``data/site/<niche>/``) and, if configured, to a GitHub Pages repo."""

    def __init__(self, config: "Config", files, github: GitHubClient | None):
        self.config = config
        self.files = files
        self.github = github

    def deploy(self, listing: Listing, checkout_url: str) -> str:
        cfg = self.config
        page = render_lander_html(listing, checkout_url, cfg.pages_base_url)
        self.files.write_text(f"site/{listing.niche}/index.html", page)
        self.files.write_text(f"site/{listing.niche}/index.md", render_lander_md(listing, checkout_url))
        if self.github and self.github.configured() and cfg.github_pages_repo:
            prefix = f"{cfg.github_pages_dir.strip('/')}/" if cfg.github_pages_dir.strip("/") else ""
            self.github.put_file(
                cfg.github_pages_repo, f"{prefix}{listing.niche}/index.html", page,
                f"Update {listing.niche} lander", cfg.github_pages_branch or cfg.github_branch,
            )
            base = cfg.pages_base_url.rstrip("/")
            return f"{base}/{listing.niche}/" if base else ""
        return ""


def serve_directory(directory: Path, port: int = 8000, block: bool = True) -> socketserver.TCPServer:
    """Local static hosting hook for landers (``automonetize serve``)."""
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    server = socketserver.TCPServer(("127.0.0.1", port), handler)
    if block:
        try:
            server.serve_forever()
        finally:
            server.server_close()
    else:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    return server

