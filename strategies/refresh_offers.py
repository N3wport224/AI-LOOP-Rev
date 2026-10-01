"""Refresh offers: past buyers get the updated dataset at a discount once it has really grown.

Hiring data goes stale; the people who bought it are the people who need it fresh. Once a buyer's
copy is ``refresh_after_days`` (30) old and the dataset on sale has at least
``refresh_min_new_rows`` (25) more rows than theirs, ``offer_refresh`` emails them once:

    Your copy of <title> has 140 rows; today's has 212 (72 new). Update for $9.50 instead of $19
    with your code UPDATEMLAI3F9A2C (valid 14 days): <link that applies it at checkout>

* Each code is single-use (``max_redemptions`` 1), limited to that product, and expires in 14
  days: Stripe enforces all three, so the offer can't leak or run forever.
* One offer per order, never for refunded or disputed orders, never to suppressed addresses or to
  buyers who already bought a newer copy, and never for stale datasets. Shares the per-buyer gap
  with the new-release emails (at most one such email per ``announce_min_gap_days``).
* Every email has the postal address and a reply-"unsubscribe" opt-out. Live mode only (codes are
  real Stripe objects); in dry run the email is logged with a placeholder code. Off with
  ``refresh_offers = false``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from strategies.base import Strategy, TaskContext, TaskResult
from tools.dispatcher import Email

DONE_KEY = "refresh_offered"
CODE_DAYS = 14
PER_CYCLE = 20


def candidates(state: Any, cfg: Any) -> list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
    """(order, asset bought, product on sale now) for every order that's due an offer."""
    from strategies.freshness_guard import niche_of, promotable

    on_sale: dict[str, dict[str, Any]] = {}
    for p in promotable(state):
        if p["kind"] == "lead_directory":
            on_sale.setdefault(niche_of(state, state.get_asset(p["id"]) or {}), p)
    cutoff = (state.clock() - timedelta(days=int(cfg.refresh_after_days))).isoformat(timespec="seconds")
    orders = state._all("SELECT * FROM orders WHERE status = 'delivered' AND kind = 'one_off' AND email IS NOT NULL "
                        "AND occurred_at <= ? ORDER BY id", (cutoff,))
    latest_by_buyer: dict[tuple[str, str], int] = {}
    out = []
    for o in state._all("SELECT * FROM orders WHERE status NOT IN ('refunded', 'disputed') AND email IS NOT NULL"):
        asset = state.get_asset(o["asset_id"]) if o.get("asset_id") else None
        if asset:
            key = (o["email"].lower(), niche_of(state, asset))
            latest_by_buyer[key] = max(latest_by_buyer.get(key, 0), int(o["id"]))
    for o in orders:
        bought = state.get_asset(o["asset_id"]) if o.get("asset_id") else None
        if not bought or bought.get("kind") != "lead_directory":
            continue
        niche = niche_of(state, bought)
        product = on_sale.get(niche)
        if not product or latest_by_buyer.get((o["email"].lower(), niche)) != o["id"]:
            continue  # not on sale, or they already bought a newer copy
        current = state.get_asset(product["id"]) or {}
        if int(current.get("lead_count") or 0) - int(bought.get("lead_count") or 0) >= int(cfg.refresh_min_new_rows):
            out.append((o, bought, {**product, "lead_count": int(current.get("lead_count") or 0),
                                    "product_ref": current.get("product_ref")}))
    return out


def offer_email(cfg: Any, order: dict[str, Any], bought: dict[str, Any], product: dict[str, Any], promo: dict[str, Any],
                link: str) -> Email:
    from strategies.share_kit import money

    old, new = int(bought.get("lead_count") or 0), int(product["lead_count"])
    discounted = round(product["price_cents"] * (100 - int(promo["percent_off"])) / 100)
    until = datetime.fromtimestamp(int(promo["expires_at"]), tz=timezone.utc).strftime("%b %d")
    mailbox = cfg.unsubscribe_email or cfg.sender_email
    lines = ["Hi,", "", f"Your copy of {product['title']} has {old} rows; today's has {new} ({new - old} new).", "",
             f"Update for {money(discounted)} instead of {money(product['price_cents'])} with your code {promo['code']} "
             f"(just for you, valid until {until}). This link applies it:", link, "",
             "No pressure: if your copy still does the job, ignore this.", "",
             "Thanks,", cfg.sender_name or "AutoMonetize", "", "--",
             "You're getting this because you bought this dataset. Reply \"unsubscribe\" and you won't get offers again."]
    if cfg.sender_postal_address:
        lines.append(cfg.sender_postal_address)
    headers = {"List-Unsubscribe": f"<mailto:{mailbox}?subject=unsubscribe>"} if mailbox else {}
    return Email(to=order["email"], subject=f"{new - old} new rows in {product['title']}", body="\n".join(lines),
                 kind="delivery", headers=headers)


class RefreshOffers(Strategy):
    name = "refresh_offers"
    tasks = ("offer_refresh",)

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        from strategies.freshness_guard import niche_of
        from strategies.release_announcer import LAST_SENT
        from tools.attribution import checkout_link
        from tools.promo import Promos, code_text, promo_link

        tools, cfg, state = ctx.tools, ctx.tools.config, ctx.tools.state
        if not cfg.refresh_offers:
            return TaskResult(True, "refresh offers off", {"sent": 0})
        live = tools.dispatcher.live
        if live and not cfg.sender_postal_address.strip():
            return TaskResult(True, "refresh offers wait for sender_postal_address (CAN-SPAM)", {"sent": 0})
        if live and not cfg.stripe_secret_key:
            return TaskResult(True, "refresh offers need the Stripe key", {"sent": 0})
        done = list(state.get(DONE_KEY) or [])
        seen = set(done)
        last_sent: dict[str, str] = dict(state.get(LAST_SENT) or {})
        gap = timedelta(days=float(cfg.announce_min_gap_days))
        now = state.clock()
        promos = Promos(tools.http, cfg.stripe_secret_key) if live else None
        sent = 0
        for order, bought, product in candidates(state, cfg):
            if sent >= PER_CYCLE:
                break
            email = order["email"].lower()
            if order["id"] in seen:
                continue
            if state.is_suppressed(email):
                done.append(order["id"])
                seen.add(order["id"])
                continue
            if email in last_sent and now - datetime.fromisoformat(last_sent[email]) < gap:
                continue
            expires = int((now + timedelta(days=CODE_DAYS)).timestamp())
            code = code_text("UPDATE", niche_of(state, bought)[:4], random_suffix=True)
            try:
                if promos:
                    promo = promos.create(str(product["product_ref"]), code, int(cfg.refresh_discount_pct), expires,
                                          max_redemptions=1, name=f"Refresh order {order['id']}")
                else:
                    promo = {"code": "PREVIEW", "percent_off": int(cfg.refresh_discount_pct), "expires_at": expires}
                link = promo_link(checkout_link(product["url"], "refresh", "refresh_offer"), promo["code"])
                tools.dispatcher.send_transactional(offer_email(cfg, order, bought, product, promo, link),
                                                    audit_key=f"refresh:{order['id']}")
            except Exception as exc:  # noqa: BLE001 - retried next cycle
                state.log_error("refresh_offers", f"refresh offer for order {order['id']} failed: {exc!r}")
                continue
            done.append(order["id"])
            seen.add(order["id"])
            last_sent[email] = state.now()
            sent += 1
        state.set(DONE_KEY, done[-5000:])
        state.set(LAST_SENT, last_sent)
        return TaskResult(True, f"refresh offers: {sent} sent", {"sent": sent})
