"""More ways to pay for the same data: a team license and a yearly plan.

**Phase 55: team license** (task ``publish_team_license``). Companies often want the whole team to
use a dataset, and pay more for it. For each dataset on sale, a second product: the same file plus
a ``LICENSE-TEAM.txt`` allowing use by up to ``team_license_seats`` (10) people in one organisation,
at ``team_license_multiplier`` (3x) the price, rounded to whole dollars. It's offered on the
dataset's own page, not as a separate page or post. When a new dataset version comes out, the team
zip is rebuilt in place (same link); when the price changes, a new link replaces the old one.

**Phase 56: yearly plan** (task ``publish_annual_plan``). For each niche with a monthly
subscription, a yearly option at ``annual_months_paid`` (10) months' price ("2 months free"),
billed once a year, with the same weekly updates. Subscribers created from it are synced and
delivered exactly like monthly ones.
"""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any

from strategies.base import Strategy, TaskContext, TaskResult

TEAM_KIND = "team_license"
ANNUAL_KIND = "subscription_annual"


def team_price(dataset_price: int, multiplier: float) -> int:
    return max(100, int(round(dataset_price * multiplier / 100.0)) * 100)


def annual_price(monthly_cents: int, months: int) -> int:
    return int(monthly_cents) * int(months)


def license_text(cfg: Any, title: str) -> str:
    return (f"Team license: {title}\n\nThis purchase lets up to {cfg.team_license_seats} people in one organisation use this "
            "dataset for their work. Please don't share it outside your organisation or resell it.\n\n"
            f"Questions: reply to your order email.\n{cfg.sender_name or ''}\n")


def team_zip(tools: Any, dataset: dict[str, Any]) -> bytes:
    src = tools.files.read_bytes(dataset["path"])
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(src)) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            zout.writestr(item, zin.read(item.filename))
        zout.writestr("LICENSE-TEAM.txt", license_text(tools.config, dataset["title"]))
    return out.getvalue()


def plan_for(state: Any, kind: str, niche: str) -> dict[str, Any] | None:
    return next((a for a in state.list_assets() if a["kind"] == kind and a.get("niche") == niche
                 and a.get("status") == "published" and a.get("checkout_url")), None)


class Plans(Strategy):
    name = "plans"
    tasks = ("publish_team_license", "publish_annual_plan", "checkout_thank_you")

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        from strategies.subscription_engine import stripe_storefront

        tools = ctx.tools
        if task == "checkout_thank_you":
            return self.thank_you(tools)
        sf = stripe_storefront(tools)
        if sf is None:
            return TaskResult(True, "plans need Stripe", {"published": 0})
        if not (tools.dispatcher.can_deliver() or tools.config.allow_manual_fulfillment):
            return TaskResult(True, "plans wait until email delivery works", {"published": 0})
        return self.team(tools, sf) if task == "publish_team_license" else self.annual(tools, sf)

    # -- Phase 57 ---------------------------------------------------------------------------------
    def thank_you(self, tools: Any) -> TaskResult:
        """Point every live Payment Link at the site's thank-you page, once that page is online."""
        from tools.catalog import live_products
        from tools.storefront.stripe_pages_publisher import flatten_form, thank_you_redirect

        cfg, state = tools.config, tools.state
        if not (cfg.checkout_thank_you and cfg.pages_base_url and cfg.stripe_secret_key) or cfg.dry_run:
            return TaskResult(True, "thank-you redirect off (needs the public site and live payments)", {"switched": 0})
        if not state.get("thanks_page_live"):
            try:
                tools.http.get(f"{cfg.pages_base_url.rstrip('/')}/thanks/", check_robots=False, attempts=1)
            except Exception:  # noqa: BLE001 - not published yet: links keep Stripe's own confirmation
                return TaskResult(True, "thank-you page not online yet", {"switched": 0})
            state.set("thanks_page_live", True)
        cfg.checkout_thank_you_live = True  # new links get the redirect from now on
        done = set(state.get("thanks_links") or [])
        switched = 0
        for p in live_products(state):
            ref = str((state.get_asset(p["id"]) or {}).get("product_ref") or "")
            if not ref.startswith("plink_") or ref in done:
                continue
            try:
                tools.http.post(f"https://api.stripe.com/v1/payment_links/{ref}", check_robots=False,
                                headers={"Authorization": f"Bearer {cfg.stripe_secret_key}"},
                                data=flatten_form({"after_completion": thank_you_redirect(cfg)}))
                done.add(ref)
                switched += 1
            except Exception as exc:  # noqa: BLE001 - retried next cycle
                state.log_error("plans", f"couldn't set the thank-you page on {ref}: {exc!r}")
        state.set("thanks_links", sorted(done))
        return TaskResult(True, f"thank-you page on {switched} more link(s)", {"switched": switched})

    # -- Phase 55 ---------------------------------------------------------------------------------
    def team(self, tools: Any, sf: Any) -> TaskResult:
        from strategies.bundle_engine import components

        cfg, state = tools.config, tools.state
        if not cfg.team_license:
            return TaskResult(True, "team licenses off", {"published": 0})
        published = refreshed = 0
        for niche, dataset in sorted(components(state).items()):
            if not tools.files.exists(dataset["path"]):
                continue
            price = team_price(int(dataset["price_cents"]), float(cfg.team_license_multiplier))
            existing = plan_for(state, TEAM_KIND, niche)
            meta = json.loads((existing or {}).get("kind_meta") or "{}")
            if existing and int(existing["price_cents"]) == price:
                if meta.get("component") != dataset["id"]:
                    tools.files.write_bytes(existing["path"], team_zip(tools, dataset))
                    state.update_asset(existing["id"], kind_meta=json.dumps({"component": dataset["id"]}))
                    refreshed += 1
                continue
            rel = f"assets/{niche}/team-license-{price}.zip"
            tools.files.write_bytes(rel, team_zip(tools, dataset))
            title = f"{dataset['title']}: team license ({cfg.team_license_seats} people)"
            aid = state.add_asset(dataset["hypothesis_id"], TEAM_KIND, title, rel, 1, int(dataset["lead_count"] or 0), price)
            link_id, url, _ = sf.create_link(title, f"{dataset['title']} for up to {cfg.team_license_seats} people in one "
                                                    "organisation.", price,
                                             {"hypothesis_id": str(dataset["hypothesis_id"]), "asset_id": str(aid), "kind": TEAM_KIND,
                                              "niche": niche})
            state.update_asset(aid, provider="stripe", product_ref=link_id, checkout_url=url, status="published", niche=niche,
                               kind_meta=json.dumps({"component": dataset["id"]}))
            if existing:
                if str(existing.get("product_ref") or "").startswith("plink_"):
                    sf.deactivate(existing["product_ref"])
                state.update_asset(existing["id"], status="retired")
            published += 1
        return TaskResult(True, f"team licenses: {published} published, {refreshed} refreshed",
                          {"published": published, "refreshed": refreshed})

    # -- Phase 56 ---------------------------------------------------------------------------------
    def annual(self, tools: Any, sf: Any) -> TaskResult:
        from strategies.subscription_engine import subscription_asset

        cfg, state = tools.config, tools.state
        if not cfg.annual_plan:
            return TaskResult(True, "yearly plans off", {"published": 0})
        niches = sorted({a["niche"] for a in state.list_assets() if a["kind"] == "subscription" and a.get("niche")
                         and a.get("checkout_url")})
        published = 0
        for niche in niches:
            monthly = subscription_asset(state, niche)
            if not monthly or json.loads(monthly.get("kind_meta") or "{}").get("interval", "month") != "month":
                continue
            price = annual_price(int(monthly["price_cents"]), int(cfg.annual_months_paid))
            existing = plan_for(state, ANNUAL_KIND, niche)
            if existing and int(existing["price_cents"]) == price:
                continue
            title = f"{monthly['title']} (yearly)"
            aid = state.add_asset(monthly["hypothesis_id"], ANNUAL_KIND, title, monthly["path"], 1, int(monthly["lead_count"] or 0),
                                  price)
            link_id, url, price_id = sf.create_subscription_link(
                title, f"The same weekly updates, billed yearly: {12 - int(cfg.annual_months_paid)} months free.", price, "year",
                {"hypothesis_id": str(monthly["hypothesis_id"]), "asset_id": str(aid), "niche": niche, "version": f"y{price}"})
            state.update_asset(aid, provider="stripe", product_ref=link_id, checkout_url=url, status="published", niche=niche,
                               kind_meta=json.dumps({"interval": "year", "price_id": price_id}))
            if existing:
                if str(existing.get("product_ref") or "").startswith("plink_"):
                    sf.deactivate(existing["product_ref"])
                state.update_asset(existing["id"], status="retired")
            published += 1
        return TaskResult(True, f"yearly plans: {published} published", {"published": published})
