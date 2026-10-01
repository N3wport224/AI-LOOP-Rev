"""Bundle upgrade credit: past buyers can get every dataset, and what they already paid counts.

Once the all-datasets bundle is on sale (``strategies/bundle_engine.py``), ``offer_bundle_upgrade``
emails each buyer who owns some but not all of its niches, ``bundle_upgrade_after_days`` (10) after
their latest purchase:

    You have Python Remote. The bundle has all 3 datasets for $34; your $19 counts, so it's $15
    for you with code UPG3F9A2C (14 days).

* The credit is what they actually paid for datasets (refunded and disputed orders don't count),
  capped so they always pay at least $1. It's a single-use Stripe code, fixed amount off, limited
  to the bundle product, valid 14 days, on a link that applies it.
* Once per buyer per bundle link; never to suppressed addresses; shares the one-email-per-14-days
  gap with the other offers. Postal address and opt-out in every email; live sending waits for the
  postal address. In dry run nothing is created in Stripe. Off with ``bundle_upgrade = false``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from strategies.base import Strategy, TaskContext, TaskResult
from tools.dispatcher import Email

DONE = "bundle_upgrade_offered"   # [f"{bundle url}|{email}"]
CODE_DAYS = 14
PER_CYCLE = 20


def live_bundle(state: Any) -> dict[str, Any] | None:
    from strategies.freshness_guard import promotable

    return next((p for p in promotable(state) if p["kind"] == "bundle"), None)


def bundle_niches(state: Any) -> set[str]:
    from strategies.freshness_guard import niche_of
    from tools.catalog import live_products

    return {niche_of(state, state.get_asset(p["id"]) or {}) for p in live_products(state) if p["kind"] == "lead_directory"}


def owners(state: Any, after_days: int) -> dict[str, dict[str, Any]]:
    """email -> {niches, paid_cents, last} for dataset buyers whose latest purchase is old enough."""
    from strategies.freshness_guard import niche_of

    out: dict[str, dict[str, Any]] = {}
    for o in state._all("SELECT * FROM orders WHERE status = 'delivered' AND kind = 'one_off' AND email IS NOT NULL"):
        asset = state.get_asset(o["asset_id"]) if o.get("asset_id") else None
        if not asset:
            continue
        rec = out.setdefault(o["email"].lower(), {"niches": set(), "paid_cents": 0, "last": "", "bundle": False})
        if asset["kind"] == "bundle":
            rec["bundle"] = True
        elif asset["kind"] == "lead_directory":
            rec["niches"].add(niche_of(state, asset))
            rec["paid_cents"] += int(o["gross_cents"] or 0)
        rec["last"] = max(rec["last"], o["occurred_at"])
    cutoff = (state.clock() - timedelta(days=after_days)).isoformat(timespec="seconds")
    return {e: r for e, r in out.items() if not r["bundle"] and r["niches"] and r["last"] <= cutoff}


def upgrade_email(cfg: Any, to: str, owned: int, total: int, bundle: dict[str, Any], credit: int, promo: dict[str, Any],
                  link: str) -> Email:
    from strategies.share_kit import money

    price = int(bundle["price_cents"])
    until = datetime.fromtimestamp(int(promo["expires_at"]), tz=timezone.utc).strftime("%b %d")
    mailbox = cfg.unsubscribe_email or cfg.sender_email
    lines = ["Hi,", "", f"You have {owned} of my {total} hiring datasets. The bundle has all {total} for {money(price)}, "
             f"and what you already paid counts: it's {money(price - credit)} for you with code {promo['code']} "
             f"(just for you, until {until}). This link applies it:", link, "",
             "If you only need the one you have, ignore this.", "", "Thanks,", cfg.sender_name or "AutoMonetize", "", "--",
             "You're getting this because you bought a dataset. Reply \"unsubscribe\" and you won't get offers again."]
    if cfg.sender_postal_address:
        lines.append(cfg.sender_postal_address)
    headers = {"List-Unsubscribe": f"<mailto:{mailbox}?subject=unsubscribe>"} if mailbox else {}
    return Email(to=to, subject=f"All {total} datasets, with your {money(credit)} counted", body="\n".join(lines), kind="delivery",
                 headers=headers)


class BundleUpgrade(Strategy):
    name = "bundle_upgrade"
    tasks = ("offer_bundle_upgrade",)

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        from strategies.release_announcer import LAST_SENT
        from tools.attribution import checkout_link
        from tools.promo import Promos, code_text, promo_link

        tools, cfg, state = ctx.tools, ctx.tools.config, ctx.tools.state
        if not cfg.bundle_upgrade:
            return TaskResult(True, "bundle upgrade offers off", {"sent": 0})
        bundle = live_bundle(state)
        if not bundle:
            return TaskResult(True, "no bundle on sale", {"sent": 0})
        live = tools.dispatcher.live
        if live and not (cfg.sender_postal_address.strip() and cfg.stripe_secret_key):
            return TaskResult(True, "bundle upgrade offers wait for the postal address and Stripe key", {"sent": 0})
        ref = str((state.get_asset(bundle["id"]) or {}).get("product_ref") or "")
        if not ref.startswith("plink_"):
            return TaskResult(True, "bundle has no Payment Link", {"sent": 0})
        total = len(bundle_niches(state))
        done = list(state.get(DONE) or [])
        seen = set(done)
        last_sent: dict[str, str] = dict(state.get(LAST_SENT) or {})
        gap = timedelta(days=float(cfg.announce_min_gap_days))
        now = state.clock()
        promos = Promos(tools.http, cfg.stripe_secret_key) if live else None
        sent = 0
        for email, rec in sorted(owners(state, int(cfg.bundle_upgrade_after_days)).items()):
            key = f"{bundle['url']}|{email}"
            owned = len(rec["niches"] & bundle_niches(state))
            if sent >= PER_CYCLE:
                break
            if key in seen or owned >= total or state.is_suppressed(email):
                continue
            if email in last_sent and now - datetime.fromisoformat(last_sent[email]) < gap:
                continue
            credit = min(int(rec["paid_cents"]), int(bundle["price_cents"]) - 100)
            if credit < 100:
                continue
            expires = int((now + timedelta(days=CODE_DAYS)).timestamp())
            try:
                if promos:
                    promo = promos.create(ref, code_text("UPG", "", random_suffix=True), expires_at=expires, max_redemptions=1,
                                          amount_off_cents=credit, currency=cfg.currency, name="Bundle upgrade credit")
                else:
                    promo = {"code": "PREVIEW", "amount_off_cents": credit, "expires_at": expires}
                link = promo_link(checkout_link(bundle["url"], "upgrade", "bundle_upgrade"), promo["code"])
                tools.dispatcher.send_transactional(upgrade_email(cfg, email, owned, total, bundle, credit, promo, link),
                                                    audit_key=f"upgrade:{key}")
            except Exception as exc:  # noqa: BLE001 - retried next cycle
                state.log_error("bundle_upgrade", f"upgrade offer to {email} failed: {exc!r}")
                continue
            done.append(key)
            seen.add(key)
            last_sent[email] = state.now()
            sent += 1
        state.set(DONE, done[-5000:])
        state.set(LAST_SENT, last_sent)
        return TaskResult(True, f"bundle upgrade offers: {sent} sent", {"sent": sent})
