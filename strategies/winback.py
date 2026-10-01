"""Subscriber win-back: one honest invitation back after a subscription ends.

``win_back`` writes once to each subscriber whose subscription was canceled between
``winback_after_days`` (7) and 30 days ago: what changed in their niche since they left (real
numbers from the current dataset) and ``winback_discount_pct`` (50%) off the first month back with
a single-use Stripe code, valid 14 days, on a link that applies it.

* Only past paying subscribers, once per subscription, never to suppressed addresses or anyone
  who already has an active subscription again (API subscribers are not contacted).
* Postal address and reply-"unsubscribe" opt-out in every email (the support desk honours it).
  Live sending waits for ``sender_postal_address``. In dry run the email is logged with a
  placeholder code and nothing is created in Stripe. Off with ``winback = false``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from strategies.base import Strategy, TaskContext, TaskResult
from tools.dispatcher import Email

DONE = "winback_offered"
WINDOW_DAYS = 30
CODE_DAYS = 14
PER_CYCLE = 20


def due(state: Any, cfg: Any) -> list[dict[str, Any]]:
    now = state.clock()
    newest = (now - timedelta(days=int(cfg.winback_after_days))).isoformat(timespec="seconds")
    oldest = (now - timedelta(days=WINDOW_DAYS)).isoformat(timespec="seconds")
    active = {(r["email"] or "").lower() for r in state._all(
        "SELECT email FROM subscribers WHERE tier = 'paid' AND subscription_status IN ('active', 'trialing', 'past_due') "
        "AND email IS NOT NULL")}
    rows = state._all("SELECT * FROM subscribers WHERE tier = 'paid' AND subscription_status = 'canceled' AND email IS NOT NULL AND niche IS NOT NULL "
                      "AND canceled_at IS NOT NULL AND canceled_at <= ? AND canceled_at >= ? ORDER BY id", (newest, oldest))
    return [r for r in rows if r["email"].lower() not in active]


def winback_email(cfg: Any, sub: dict[str, Any], product: dict[str, Any], promo: dict[str, Any], link: str, facts: list[str]) -> Email:
    from strategies.share_kit import money

    price = int(product["price_cents"] or 0)
    first = round(price * (100 - int(promo["percent_off"])) / 100)
    until = datetime.fromtimestamp(int(promo["expires_at"]), tz=timezone.utc).strftime("%b %d")
    mailbox = cfg.unsubscribe_email or cfg.sender_email
    lines = ["Hi,", "", f"You used to get the {product['title']} updates. Here's where the data stands now:"]
    lines += [f"- {f}" for f in facts] or ["- refreshed every week from new job postings"]
    lines += ["", f"If you'd like it back, your first month is {money(first)} instead of {money(price)} with code {promo['code']} "
              f"(just for you, until {until}). This link applies it:", link, "",
              "If not, no worries: this is the only time I'll ask.", "",
              "Thanks,", cfg.sender_name or "AutoMonetize", "", "--",
              "You're getting this because you subscribed before. Reply \"unsubscribe\" and you won't hear from me again."]
    if cfg.sender_postal_address:
        lines.append(cfg.sender_postal_address)
    headers = {"List-Unsubscribe": f"<mailto:{mailbox}?subject=unsubscribe>"} if mailbox else {}
    return Email(to=sub["email"], subject=f"{product['title']}: what's changed since you left", body="\n".join(lines),
                 kind="delivery", headers=headers)


class WinBack(Strategy):
    name = "winback"
    tasks = ("win_back",)

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        from strategies.share_kit import facts
        from strategies.subscription_engine import subscription_asset
        from tools.attribution import checkout_link
        from tools.promo import Promos, code_text, promo_link

        tools, cfg, state = ctx.tools, ctx.tools.config, ctx.tools.state
        if not cfg.winback:
            return TaskResult(True, "win-back off", {"sent": 0})
        live = tools.dispatcher.live
        if live and not cfg.sender_postal_address.strip():
            return TaskResult(True, "win-back waits for sender_postal_address (CAN-SPAM)", {"sent": 0})
        if live and not cfg.stripe_secret_key:
            return TaskResult(True, "win-back needs the Stripe key", {"sent": 0})
        done = list(state.get(DONE) or [])
        seen = set(done)
        promos = Promos(tools.http, cfg.stripe_secret_key) if live else None
        sent = 0
        for sub in due(state, cfg):
            if sent >= PER_CYCLE:
                break
            if sub["id"] in seen:
                continue
            product = subscription_asset(state, sub["niche"])
            if state.is_suppressed(sub["email"]) or not product or not str(product.get("product_ref") or "").startswith("plink_"):
                done.append(sub["id"])
                seen.add(sub["id"])
                continue
            expires = int((state.clock() + timedelta(days=CODE_DAYS)).timestamp())
            code = code_text("BACK", sub["niche"][:4], random_suffix=True)
            try:
                if promos:
                    promo = promos.create(product["product_ref"], code, int(cfg.winback_discount_pct), expires,
                                          max_redemptions=1, name=f"Win-back {sub['id']}")
                else:
                    promo = {"code": "PREVIEW", "percent_off": int(cfg.winback_discount_pct), "expires_at": expires}
                link = promo_link(checkout_link(product["checkout_url"], "winback", "winback"), promo["code"])
                tools.dispatcher.send_transactional(
                    winback_email(cfg, sub, product, promo, link,
                                  facts(tools.files, sub["niche"])[:3]),
                    audit_key=f"winback:{sub['id']}")
            except Exception as exc:  # noqa: BLE001 - retried next cycle
                state.log_error("winback", f"win-back for subscriber {sub['id']} failed: {exc!r}")
                continue
            done.append(sub["id"])
            seen.add(sub["id"])
            sent += 1
        state.set(DONE, done[-5000:])
        return TaskResult(True, f"win-back: {sent} sent", {"sent": sent})
