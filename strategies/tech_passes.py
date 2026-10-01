"""Technology passes (Phases 320-324): one subscription for everything about a technology.

* **Phase 320, the offer:** once a technology has ``MIN_PRODUCTS`` (4) factory products on sale, a
  monthly subscription "Every Rust Dataset, Updated Weekly" (``PASS_PRICE``, $19/month) goes on sale
  (a Stripe recurring Payment Link; at most ``PER_DAY`` new passes in any 24 hours). It needs working delivery
  email and the public download links (the tunnel), because it's delivered by link.
* **Phase 321, welcome:** a new subscriber gets download links for every current product about the
  technology, at once.
* **Phase 322, every week:** on the usual delivery day (``subscription_delivery_weekday`` /
  ``_hour``), fresh links to the current version of each product, including any made that week.
  Only active (or trialing) subscriptions get it; cancelling in Stripe stops it.
* **Phase 323, offered where it matters:** each product page about the technology shows "Every Rust
  dataset, updated weekly: $19/month" next to its own price.
* **Phase 324, counted:** Monday's report shows pass subscribers and their monthly revenue.

The standard weekly-update subscription skips pass subscribers (they're served here), so nobody
gets two emails.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import timedelta
from typing import Any

PREFIX = "pass-"
MIN_PRODUCTS = 4
PASS_PRICE = 1900
PER_DAY = 2


def is_pass(niche: Any) -> bool:
    return str(niche or "").startswith(PREFIX)


def _by_tech(state: Any) -> dict[str, list[dict[str, Any]]]:
    from strategies.product_factory import ensure

    ensure(state)
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in state._all("SELECT f.slug AS slug, f.title AS title, f.filters AS filters, a.path AS path, a.id AS asset_id "
                        "FROM factory_products f JOIN assets a ON a.id = f.asset_id WHERE f.status = 'live'"):
        f = json.loads(r["filters"])
        if f.get("tech") and f.get("type") != "pack":
            out[f["tech"]].append(dict(r))
    return out


def pass_asset(state: Any, tech: str) -> dict[str, Any] | None:
    from strategies.product_factory import _slug
    from strategies.subscription_engine import subscription_asset

    return subscription_asset(state, PREFIX + _slug(tech))


# ------------------------------------------------------------------ Phase 320
def publish(tools: Any) -> list[str]:
    from strategies.product_factory import _slug, label
    from strategies.subscription_engine import SUB_KIND, stripe_storefront
    from tools import download_links

    state, cfg = tools.state, tools.config
    since = (state.clock() - timedelta(hours=24)).isoformat(timespec="seconds")
    today = int(state._one("SELECT COUNT(*) AS n FROM assets WHERE kind = ? AND niche LIKE ? AND created_at >= ?",
                           (SUB_KIND, PREFIX + "%", since))["n"])
    if today >= PER_DAY:
        return []
    sf = stripe_storefront(tools)
    if sf is None or not download_links.available(cfg) or not tools.dispatcher.can_deliver():
        return []
    made = []
    for tech, products in sorted(_by_tech(state).items(), key=lambda kv: -len(kv[1])):
        if len(products) < MIN_PRODUCTS or pass_asset(state, tech) or today + len(made) >= PER_DAY:
            continue
        niche = PREFIX + _slug(tech)
        title = f"Every {label(tech)} Dataset, Updated Weekly"
        summary = (f"All {len(products)}+ {label(tech)} hiring datasets, and every new one, with fresh versions each week. "
                   f"${PASS_PRICE / 100:.0f}/month, cancel any time.")
        ref, url, price_id = sf.create_subscription_link(title, summary, PASS_PRICE, "month", {"niche": niche, "version": "1"})
        aid = state.add_asset(None, SUB_KIND, title, products[0]["path"], 1, len(products), PASS_PRICE, product_ref=ref)
        state.update_asset(aid, niche=niche, provider="stripe", checkout_url=url, status="published",
                           kind_meta=json.dumps({"interval": "month", "price_id": price_id, "tech": tech}))
        state.log_action(int(state.get("iteration", 0)), None, "tech_pass", "ok", f"{title}: ${PASS_PRICE / 100:.0f}/month: {url}")
        made.append(niche)
    return made


# ------------------------------------------------------------------ Phases 321-322
def _tech_of(state: Any, niche: str) -> str:
    a = next((a for a in state.list_assets() if a["kind"] == "subscription" and a.get("niche") == niche), None)
    return json.loads((a or {}).get("kind_meta") or "{}").get("tech", "")


def _send(tools: Any, sub: dict[str, Any], period: str, subject: str, intro: str) -> str:
    from tools import download_links
    from tools.dispatcher import Email

    state = tools.state
    tech = _tech_of(state, sub["niche"])
    products = _by_tech(state).get(tech) or []
    if not products or not download_links.available(tools.config):
        return "skipped"
    links = [f"- {p['title']}: {download_links.issue(state, tools.config, p['path'], sub['email'])}" for p in products]
    try:
        outcome = tools.dispatcher.send_transactional(Email(
            to=sub["email"], kind="delivery", subject=subject,
            body=intro + "\n\n" + "\n".join(links) + "\n\nEach link works a few times for a week. Manage or cancel any time "
                 f"from your Stripe receipt.\n\n{tools.config.sender_name or 'AutoMonetize'}"),
            audit_key=f"pass:{sub['id']}:{period}")
    except Exception as exc:  # noqa: BLE001 - retried next tick
        state.record_subscription_delivery(sub["id"], period, "failed", detail=repr(exc))
        return "failed"
    state.record_subscription_delivery(sub["id"], period, outcome, len(products))
    return outcome


def deliver(tools: Any) -> dict[str, int]:
    from strategies.product_factory import label
    from strategies.subscription_engine import DELIVERABLE, delivery_moment

    state, cfg = tools.state, tools.config
    sent = {"welcome": 0, "weekly": 0}
    now = state.clock()
    moment, period = delivery_moment(now, cfg.subscription_delivery_weekday, cfg.subscription_delivery_hour,
                                     cfg.subscription_timezone)
    for sub in state.list_subscribers(DELIVERABLE):
        if not is_pass(sub.get("niche")) or not sub.get("email"):
            continue
        tech = label(_tech_of(state, sub["niche"]))
        welcome = state.subscription_delivery(sub["id"], "welcome")
        if not welcome or welcome["status"] not in ("delivered", "dry_run"):
            if _send(tools, sub, "welcome", f"Welcome: every {tech} dataset",
                     f"Thanks for subscribing. Here is every {tech} hiring dataset on sale today:") in ("delivered", "dry_run"):
                sent["welcome"] += 1
            continue
        if now < moment or welcome.get("created_at", "") >= moment.isoformat(timespec="seconds"):
            continue  # not the delivery day yet, or they joined after this week's delivery moment
        done = state.subscription_delivery(sub["id"], period)
        if done and done["status"] in ("delivered", "dry_run"):
            continue
        if _send(tools, sub, period, f"This week's {tech} datasets ({period})",
                 f"Fresh links to the current version of every {tech} dataset:") in ("delivered", "dry_run"):
            sent["weekly"] += 1
    return sent


# ------------------------------------------------------------------ Phase 323
def offer_for(state: Any, tech: str) -> dict[str, Any] | None:
    a = pass_asset(state, tech) if tech else None
    return {"url": a["checkout_url"], "price_cents": int(a["price_cents"])} if a and a.get("checkout_url") else None


# ------------------------------------------------------------------ Phase 324
def weekly_line(state: Any) -> str:
    from strategies.subscription_engine import DELIVERABLE

    subs = [s for s in state.list_subscribers(DELIVERABLE) if is_pass(s.get("niche"))]
    if not subs:
        return ""
    mrr = sum(int(s.get("price_cents") or PASS_PRICE) for s in subs)
    return f"Technology passes: {len(subs)} subscriber(s), ${mrr / 100:,.2f} a month."


def tick(tools: Any) -> dict[str, Any]:
    return {"published": publish(tools), **deliver(tools)}
