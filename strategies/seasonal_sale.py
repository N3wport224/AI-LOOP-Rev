"""Quarterly sale: a short, real, store-wide discount every few months.

A deadline moves people who've been meaning to buy. ``run_sale`` starts a sale every
``sale_every_days`` (90), once the store has been open ``sale_min_store_age_days`` (30):

* One Stripe promotion code (e.g. ``SALEJAN27``), ``sale_pct`` (25%) off every dataset and the
  bundle, valid ``sale_days`` (3). Stripe enforces the end date, so "ends Sunday" is true.
* Past buyers get one email listing what they don't own yet, with links that apply the code (never
  to suppressed addresses; shares the one-email-per-14-days gap with the other offers).
* The share kit shows the code on every product while the sale runs, and the daily report
  mentions it.

Live mode only (nothing is created in Stripe in dry run). Off with ``seasonal_sale = false``.
State: kv ``sale`` (the current or last sale), ``sale_sent:<code>`` (who was emailed).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from strategies.base import Strategy, TaskContext, TaskResult
from tools.dispatcher import Email

KEY = "sale"
KINDS = ("lead_directory", "bundle")
PER_CYCLE = 50


def active_sale(state: Any) -> dict[str, Any] | None:
    sale = state.get(KEY)
    if sale and sale.get("code") and int(sale["expires_at"]) > int(state.clock().timestamp()):
        return sale
    return None


def sale_products(state: Any) -> list[dict[str, Any]]:
    from strategies.freshness_guard import promotable

    out = []
    for p in promotable(state):
        ref = str((state.get_asset(p["id"]) or {}).get("product_ref") or "")
        if p["kind"] in KINDS and ref.startswith("plink_"):
            out.append({**p, "product_ref": ref})
    return out


def store_age_days(state: Any) -> float:
    row = state._one("SELECT MIN(created_at) AS t FROM assets WHERE status = 'published' AND checkout_url LIKE 'https://%'")
    if not row or not row["t"]:
        return 0.0
    return (state.clock() - datetime.fromisoformat(row["t"])).total_seconds() / 86400


def sale_email(cfg: Any, to: str, sale: dict[str, Any], products: list[dict[str, Any]]) -> Email:
    from strategies.share_kit import money
    from tools.attribution import checkout_link
    from tools.promo import promo_link

    ends = datetime.fromtimestamp(int(sale["expires_at"]), tz=timezone.utc).strftime("%A %b %d")
    mailbox = cfg.unsubscribe_email or cfg.sender_email
    lines = ["Hi,", "", f"A short sale: {sale['percent_off']}% off every dataset until {ends}, with code {sale['code']}.", ""]
    for p in products:
        price = int(p["price_cents"])
        lines += [f"- {p['title']}: {money(round(price * (100 - int(sale['percent_off'])) / 100))} instead of {money(price)}",
                  "  " + promo_link(checkout_link(p["url"], "sale", sale["code"].lower()), sale["code"])]
    lines += ["", "The links apply the code. After the sale, prices go back to normal.", "", "Thanks,", cfg.sender_name or "AutoMonetize",
              "", "--", "You're getting this because you bought a dataset. Reply \"unsubscribe\" and you won't get offers again."]
    if cfg.sender_postal_address:
        lines.append(cfg.sender_postal_address)
    headers = {"List-Unsubscribe": f"<mailto:{mailbox}?subject=unsubscribe>"} if mailbox else {}
    return Email(to=to, subject=f"{sale['percent_off']}% off every dataset until {ends}", body="\n".join(lines), kind="delivery",
                 headers=headers)


class SeasonalSale(Strategy):
    name = "seasonal_sale"
    tasks = ("run_sale",)

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        tools, cfg, state = ctx.tools, ctx.tools.config, ctx.tools.state
        if not cfg.seasonal_sale:
            return TaskResult(True, "seasonal sale off", {})
        sale = active_sale(state)
        if not sale:
            started = self.maybe_start(tools)
            if not started:
                return TaskResult(True, "no sale running", {})
            sale = started
        sent = self.announce(tools, sale)
        return TaskResult(True, f"sale {sale['code']} running; {sent} email(s) sent", {"sent": sent, "code": sale["code"]})

    def maybe_start(self, tools: Any) -> dict[str, Any] | None:
        from tools.promo import Promos, code_text

        cfg, state = tools.config, tools.state
        last = state.get(KEY) or {}
        now = state.clock()
        if last.get("started_at") and now - datetime.fromisoformat(last["started_at"]) < timedelta(days=int(cfg.sale_every_days)):
            return None
        if store_age_days(state) < float(cfg.sale_min_store_age_days):
            return None
        if not tools.dispatcher.live or not cfg.stripe_secret_key:
            return None
        products = sale_products(state)
        if not products:
            return None
        code = code_text("SALE", now.strftime("%b%y"))
        expires = int((now + timedelta(days=int(cfg.sale_days))).timestamp())
        try:
            rec = Promos(tools.http, cfg.stripe_secret_key).create_for_links(
                [p["product_ref"] for p in products], code, int(cfg.sale_pct), expires, name=f"Sale {now:%b %Y}")
        except Exception as exc:  # noqa: BLE001 - retried next cycle
            state.log_error("seasonal_sale", f"couldn't start the sale: {exc!r}")
            return None
        sale = {**rec, "started_at": state.now(), "urls": [p["url"] for p in products]}
        state.set(KEY, sale)
        state.log_action(int(state.get("iteration", 0)), None, "sale", "ok",
                         f"{code}: {cfg.sale_pct}% off {len(products)} product(s) for {cfg.sale_days} days")
        return sale

    def announce(self, tools: Any, sale: dict[str, Any]) -> int:
        from strategies.bundle_upgrade import owners
        from strategies.release_announcer import LAST_SENT

        cfg, state = tools.config, tools.state
        if not cfg.sender_postal_address.strip():
            return 0
        sent_key = f"sale_sent:{sale['code']}"
        done = set(state.get(sent_key) or [])
        last_sent: dict[str, str] = dict(state.get(LAST_SENT) or {})
        gap = timedelta(days=float(cfg.announce_min_gap_days))
        now = state.clock()
        products = [p for p in sale_products(state) if p["url"] in set(sale.get("urls") or [])]
        bought = {}
        for o in state._all("SELECT email, asset_id FROM orders WHERE status = 'delivered' AND email IS NOT NULL"):
            asset = state.get_asset(o["asset_id"]) if o.get("asset_id") else None
            bought.setdefault(o["email"].lower(), set()).add((asset or {}).get("checkout_url"))
        sent = 0
        for email in sorted(owners(state, 0)):
            if sent >= PER_CYCLE:
                break
            if email in done or state.is_suppressed(email):
                continue
            if email in last_sent and now - datetime.fromisoformat(last_sent[email]) < gap:
                continue
            offer = [p for p in products if p["url"] not in bought.get(email, set())]
            if not offer:
                continue
            try:
                tools.dispatcher.send_transactional(sale_email(cfg, email, sale, offer), audit_key=f"sale:{sale['code']}:{email}")
            except Exception as exc:  # noqa: BLE001 - retried next cycle
                state.log_error("seasonal_sale", f"sale email to {email} failed: {exc!r}")
                continue
            done.add(email)
            last_sent[email] = state.now()
            sent += 1
        state.set(sent_key, sorted(done))
        state.set(LAST_SENT, last_sent)
        return sent
