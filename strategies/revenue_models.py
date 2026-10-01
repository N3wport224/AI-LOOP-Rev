"""New ways to get paid (Phases 155-159), each a Stripe Payment Link the agent creates and fulfils.

``publish_offers`` creates any that are switched on and don't exist yet (needs live payments and
working email, like every product); ``/more/`` on the site lists them.

* **Phase 155, custom dataset** (``offer_custom_request``, $29): checkout asks "Which technology,
  region or seniority?". The agent reads the answer, builds that slice from every posting it has
  (a lower floor: ``CUSTOM_MIN_ROWS``) and emails it. If it can't, the order waits for you with an
  alert quoting the request: you reply or refund (money decisions stay yours).
* **Phase 156, name your price** (``offer_pay_what_you_want``, from $3): a supporter product: the
  buyer picks the amount and gets the full catalog index (every product with its link).
* **Phase 157, sponsorship** (``offer_sponsorship``, $49/week): after payment you get an alert;
  nothing is shown until you approve the sponsor's line with ``automonetize sponsor approve``.
  The line then appears at the top of the home and pricing pages for 7 days.
* **Phase 158, lifetime pass** (``offer_lifetime``, $149): download links for every product on
  sale now, then once a week an email with links to the products added that week.
* **Phase 159, gift card** (``offer_gift``, $25): the buyer gets a one-time code worth $25 off any
  product, valid a year, to pass on. New Payment Links accept promotion codes for this.

State: kv ``offers`` (kind → asset id), ``sponsors``, table ``lifetime_passes``.
"""

from __future__ import annotations

import csv
import io
import json
import re
import secrets
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any

from strategies.base import Strategy, TaskContext, TaskResult

OFFERS_KEY = "offers"
SPONSORS = "sponsors"
CUSTOM_MIN_ROWS = 5
SPONSOR_DAYS = 7
GIFT_DAYS = 365
KINDS = {
    "custom_request": ("offer_custom_request", "Custom Hiring Dataset (built to order)", 2900,
                       "Tell us a technology, region or seniority and get a dataset of companies hiring for it, built from "
                       "every current posting and emailed within minutes."),
    "pay_what_you_want": ("offer_pay_what_you_want", "Support the Project (name your price)", 300,
                          "Pay what you like. You get the full catalog index (every dataset with its link) and our thanks."),
    "sponsorship": ("offer_sponsorship", "Sponsor the Site for a Week", 4900,
                    "One line with your link at the top of the home and pricing pages for 7 days, approved by a human "
                    "before it goes live. Reply to the receipt with your line and URL."),
    "lifetime": ("offer_lifetime", "Lifetime Pass: Every Dataset, Now and Future", 14900,
                 "Download links for every dataset on sale today, then a weekly email with every new one. One payment."),
    "gift": ("offer_gift", "Gift Card: $25 off any dataset", 2500,
             "A one-time code worth $25 off any dataset, valid for a year. You get the code by email to pass on."),
}
LIFETIME_SCHEMA = """CREATE TABLE IF NOT EXISTS lifetime_passes (
    email TEXT PRIMARY KEY,
    order_pk INTEGER,
    created_at TEXT NOT NULL,
    last_digest_at TEXT
)"""


def offers(state: Any) -> dict[str, int]:
    return dict(state.get(OFFERS_KEY) or {})


def kind_of(asset: dict[str, Any] | None) -> str:
    return str((asset or {}).get("kind") or "")


# ------------------------------------------------------------------ publishing
def create_offer(tools: Any, kind: str) -> int:
    from tools.storefront.stripe_pages_publisher import link_options

    _, title, price, summary = KINDS[kind]
    state, cfg = tools.state, tools.config
    sf = next(s for s in tools.storefronts if s.name == "stripe")
    aid = state.add_asset(None, kind, title, "", 1, 0, price)
    state.update_asset(aid, niche=f"offer-{kind.replace('_', '-')}", status="staged")
    key = f"automonetize-offer-{kind}-{aid}"
    meta = {"asset_id": str(aid), "kind": kind}
    product = sf.client.create_product(title, summary, meta, key + "-product")
    price_data: dict[str, Any] = {"product": product["id"], "currency": cfg.currency}
    if kind == "pay_what_you_want":
        price_data["custom_unit_amount"] = {"enabled": True, "minimum": price, "preset": 900}
    else:
        price_data["unit_amount"] = price
    price_obj = sf.client._post("/prices", price_data, key + "-price")
    options = link_options(cfg, meta)
    if kind == "custom_request":
        options["custom_fields"] = [{"key": "request", "label": {"type": "custom", "custom": "Technology, region or seniority?"},
                                     "type": "text"}]
    link = sf.client.create_payment_link(price_obj["id"], meta, "Thank you! Watch your inbox.", key + "-link", options=options)
    state.update_asset(aid, provider="stripe", product_ref=link["id"], checkout_url=link["url"], status="published")
    known = offers(state)
    known[kind] = aid
    state.set(OFFERS_KEY, known)
    return aid


class RevenueModels(Strategy):
    name = "revenue_models"
    tasks = ("publish_offers", "serve_passes")

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        return self.publish(ctx.tools) if task == "publish_offers" else self.serve(ctx.tools)

    def publish(self, tools: Any) -> TaskResult:
        cfg = tools.config
        live = str(cfg.stripe_secret_key or "").startswith(("sk_live_", "rk_live_", "sk_test_", "rk_test_"))
        if not live or not any(s.name == "stripe" for s in tools.storefronts):
            return TaskResult(True, "offers need Stripe", {"created": 0})
        if not (tools.dispatcher.can_deliver() or cfg.allow_manual_fulfillment):
            return TaskResult(True, "offers wait until email delivery works", {"created": 0})
        created = []
        known = offers(tools.state)
        for kind, (flag, *_rest) in KINDS.items():
            if getattr(cfg, flag, False) and kind not in known:
                try:
                    create_offer(tools, kind)
                    created.append(kind)
                except Exception as exc:  # noqa: BLE001 - retried next cycle
                    tools.state.log_error("revenue_models", f"couldn't create the {kind} offer: {exc!r}")
        return TaskResult(True, f"offers: created {', '.join(created)}" if created else "offers: all live", {"created": len(created)})

    def serve(self, tools: Any) -> TaskResult:
        sent = weekly_pass_digest(tools)
        expired = expire_sponsors(tools.state)
        return TaskResult(True, f"lifetime digests sent: {sent}" + (f"; {expired} sponsorship(s) ended" if expired else ""),
                          {"sent": sent})


# ------------------------------------------------------------------ fulfilment
FULFILLED = ("custom_request", "pay_what_you_want", "sponsorship", "lifetime", "gift")


def fulfil(tools: Any, order: dict[str, Any], asset: dict[str, Any]) -> str:
    kind = kind_of(asset)
    return {"custom_request": fulfil_custom, "pay_what_you_want": fulfil_supporter, "sponsorship": fulfil_sponsor,
            "lifetime": fulfil_lifetime, "gift": fulfil_gift}[kind](tools, order)


def _send(tools: Any, order: dict[str, Any], subject: str, body: str, attachments: list[Any] | None = None) -> str:
    from tools.dispatcher import Email

    return tools.dispatcher.send_transactional(Email(to=order["email"], subject=subject, body=body + "\n\nThank you!",
                                                     kind="delivery", attachments=attachments or []),
                                               audit_key=f"order:{order['id']}")


# Phase 155
def parse_request(text: str) -> dict[str, str]:
    from strategies.b2b_lead_aggregator import TECH_KEYWORDS

    low = f" {text.lower()} "
    tech = next((t for t in sorted(TECH_KEYWORDS, key=len, reverse=True)
                 if re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", low)), "")
    if not tech and re.search(r"\bgo\b", low):
        tech = "golang"
    region = ("remote" if "remote" in low else "europe" if re.search(r"europe|\beu\b|uk|germany|berlin|london", low)
              else "us" if re.search(r"\bus\b|usa|united states|america", low) else "")
    level = "senior" if "senior" in low else "junior" if re.search(r"junior|entry|graduate", low) else ""
    return {"tech": tech, "region": region, "level": level}


def fulfil_custom(tools: Any, order: dict[str, Any]) -> str:
    from strategies.product_factory import facets, fresh_leads, matches, slice_content, slice_spec
    from tools.dispatcher import Attachment

    state = tools.state
    meta = json.loads(order.get("meta") or "{}")
    text = str(meta.get("field:request") or "")
    wanted = parse_request(text)
    rows = []
    if wanted["tech"]:
        rows = [lead for lead in fresh_leads(state, 120) if matches(facets(lead), wanted)]
    if len(rows) < CUSTOM_MIN_ROWS:
        state.log_error("revenue_models", f"Custom dataset order {order['order_id']} from {order['email']} couldn't be built "
                                          f"automatically (request: \"{text[:200] or 'none given'}\"). Reply to the buyer, or "
                                          "refund in Stripe if you can't help.", kind="alert")
        return "manual"
    spec = slice_spec(wanted["tech"], wanted["region"], wanted["level"])
    cand = {**spec, "rows": rows, "companies": len({str(r.get('company') or '').lower() for r in rows})}
    made = slice_content(cand, tools.config, state.clock())
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, body in made["files"].items():
            zf.writestr(f"{spec['slug']}/{name}", body)
    return _send(tools, order, f"Your custom dataset: {spec['title']}",
                 f"You asked for: \"{text[:200]}\"\n\nAttached: {spec['title']}, {made['rows']} postings from "
                 f"{cand['companies']} companies. Reply if it's not what you meant and it'll be put right.",
                 [Attachment(f"{spec['slug']}.zip", data.getvalue())])


# Phase 156
def catalog_csv(state: Any) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["title", "price_usd", "checkout_url"])
    seen = set()
    for a in state.list_assets():
        if a.get("status") == "published" and a.get("checkout_url") and a["title"] not in seen and a["kind"] not in FULFILLED:
            seen.add(a["title"])
            w.writerow([a["title"], f"{int(a['price_cents']) / 100:.2f}", a["checkout_url"]])
    return buf.getvalue().encode()


def fulfil_supporter(tools: Any, order: dict[str, Any]) -> str:
    from tools.dispatcher import Attachment

    return _send(tools, order, "Thank you for your support",
                 f"Thank you for paying ${int(order['gross_cents']) / 100:.2f}: it keeps the data coming. Attached is the full "
                 "catalog index, every dataset with its link.", [Attachment("catalog.csv", catalog_csv(tools.state))])


# Phase 157
def fulfil_sponsor(tools: Any, order: dict[str, Any]) -> str:
    state = tools.state
    sponsors = list(state.get(SPONSORS) or [])
    sponsors.append({"id": int(order["id"]), "email": order["email"], "paid_at": order["occurred_at"], "status": "pending"})
    state.set(SPONSORS, sponsors)
    state.log_error("revenue_models", f"New sponsorship paid by {order['email']} (order {order['id']}). Ask for their line and "
                                      f"URL, then: automonetize sponsor approve {order['id']} \"<line>\" <https URL>",
                    kind="alert")
    _send(tools, order, "Your sponsorship: one step left",
          "Thanks for sponsoring! Reply with one line (up to 90 characters) and the https link it should point to. It goes "
          "live for 7 days once approved.")
    return "manual"  # the placement is delivered when you approve it


def approve_sponsor(state: Any, order_pk: int, line: str, url: str) -> dict[str, Any]:
    if not url.startswith("https://") or len(line.strip()) == 0 or len(line) > 90:
        raise ValueError("needs a line of 1-90 characters and an https:// URL")
    sponsors = list(state.get(SPONSORS) or [])
    entry = next((s for s in sponsors if int(s["id"]) == int(order_pk)), None)
    if entry is None:
        raise ValueError(f"no sponsorship order {order_pk}")
    entry.update(status="live", line=" ".join(line.split()), url=url, starts=state.now(),
                 ends=(state.clock() + timedelta(days=SPONSOR_DAYS)).isoformat(timespec="seconds"))
    state.set(SPONSORS, sponsors)
    state.set_order_status(int(order_pk), "delivered")
    return entry


def active_sponsor(state: Any) -> dict[str, Any] | None:
    now = state.clock()
    return next((s for s in state.get(SPONSORS) or [] if s.get("status") == "live"
                 and datetime.fromisoformat(s["ends"]) > now), None)


def expire_sponsors(state: Any) -> int:
    sponsors, n = list(state.get(SPONSORS) or []), 0
    for s in sponsors:
        if s.get("status") == "live" and datetime.fromisoformat(s["ends"]) <= state.clock():
            s["status"], n = "ended", n + 1
    if n:
        state.set(SPONSORS, sponsors)
    return n


def sponsor_html(state: Any) -> str:
    import html

    s = active_sponsor(state)
    if not s:
        return ""
    return (f'<p class="muted" style="border:1px solid #ddd;border-radius:6px;padding:6px 10px">Sponsored: '
            f'<a href="{html.escape(s["url"])}" rel="sponsored noopener">{html.escape(s["line"])}</a></p>')


# Phase 158
def _links(tools: Any, assets: list[dict[str, Any]], email: str) -> list[str]:
    from tools import download_links

    out = []
    for a in assets:
        if a.get("path"):
            out.append(f"- {a['title']}: {download_links.issue(tools.state, tools.config, a['path'], email)}")
    return out


def live_products(state: Any, since: str = "") -> list[dict[str, Any]]:
    """The newest version of every product on sale; with ``since``, only products first released
    after it (a refreshed version of an older product isn't new)."""
    assets = state.list_assets()
    first: dict[str, str] = {}
    for a in assets:
        key = str(a.get("niche") or a["id"])
        first[key] = min(first.get(key, a["created_at"]), a["created_at"])
    seen, out = set(), []
    for a in assets:
        key = str(a.get("niche") or a["id"])
        if (a.get("status") == "published" and a.get("path") and a["kind"] not in FULFILLED and key not in seen
                and a["kind"] not in ("subscription", "subscription_annual", "team_license") and (not since or first[key] > since)):
            seen.add(key)
            out.append(a)
    return out


def fulfil_lifetime(tools: Any, order: dict[str, Any]) -> str:
    from tools import download_links

    state = tools.state
    state._exec(LIFETIME_SCHEMA)
    state._exec("INSERT OR IGNORE INTO lifetime_passes (email, order_pk, created_at, last_digest_at) VALUES (?,?,?,?)",
                (order["email"], order["id"], state.now(), state.now()))
    if not download_links.available(tools.config):
        state.log_error("revenue_models", f"Lifetime pass for {order['email']}: download links need the public URL (tunnel); "
                                          "deliver by hand or set up the tunnel.", kind="alert")
        return "manual"
    links = _links(tools, live_products(state), order["email"])
    return _send(tools, order, "Your lifetime pass: every dataset",
                 "Welcome! Download links for every dataset on sale today (each works a few times; reply for fresh ones):\n\n"
                 + "\n".join(links) + "\n\nEvery week you'll get an email with the datasets added that week.")


def weekly_pass_digest(tools: Any) -> int:
    from tools import download_links
    from tools.dispatcher import Email

    state = tools.state
    state._exec(LIFETIME_SCHEMA)
    if not download_links.available(tools.config):
        return 0
    cutoff = (state.clock() - timedelta(days=7)).isoformat(timespec="seconds")
    sent = 0
    for p in state._all("SELECT * FROM lifetime_passes WHERE last_digest_at <= ?", (cutoff,)):
        new = live_products(state, since=p["last_digest_at"])
        if new:
            links = _links(tools, new, p["email"])
            tools.dispatcher.send_transactional(
                Email(to=p["email"], subject=f"Your lifetime pass: {len(new)} new dataset(s) this week",
                      body="New this week:\n\n" + "\n".join(links) + "\n\nThank you!", kind="delivery"),
                audit_key=f"lifetime:{p['email']}:{state.now()[:10]}")
            sent += 1
        state._exec("UPDATE lifetime_passes SET last_digest_at = ? WHERE email = ?", (state.now(), p["email"]))
    return sent


# Phase 159
def fulfil_gift(tools: Any, order: dict[str, Any]) -> str:
    state, cfg = tools.state, tools.config
    sf = next(s for s in tools.storefronts if s.name == "stripe")
    code = "GIFT-" + secrets.token_hex(4).upper()
    expires = int((datetime.now(timezone.utc) + timedelta(days=GIFT_DAYS)).timestamp())
    coupon = sf.client._post("/coupons", {"duration": "once", "amount_off": int(order["gross_cents"]), "currency": cfg.currency,
                                          "redeem_by": expires, "name": f"Gift {order['id']}"[:40]}, f"gift-coupon-{order['id']}")
    sf.client._post("/promotion_codes", {"coupon": coupon["id"], "code": code, "max_redemptions": 1, "expires_at": expires},
                    f"gift-code-{order['id']}")
    state.log_action(int(state.get("iteration", 0)), None, "gift", "ok", f"gift code for order {order['id']}")
    return _send(tools, order, "Your gift card code",
                 f"Here's your gift: the code {code} takes ${int(order['gross_cents']) / 100:.2f} off any dataset, once, until "
                 f"{datetime.fromtimestamp(expires, tz=timezone.utc):%B %d, %Y}. Pass it on with the site's address; it's "
                 "entered at checkout (\"Add promotion code\").")


# ------------------------------------------------------------------ site pages
def thanks_offers_html(state: Any) -> str:
    """Phase 163: on the thank-you page, the two offers a fresh buyer is most likely to want."""
    import html

    items = []
    for kind in ("lifetime", "custom_request"):
        aid = offers(state).get(kind)
        a = state.get_asset(int(aid)) if aid else None
        if a and a.get("status") == "published" and a.get("checkout_url"):
            _, title, price, summary = KINDS[kind]
            items.append(f'<li><b>{html.escape(title)}</b> (${price / 100:.0f}): {html.escape(summary)} '
                         f'<a href="{html.escape(a["checkout_url"])}" rel="noopener">Get it</a></li>')
    return f"<h2>Want more?</h2><ul>{''.join(items)}</ul>" if items else ""



def more_page(state: Any, shell: Any) -> str:
    import html

    rows = []
    for kind, aid in offers(state).items():
        a = state.get_asset(int(aid)) or {}
        if a.get("status") != "published" or not a.get("checkout_url"):
            continue
        _, title, price, summary = KINDS[kind]
        cta = "Name your price" if kind == "pay_what_you_want" else f"${price / 100:.0f}"
        rows.append(f"<h2>{html.escape(title)}</h2><p>{html.escape(summary)}</p>"
                    f'<p><a class="cta" href="{html.escape(a["checkout_url"])}" rel="noopener">{html.escape(cta)}</a></p>')
    body = "<h1>More ways to buy</h1>" + ("".join(rows) or "<p>Coming soon.</p>")
    return shell("More ways to buy", body, "Custom datasets built to order, a lifetime pass to every dataset, gift cards, "
                                           "sponsorships and a name-your-price option.")
