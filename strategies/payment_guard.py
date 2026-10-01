"""Payment guard: card testing, accidental double purchases, and sales tax.

Three read-mostly checks on the Stripe side of the business (task ``guard_payments``):

**Phase 52: card-testing guard.** Fraudsters test stolen cards on cheap public checkouts: a burst
of failed charges. Every 15 minutes the guard counts failed charges in the last hour; at
``card_testing_threshold`` (10) or more you get an alert (at most every 6 hours) with what to do:
turn on Stripe Radar's rules, and if it continues, deactivate the link being hit. The guard doesn't
switch links off itself: that stops real sales too, so it's your call.

**Phase 53: duplicate-purchase guard.** A buyer who pays twice for the same dataset within 7 days
almost always did it by accident. You get one alert per case with the order ids, so you can refund
the extra one in Stripe (refunds stay your decision; ``sync_refunds`` then records it). Also on the
to-do list.

**Phase 54: Stripe Tax.** Once a day: if Stripe Tax is active on the account, every new Payment
Link gets ``automatic_tax`` and existing live links are switched over once. If it isn't, the doctor
says so (whether you must collect sales tax or VAT depends on where you and your buyers are).

State: kv ``payment_guard``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from strategies.base import Strategy, TaskContext, TaskResult

KEY = "payment_guard"
STRIPE_API = "https://api.stripe.com/v1"
CARD_CHECK_MINUTES = 15
CARD_ALERT_HOURS = 6
DUPLICATE_DAYS = 7


def duplicates(state: Any) -> list[dict[str, Any]]:
    """Pairs of delivered/paid orders: same buyer, same dataset, within DUPLICATE_DAYS."""
    from strategies.freshness_guard import niche_of

    rows = state._all("SELECT * FROM orders WHERE status IN ('paid', 'delivered', 'delivering') AND kind = 'one_off' "
                      "AND email IS NOT NULL AND asset_id IS NOT NULL ORDER BY occurred_at")
    seen: dict[tuple[str, str, str], dict[str, Any]] = {}
    out = []
    for o in rows:
        asset = state.get_asset(o["asset_id"]) or {}
        key = (o["email"].lower(), asset.get("kind", ""), niche_of(state, asset))
        first = seen.get(key)
        if first and datetime.fromisoformat(o["occurred_at"]) - datetime.fromisoformat(first["occurred_at"]) <= \
                timedelta(days=DUPLICATE_DAYS):
            out.append({"email": o["email"], "title": asset.get("title", "dataset"), "first": first["order_id"],
                        "second": o["order_id"], "amount_cents": int(o["gross_cents"] or 0)})
        else:
            seen[key] = o
    return out


class PaymentGuard(Strategy):
    name = "payment_guard"
    tasks = ("guard_payments",)

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        tools, cfg, state = ctx.tools, ctx.tools.config, ctx.tools.state
        data: dict[str, Any] = dict(state.get(KEY) or {})
        now = state.clock()
        notes = []
        # -- 53: duplicates (local data, every cycle) --------------------------------------------
        known = set(data.get("duplicates_alerted") or [])
        for d in duplicates(state):
            if d["second"] in known:
                continue
            state.log_error("payment_guard", f"{d['email']} bought {d['title']} twice within {DUPLICATE_DAYS} days (orders "
                                             f"{d['first']} and {d['second']}). If it was an accident, refund "
                                             f"${d['amount_cents'] / 100:.2f} in Stripe → Payments.", kind="alert")
            known.add(d["second"])
            notes.append("duplicate purchase flagged")
        data["duplicates_alerted"] = sorted(known)[-500:]
        data["open_duplicates"] = [d for d in duplicates(state) if d["second"] in known
                                   and now - datetime.fromisoformat(state.get_order("stripe", d["second"])["occurred_at"])
                                   <= timedelta(days=14)] if known else []
        if cfg.stripe_secret_key and not cfg.dry_run:
            headers = {"Authorization": f"Bearer {cfg.stripe_secret_key}"}
            # -- 52: card testing ---------------------------------------------------------------
            last = data.get("card_checked_at")
            if not last or now - datetime.fromisoformat(last) >= timedelta(minutes=CARD_CHECK_MINUTES):
                try:
                    since = int((now - timedelta(hours=1)).timestamp())
                    charges = tools.http.get_json(f"{STRIPE_API}/charges", params={"created[gte]": since, "limit": 100},
                                                  headers=headers, check_robots=False).get("data") or []
                    failed = sum(1 for c in charges if c.get("status") == "failed")
                    data.update(card_checked_at=state.now(), failed_last_hour=failed)
                    alerted = data.get("card_alerted_at")
                    if failed >= int(cfg.card_testing_threshold) and (
                            not alerted or now - datetime.fromisoformat(alerted) >= timedelta(hours=CARD_ALERT_HOURS)):
                        state.log_error("payment_guard", f"{failed} failed card payments in the last hour: probably card "
                                                         "testing (fraudsters checking stolen cards). In Stripe → Radar → Rules, "
                                                         "turn on blocking for failed CVC and postal-code checks; if it "
                                                         "continues, deactivate the payment link being hit (Payment Links).",
                                        kind="alert")
                        data["card_alerted_at"] = state.now()
                        notes.append(f"card testing suspected ({failed} failures)")
                except Exception as exc:  # noqa: BLE001 - retried next time
                    state.log_error("payment_guard", f"couldn't read charges: {exc!r}")
            # -- 54: Stripe Tax -------------------------------------------------------------------
            tax_last = data.get("tax_checked_at")
            if not tax_last or now - datetime.fromisoformat(tax_last) >= timedelta(hours=24):
                notes += self.tax(tools, data, headers)
        state.set(KEY, data)
        return TaskResult(True, "payments: " + ("; ".join(notes) or "ok"), {"notes": len(notes)})

    def tax(self, tools: Any, data: dict[str, Any], headers: dict[str, str]) -> list[str]:
        from tools.catalog import live_products
        from tools.storefront.stripe_pages_publisher import flatten_form

        cfg, state = tools.config, tools.state
        data["tax_checked_at"] = state.now()
        try:
            settings = tools.http.get_json(f"{STRIPE_API}/tax/settings", headers=headers, check_robots=False)
        except Exception as exc:  # noqa: BLE001
            data["tax_status"] = f"unknown ({type(exc).__name__})"
            return []
        status = str(settings.get("status") or "pending")
        data["tax_status"] = status
        if status != "active":
            cfg.stripe_automatic_tax = False
            return []
        cfg.stripe_automatic_tax = True
        done = set(data.get("tax_links") or [])
        switched = 0
        for p in live_products(state):
            ref = str((state.get_asset(p["id"]) or {}).get("product_ref") or "")
            if not ref.startswith("plink_") or ref in done:
                continue
            try:
                tools.http.post(f"{STRIPE_API}/payment_links/{ref}", headers=headers, check_robots=False,
                                data=flatten_form({"automatic_tax": {"enabled": True}}))
                done.add(ref)
                switched += 1
            except Exception as exc:  # noqa: BLE001 - retried tomorrow
                state.log_error("payment_guard", f"couldn't turn on automatic tax for {ref}: {exc!r}")
        data["tax_links"] = sorted(done)
        if switched:
            state.log_action(int(state.get("iteration", 0)), None, "tax", "ok", f"automatic tax on {switched} payment link(s)")
            return [f"automatic tax on {switched} link(s)"]
        return []
