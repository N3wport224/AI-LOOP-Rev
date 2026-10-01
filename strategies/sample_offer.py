"""Sample-to-paid offer: one time-limited discount for people who took the free sample.

Free-sample signups (``strategies/lead_magnet.py``) get the weekly Pulse with upgrade buttons. Some
never take the step. ``offer_sample_upgrade`` writes once to each confirmed signup who joined at
least ``sample_offer_after_days`` (14) ago and hasn't bought anything: ``sample_offer_pct`` (25%)
off the full dataset of their niche, with a single-use Stripe code valid 7 days and a link that
applies it.

* Only confirmed free-sample signups (they asked for email from us), once each, never to
  unsubscribed or suppressed addresses, never to buyers or subscribers, never for stale datasets.
* Same compliance as the weekly email: their one-click unsubscribe link (RFC 8058 header) and the
  postal address; it waits until both a postal address and the public unsubscribe URL exist. In
  dry run nothing is created in Stripe. Off with ``sample_offer = false``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from strategies.base import Strategy, TaskContext, TaskResult
from strategies.offer_tuner import offer_pct
from tools import contact_policy as contact
from tools.dispatcher import Email

DONE = "sample_offer_sent"
CODE_DAYS = 7
PER_CYCLE = 30


def candidates(state: Any, days: int) -> list[dict[str, Any]]:
    cutoff = (state.clock() - timedelta(days=days)).isoformat(timespec="seconds")
    buyers = {(r["email"] or "").lower() for r in state._all("SELECT email FROM orders WHERE email IS NOT NULL")}
    buyers |= {(r["email"] or "").lower() for r in state._all("SELECT email FROM subscribers WHERE tier = 'paid' AND email IS NOT NULL")}
    rows = state._all("SELECT * FROM subscribers WHERE tier = 'free' AND subscription_status = 'active' AND email IS NOT NULL "
                      "AND niche IS NOT NULL AND started_at <= ? ORDER BY id", (cutoff,))
    return [r for r in rows if r["email"].lower() not in buyers]


def dataset_for(state: Any, niche: str) -> dict[str, Any] | None:
    from strategies.freshness_guard import niche_of, promotable

    for p in promotable(state):
        asset = state.get_asset(p["id"]) or {}
        if p["kind"] == "lead_directory" and niche_of(state, asset) == niche and str(asset.get("product_ref") or "").startswith("plink_"):
            return {**p, "product_ref": asset["product_ref"]}
    return None


def offer_email(cfg: Any, sub: dict[str, Any], product: dict[str, Any], promo: dict[str, Any], link: str, unsubscribe: str) -> Email:
    from strategies.lead_magnet import _footer_text, _list_unsubscribe
    from strategies.share_kit import money

    price = int(product["price_cents"])
    until = datetime.fromtimestamp(int(promo["expires_at"]), tz=timezone.utc).strftime("%b %d")
    lines = ["Hi,", "", f"You've had the free sample of {product['title']} for a couple of weeks. If the full list would help, "
             f"it's {money(round(price * (100 - int(promo['percent_off'])) / 100))} instead of {money(price)} with code "
             f"{promo['code']} until {until} (just for you). This link applies it:", link, "",
             "If the weekly sample is all you need, that's fine: nothing changes.", "", cfg.sender_name or "AutoMonetize", "",
             _footer_text(cfg, unsubscribe)]
    return Email(to=sub["email"], subject=f"{promo['percent_off']}% off the full {product['title']} (until {until})",
                 body="\n".join(lines), kind="nurture", headers=_list_unsubscribe(cfg, unsubscribe))


class SampleOffer(Strategy):
    name = "sample_offer"
    tasks = ("offer_sample_upgrade",)

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        from strategies.lead_magnet import link as lead_link
        from strategies.lead_magnet import nurture_problems
        from tools.attribution import checkout_link
        from tools.promo import Promos, code_text, promo_link

        tools, cfg, state = ctx.tools, ctx.tools.config, ctx.tools.state
        if not cfg.sample_offer:
            return TaskResult(True, "sample offers off", {"sent": 0})
        problems = nurture_problems(tools)
        if problems:
            return TaskResult(True, "sample offers wait: " + "; ".join(problems), {"sent": 0})
        live = tools.dispatcher.live
        if live and not cfg.stripe_secret_key:
            return TaskResult(True, "sample offers need the Stripe key", {"sent": 0})
        done = list(state.get(DONE) or [])
        seen = set(done)
        promos = Promos(tools.http, cfg.stripe_secret_key) if live else None
        sent = 0
        for sub in candidates(state, int(cfg.sample_offer_after_days)):
            if sent >= PER_CYCLE:
                break
            if sub["id"] in seen:
                continue
            product = dataset_for(state, sub["niche"])
            if state.is_suppressed(sub["email"]) or not product or not sub.get("token"):
                if state.is_suppressed(sub["email"]):
                    done.append(sub["id"])
                    seen.add(sub["id"])
                continue
            if contact.blocked(state, cfg, sub["email"], "sample"):
                continue
            expires = int((state.clock() + timedelta(days=CODE_DAYS)).timestamp())
            try:
                if promos:
                    promo = promos.create(product["product_ref"], code_text("SAMPLE", "", random_suffix=True),
                                          offer_pct(state, cfg, "sample"), expires, max_redemptions=1, name="Sample upgrade")
                else:
                    promo = {"code": "PREVIEW", "percent_off": offer_pct(state, cfg, "sample"), "expires_at": expires}
                link = promo_link(checkout_link(product["url"], "leadmagnet", "sample_offer"), promo["code"])
                tools.dispatcher.send_transactional(
                    offer_email(cfg, sub, product, promo, link, lead_link(cfg, "unsubscribe", sub["token"])),
                    audit_key=f"sample_offer:{sub['id']}")
            except Exception as exc:  # noqa: BLE001 - retried next cycle
                state.log_error("sample_offer", f"sample offer to subscriber {sub['id']} failed: {exc!r}")
                continue
            done.append(sub["id"])
            seen.add(sub["id"])
            contact.record(state, sub["email"], "sample", str(sub["id"]))
            sent += 1
        state.set(DONE, done[-10000:])
        return TaskResult(True, f"sample offers: {sent} sent", {"sent": sent})
