"""Stripe Payment Links + static sales landers (GitHub Pages or local hosting).

Stripe flow per dataset: ``POST /v1/products`` → ``POST /v1/prices`` → ``POST /v1/payment_links``.
Orders are the link's completed Checkout Sessions (``GET /v1/checkout/sessions?payment_link=…``),
which carry the buyer's email so the agent can deliver the file itself.

Without a secret key you can still pre-create Payment Links in the Stripe dashboard and map them
per niche in ``stripe_payment_links``; landers will use them, but orders can't be polled.
"""

from __future__ import annotations

import html
import http.server
import socketserver
import threading
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tools.storefront import Listing, Order, PublishResult
from tools.storefront.github import GitHubClient

if TYPE_CHECKING:  # pragma: no cover
    from agent.config import Config
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

    def create_price(self, product_id: str, unit_amount: int, currency: str, key: str) -> dict[str, Any]:
        return self._post("/prices", {"product": product_id, "unit_amount": unit_amount, "currency": currency}, key)

    def create_payment_link(self, price_id: str, metadata: dict[str, str], message: str, key: str) -> dict[str, Any]:
        return self._post(
            "/payment_links",
            {
                "line_items": [{"price": price_id, "quantity": 1}],
                "metadata": metadata,
                "after_completion": {"type": "hosted_confirmation", "hosted_confirmation": {"custom_message": message}},
            },
            key,
        )

    def deactivate_payment_link(self, link_id: str) -> None:
        self._post(f"/payment_links/{link_id}", {"active": False}, f"deactivate-{link_id}")

    def completed_sessions(self, payment_link: str, created_gte: int, max_pages: int = 10) -> list[dict[str, Any]]:
        sessions: list[dict[str, Any]] = []
        params: dict[str, Any] = {
            "payment_link": payment_link, "status": "complete", "limit": 100, "created[gte]": created_gte,
        }
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

        key = f"automonetize-{listing.hypothesis_id}-{listing.asset_id}-{listing.price_cents}"
        meta = {"niche": listing.niche, "hypothesis_id": str(listing.hypothesis_id), "asset_id": str(listing.asset_id)}
        product = self.client.create_product(listing.title, listing.summary, meta, key + "-product")
        price = self.client.create_price(product["id"], listing.price_cents, cfg.currency, key + "-price")
        link = self.client.create_payment_link(
            price["id"], meta,
            "Thanks for your purchase! The full dataset will be emailed to you shortly.",
            key + "-link",
        )
        if previous and previous.get("provider") == self.name and previous.get("product_ref", "").startswith("plink_"):
            try:
                self.client.deactivate_payment_link(previous["product_ref"])
            except Exception:  # noqa: BLE001 - an old link staying active is harmless
                pass
        return PublishResult(self.name, True, link["url"], link["id"], f"created {product['id']} / {link['id']}")

    def fetch_orders(self, product_refs: list[str], since: datetime) -> list[Order]:
        if not self.config.stripe_secret_key:
            return []
        orders = []
        for ref in sorted({r for r in product_refs if r and r.startswith("plink_")}):
            for s in self.client.completed_sessions(ref, int(since.timestamp())):
                if s.get("payment_status") != "paid":
                    continue
                orders.append(
                    Order(
                        provider=self.name,
                        order_id=s["id"],
                        email=(s.get("customer_details") or {}).get("email") or s.get("customer_email"),
                        gross_cents=int(s.get("amount_total") or 0),
                        product_ref=ref,
                        occurred_at=datetime.fromtimestamp(int(s.get("created", 0)), tz=timezone.utc).isoformat(timespec="seconds"),
                    )
                )
        return orders


# ---------------------------------------------------------------------------- landers
def _cell(value: Any) -> str:
    if isinstance(value, list):
        value = ", ".join(str(v) for v in value[:6])
    return str(value if value is not None else "")


def render_lander_html(listing: Listing, checkout_url: str) -> str:
    title = html.escape(listing.title)
    cols = listing.sample_columns
    head = "".join(f"<th>{html.escape(c.replace('_', ' '))}</th>" for c in cols)
    rows = "\n".join(
        "<tr>" + "".join(f"<td>{html.escape(_cell(r.get(c)))}</td>" for c in cols) + "</tr>" for r in listing.sample_rows
    )
    price = f"${listing.price_cents / 100:.2f}"
    cta = (
        f'<a class="cta" href="{html.escape(checkout_url)}" rel="noopener">Buy the full dataset: {price}</a>'
        if checkout_url else "<p><em>Checkout opens soon.</em></p>"
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><meta name="description" content="{html.escape(listing.summary)}">
<style>
body{{font-family:system-ui,-apple-system,sans-serif;max-width:920px;margin:0 auto;padding:2rem 1rem;line-height:1.55;color:#111}}
h1{{font-size:1.9rem;margin-bottom:.3rem}} .lede{{color:#444;font-size:1.1rem}}
.wrap{{overflow-x:auto}} table{{border-collapse:collapse;width:100%;font-size:.92rem}}
td,th{{border-bottom:1px solid #e3e3e3;padding:.45rem;text-align:left;vertical-align:top}} th{{text-transform:capitalize}}
.cta{{display:inline-block;background:#111;color:#fff;padding:.8rem 1.2rem;border-radius:8px;text-decoration:none;font-weight:600;margin:1rem 0}}
.muted{{color:#666;font-size:.9rem}}
@media (prefers-color-scheme: dark){{body{{background:#111;color:#eee}}.lede{{color:#bbb}}td,th{{border-color:#333}}.cta{{background:#eee;color:#111}}.muted{{color:#999}}}}
</style></head><body>
<h1>{title}</h1>
<p class="lede">{html.escape(listing.summary)}</p>
{cta}
<h2>Free 5-record preview</h2>
<div class="wrap"><table><thead><tr>{head}</tr></thead><tbody>
{rows}
</tbody></table></div>
<p class="muted">Built from public job-board APIs; every record links to its source. Delivered as CSV + JSON + an executive summary.</p>
</body></html>
"""


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
        page = render_lander_html(listing, checkout_url)
        self.files.write_text(f"site/{listing.niche}/index.html", page)
        self.files.write_text(f"site/{listing.niche}/index.md", render_lander_md(listing, checkout_url))
        cfg = self.config
        if self.github and self.github.configured() and cfg.github_pages_repo:
            self.github.put_file(
                cfg.github_pages_repo, f"docs/{listing.niche}/index.html", page,
                f"Update {listing.niche} lander", cfg.github_branch,
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

