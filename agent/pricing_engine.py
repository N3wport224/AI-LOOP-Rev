"""Dynamic price optimization, bundling and demand-driven expansion.

Every live dataset runs one *price experiment* at a time: a price from ``price_matrix``
($5, $9, $14, $19) with its own Payment Link, so each order is tied to the price that produced it.
Per experiment the engine tracks showcase **views** (GitHub traffic gained since it started),
**checkout initiations** (Checkout Sessions of any status, from webhooks and polling),
**completed orders**, and **cart drop-off** (1 − orders / initiations).

Decisions, evaluated every cycle:

* **Lower**: more than ``pricing_min_views`` (20) views and zero orders after
  ``pricing_window_hours`` (48h) → new Price one tier down.
* **Bundle**: the same condition at the lowest tier → a 2-for-1 bundle with another niche's
  dataset, at the higher of the two prices.
* **Raise**: two or more orders at the current price → try one tier up, unless that tier
  already earned less revenue per view.
* **Explore**: weak conversion (one order, enough views) → test the cheaper tier once.
* **Converge**: once the neighbouring tiers have been tested and did worse per view, stop and
  hold the best tier.
  Revenue per view uses a smoothed conversion estimate (orders + 0.5) / (views + 20.5), so one
  lucky sale doesn't decide it.

Demand: more than two sales (``demand_sales_threshold``) in 24h for a niche → raise scraping depth
for its keyword cluster (more pages and comments, broader keywords) and build a premium
deep-dive add-on at ``premium_price_cents``, at most once a week.
"""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from strategies.base import Strategy, TaskContext, TaskResult
from strategies.digital_asset_packager import ASSET_KIND, niche_title

ALPHA, BETA = 0.5, 20.0


@dataclass
class Decision:
    action: str  # hold | lower | raise | bundle | converge | wait
    price_cents: int | None = None
    reason: str = ""


def revenue_per_view(price_cents: int, orders: int, views: int) -> float:
    return price_cents * (orders + ALPHA) / (views + ALPHA + BETA)


def tiers(matrix: list[int]) -> list[int]:
    return sorted(set(int(p) for p in matrix))


def neighbour(matrix: list[int], price: int, step: int) -> int | None:
    t = tiers(matrix)
    below = [p for p in t if p < price]
    above = [p for p in t if p > price]
    if step < 0:
        return below[-1] if below else None
    return above[0] if above else None


ABANDONED_CHECKOUTS = 3  # started-but-unpaid checkouts with no sale that count as a price signal


def decide(
    exp: dict[str, Any], stats: dict[str, int], history: list[dict[str, Any]], now: datetime,
    matrix: list[int], min_views: int, window_hours: int, can_bundle: bool = True, view_tracking: bool = True,
) -> Decision:
    """Pure decision function: current experiment + its stats + past experiments → next action.

    Signals, strongest first: completed orders, abandoned checkouts (reached Stripe, didn't pay),
    showcase views. Without view tracking, time alone (2 × window with no sale) stands in for
    traffic, so an unsold price can never be held forever.
    """
    price = exp["price_cents"]
    age = now - datetime.fromisoformat(exp["started_at"])
    window = timedelta(hours=window_hours)
    views, orders = stats["views"], stats["orders"]
    initiations = stats.get("initiations", 0)
    here = revenue_per_view(price, orders, views)

    def step_down(reason: str) -> Decision:
        down = neighbour(matrix, price, -1)
        if down is not None:
            return Decision("lower", down, reason)
        if can_bundle:
            return Decision("bundle", price, f"{reason} at the lowest tier")
        return Decision("hold", price, "lowest tier and nothing to bundle with")

    if exp["status"] == "converged":
        recent = stats.get("recent_orders", orders)
        if recent == 0 and age >= 2 * window and (views > min_views or not view_tracking):
            return step_down(f"converged ${price / 100:.0f} stopped selling: demand shifted, re-exploring")
        return Decision("hold", price, "converged")

    def tested(p: int) -> list[dict[str, Any]]:
        return [h for h in history if h["price_cents"] == p and h["id"] != exp["id"]]

    if orders >= 2:
        up = neighbour(matrix, price, +1)
        if up is None:
            return Decision("converge", price, "strong demand at the top tier")
        prior = tested(up)
        if prior and max(revenue_per_view(up, h.get("orders", 0), h.get("views", 0)) for h in prior) <= here:
            return Decision("converge", price, f"${up / 100:.0f} already tested and earned less per view")
        return Decision("raise", up, f"{orders} orders at ${price / 100:.0f}: testing ${up / 100:.0f}")

    if age < window:
        return Decision("wait", price, f"experiment is {age.total_seconds() / 3600:.0f}h old (< {window_hours}h)")

    if orders == 0:
        if initiations >= ABANDONED_CHECKOUTS:
            return step_down(f"{initiations} checkouts abandoned, 0 orders at ${price / 100:.0f}")
        if views > min_views:
            return step_down(f"{views} views, 0 orders in {window_hours}h at ${price / 100:.0f}")
        if not view_tracking and age >= 2 * window:
            return step_down(f"no traffic data; 0 orders in {age.total_seconds() / 3600:.0f}h at ${price / 100:.0f}")

    enough_signal = views > min_views or (not view_tracking and age >= 2 * window)
    if orders >= 1 and enough_signal:
        # Weak but non-zero conversion: explore the cheaper tier once, then settle on the best one seen.
        down = neighbour(matrix, price, -1)
        if down is not None and not tested(down):
            return Decision("lower", down, f"only {orders} order(s) in {views} views at ${price / 100:.0f}: exploring ${down / 100:.0f}")
        best_price, best_rpv = price, here
        for p in (down, neighbour(matrix, price, +1)):
            for h in tested(p) if p is not None else []:
                rpv = revenue_per_view(p, h.get("orders", 0), h.get("views", 0))
                if rpv > best_rpv:
                    best_price, best_rpv = p, rpv
        if best_price == price:
            return Decision("converge", price, "best revenue per view among tested tiers")
        return Decision("lower" if best_price < price else "raise", best_price, f"${best_price / 100:.0f} earned more per view")
    return Decision("hold", price, "not enough signal yet")


class PricingEngine:
    def __init__(self, tools):
        self.tools = tools
        self.cfg = tools.config
        self.state = tools.state

    # -- plumbing -------------------------------------------------------------------
    def storefront(self):
        sf = self.tools.storefront
        return sf if hasattr(sf, "create_link") and sf.configured() and (sf.name != "stripe" or self.cfg.stripe_secret_key) else None

    def views_now(self, hypothesis_id: int) -> int:
        return int(self.state.metrics_for_hypothesis(hypothesis_id).get("views", 0))

    def refresh(self, exp: dict[str, Any]) -> dict[str, int]:
        refs = [exp["product_ref"]] if exp.get("product_ref") else []
        sf = self.tools.storefront
        if refs and hasattr(sf, "session_statuses"):
            started = int(datetime.fromisoformat(exp["started_at"]).timestamp())
            try:
                for s in sf.session_statuses(refs[0], started):
                    self.state.upsert_checkout_session(
                        s["id"], refs[0], s.get("payment_intent") if isinstance(s.get("payment_intent"), str) else None,
                        s.get("status") or "open", s.get("payment_status"),
                        (s.get("customer_details") or {}).get("email"), s.get("amount_total"),
                    )
            except Exception as exc:  # noqa: BLE001 - initiations are best effort; webhooks also feed them
                self.state.log_error("pricing", f"session poll failed: {exc!r}")
        views = max(0, self.views_now(exp["hypothesis_id"]) - exp["views_at_start"]) if exp.get("hypothesis_id") else 0
        orders = self.state.orders_for_refs(refs, since=exp["started_at"])
        initiations = max(self.state.initiations_for_refs(refs, since=exp["started_at"]), orders)
        recent_since = (self.state.clock() - timedelta(hours=2 * self.cfg.pricing_window_hours)).isoformat(timespec="seconds")
        self.state.update_experiment(exp["id"], views=views, initiations=initiations)
        return {"views": views, "initiations": initiations, "orders": orders,
                "recent_orders": self.state.orders_for_refs(refs, since=recent_since),
                "dropoff_pct": round(100 * (1 - orders / initiations)) if initiations else 0}

    def history(self, asset: dict[str, Any]) -> list[dict[str, Any]]:
        """Every experiment on this product line (same hypothesis and kind), across data versions."""
        kinds = {a["id"]: a["kind"] for a in self.state.list_assets(asset["hypothesis_id"])}
        rows = [e for e in self.state.experiments_for_hypothesis(asset["hypothesis_id"])
                if kinds.get(e["asset_id"]) == asset["kind"]]
        for e in rows:
            e["orders"] = self.state.orders_for_refs([e["product_ref"]] if e["product_ref"] else [], since=e["started_at"])
        return rows

    def ensure_experiment(self, asset: dict[str, Any]) -> dict[str, Any] | None:
        exp = self.state.running_experiment(asset["id"])
        if exp is None and asset.get("product_ref"):
            # A new data version of the same product (same Payment Link) continues its experiment:
            # restarting the 48h window on every data refresh would mean it never elapses.
            for prior in reversed(self.state.experiments_for_hypothesis(asset["hypothesis_id"])):
                if prior["status"] in ("running", "converged") and prior["product_ref"] == asset["product_ref"]:
                    self.state.update_experiment(prior["id"], asset_id=asset["id"])
                    return self.state.running_experiment(asset["id"])
        if exp is None and asset.get("checkout_url") and asset.get("product_ref"):
            self.state.start_experiment(
                asset["id"], asset["hypothesis_id"], asset["price_cents"], asset.get("provider") or "",
                asset["product_ref"], asset["checkout_url"], self.views_now(asset["hypothesis_id"]),
            )
            exp = self.state.running_experiment(asset["id"])
        return exp

    def reprice(self, asset: dict[str, Any], exp: dict[str, Any], price: int, reason: str) -> dict[str, Any]:
        sf = self.storefront()
        n = len(self.state.experiments_for_hypothesis(asset["hypothesis_id"])) + 1
        meta = {"niche": asset.get("niche") or "", "hypothesis_id": str(asset["hypothesis_id"]),
                "asset_id": str(asset["id"]), "experiment": str(n)}
        ref, url, _ = sf.create_link(asset["title"], f"{asset['title']} (${price / 100:.2f})", price, meta)
        self.state.update_experiment(exp["id"], status="ended", reason=reason, ended_at=self.state.now())
        self.state.start_experiment(asset["id"], asset["hypothesis_id"], price, sf.name, ref, url, self.views_now(asset["hypothesis_id"]))
        self.state.update_asset(asset["id"], price_cents=price, checkout_url=url, product_ref=ref)
        if exp.get("product_ref") and exp["product_ref"] != ref:
            sf.deactivate(exp["product_ref"])
        self._redeploy(asset["id"])
        return {"price_cents": price, "checkout_url": url}

    def _redeploy(self, asset_id: int) -> None:
        """Push the new checkout onto the lander immediately (build_site refreshes the rest)."""
        from strategies.distribution_engine import _load_listing
        from tools.storefront.stripe_pages_publisher import PagesDeployer

        asset = self.state.get_asset(asset_id)
        if not asset or asset["kind"] != ASSET_KIND:
            return
        hyp = self.state.get_hypothesis(asset["hypothesis_id"])
        ctx = TaskContext(self.tools, hyp, {})
        try:
            listing = _load_listing(ctx, asset)
        except FileNotFoundError:
            return
        listing.price_cents = asset["price_cents"]
        PagesDeployer(self.cfg, self.tools.files, self.tools.github).deploy(listing, asset["checkout_url"])

    # -- bundles & premium ----------------------------------------------------------
    def bundle_partner(self, asset: dict[str, Any]) -> dict[str, Any] | None:
        from agent.hypotheses import ADJACENT

        niche = asset.get("niche") or ""
        candidates = [a for a in self.state.list_assets() if a["kind"] == ASSET_KIND and a.get("niche") and a["niche"] != niche]
        latest: dict[str, dict[str, Any]] = {}
        for a in candidates:  # newest first
            latest.setdefault(a["niche"], a)
        if not latest:
            return None
        adjacent = [n for n in ADJACENT.get(niche, []) if n in latest]
        return latest[adjacent[0]] if adjacent else next(iter(latest.values()))

    def create_bundle(self, asset: dict[str, Any], partner: dict[str, Any], price: int) -> dict[str, Any]:
        files = self.tools.files
        niche = asset["niche"]
        existing = [a for a in self.state.list_assets(asset["hypothesis_id"]) if a["kind"] == "bundle"]
        version = (existing[0]["version"] + 1) if existing else 1
        base = f"assets/{niche}/bundle-v{version}"
        title = f"{niche_title(niche)} + {niche_title(partner['niche'])} Tech Stack Intel (2-for-1 Bundle)"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
            for part in (asset, partner):
                with zipfile.ZipFile(io.BytesIO(files.read_bytes(part["path"]))) as src:
                    for name in src.namelist():
                        out.writestr(f"bundle/{name}", src.read(name))
        zip_rel = f"assets/{niche}/{niche}-bundle-v{version}.zip"
        files.write_bytes(zip_rel, buf.getvalue())
        samples = []
        for part in (asset, partner):
            p = f"assets/{part['niche']}/v{part['version']}/sample.json"
            samples.append(files.read_json(p) if files.exists(p) else {"fields": [], "rows": []})
        fields = samples[0]["fields"] or samples[1]["fields"]
        rows = (samples[0]["rows"][:3] + samples[1]["rows"][:2])
        summary = (f"Two datasets for the price of one: {niche_title(niche)} and {niche_title(partner['niche'])} "
                   f"company-level tech stack intelligence, CSV + JSON + executive radars.")
        files.write_json(f"{base}/listing.json", {"name": title, "summary": summary, "price_cents": price, "file": zip_rel})
        files.write_json(f"{base}/sample.json", {"fields": fields, "rows": rows})
        bundle_id = self.state.add_asset(asset["hypothesis_id"], "bundle", title, zip_rel, version,
                                         asset["lead_count"] + partner["lead_count"], price)
        self.state.update_asset(bundle_id, niche=niche, kind_meta=json.dumps({"parts": [asset["id"], partner["id"]]}))
        return self._list(bundle_id, title, summary, price)

    def create_premium(self, hyp: dict[str, Any], asset: dict[str, Any]) -> dict[str, Any] | None:
        files = self.tools.files
        niche = asset["niche"]
        intel_path = f"exports/intel/{niche}/tech_radar.json"
        if not files.exists(intel_path):
            return None
        records = files.read_json(intel_path)
        existing = [a for a in self.state.list_assets(hyp["id"]) if a["kind"] == "premium"]
        version = (existing[0]["version"] + 1) if existing else 1
        base = f"assets/{niche}/premium-v{version}"
        title = f"{niche_title(niche)} Deep Dive: Company Profiles (Premium)"
        md = [f"# {title}", "", f"{len(records)} companies, ranked by hiring urgency. Generated {self.state.now()}.", ""]
        for r in records[:40]:
            md += [f"## {r['company']}  (urgency {r['urgency_score']})", ""]
            if r.get("domain"):
                md.append(f"- **Domain:** {r['domain']}")
            md.append(f"- **Open roles ({r['openings']}):** {', '.join(r['open_positions'])}")
            for cat, techs in (r.get("tech_stack") or {}).items():
                md.append(f"- **{cat.replace('_', ' ').title()}:** {', '.join(techs)}")
            md.append(f"- **Intent signals:** {', '.join(s.replace('_', ' ') for s in r['intent_signals']) or 'none detected'}")
            md.append(f"- **Careers page:** {r.get('careers_url') or 'n/a'}{' (verified)' if r.get('careers_url_verified') else ''}")
            md.append(f"- **Latest posting:** {r.get('latest_posted_at') or 'n/a'} · sources: {', '.join(r.get('sources', []))}")
            md.append("")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
            out.writestr(f"{niche}-deep-dive/DEEP_DIVE.md", "\n".join(md) + "\n")
            out.writestr(f"{niche}-deep-dive/tech_radar.json", json.dumps(records, indent=2))
        zip_rel = f"assets/{niche}/{niche}-premium-v{version}.zip"
        files.write_bytes(zip_rel, buf.getvalue())
        price = max([self.cfg.premium_price_cents, *self.cfg.price_matrix])
        summary = f"Per-company profiles for {min(40, len(records))} {niche_title(niche)} companies: full stack by category, every open role, intent signals and verified careers pages."
        files.write_json(f"{base}/listing.json", {"name": title, "summary": summary, "price_cents": price, "file": zip_rel})
        top = [{"company": r["company"], "urgency_score": r["urgency_score"], "openings": r["openings"],
                "stack": r["stack"][:4]} for r in records[:5]]
        files.write_json(f"{base}/sample.json", {"fields": ["company", "urgency_score", "openings", "stack"], "rows": top})
        premium_id = self.state.add_asset(hyp["id"], "premium", title, zip_rel, version, asset["lead_count"], price)
        self.state.update_asset(premium_id, niche=niche)
        return self._list(premium_id, title, summary, price)

    def _list(self, asset_id: int, title: str, summary: str, price: int) -> dict[str, Any]:
        sf = self.storefront()
        asset = self.state.get_asset(asset_id)
        ref, url, _ = sf.create_link(title, summary, price, {"niche": asset["niche"], "hypothesis_id": str(asset["hypothesis_id"]),
                                                             "asset_id": str(asset_id), "experiment": "1"})
        self.state.update_asset(asset_id, provider=sf.name, checkout_url=url, product_ref=ref, status="published")
        self.state.start_experiment(asset_id, asset["hypothesis_id"], price, sf.name, ref, url, self.views_now(asset["hypothesis_id"]))
        return {"asset_id": asset_id, "checkout_url": url, "price_cents": price}

    # -- demand ----------------------------------------------------------------------
    def expand_on_demand(self, hyp: dict[str, Any], asset: dict[str, Any] | None) -> list[str]:
        now = self.state.clock()
        sales = self.state.orders_since(hyp["id"], now - timedelta(hours=24))
        if sales < self.cfg.demand_sales_threshold:
            return []
        actions = []
        params = dict(hyp["params"])
        depth = int(params.get("depth", 1))
        last = self.state.get(f"expanded:{hyp['id']}")
        if depth < self.cfg.max_scrape_depth and (not last or now - datetime.fromisoformat(last) >= timedelta(hours=24)):
            own = [t for t, _ in self.state.tag_frequencies(limit=15) if t not in params["keywords"]]
            params["depth"] = depth + 1
            params["keywords"] = list(dict.fromkeys(params["keywords"] + own[:2]))
            self.state.update_hypothesis_params(hyp["id"], params)
            self.state.set(f"expanded:{hyp['id']}", now.isoformat(timespec="seconds"))
            actions.append(f"scrape depth {depth}→{depth + 1}, keywords +{own[:2]}")
        premium = [a for a in self.state.list_assets(hyp["id"]) if a["kind"] == "premium"]
        fresh = premium and now - datetime.fromisoformat(premium[0]["created_at"]) < timedelta(days=7)
        if asset and not fresh and self.storefront():
            made = self.create_premium(hyp, asset)
            if made:
                actions.append(f"premium add-on at ${made['price_cents'] / 100:.2f}: {made['checkout_url']}")
        return actions

    # -- one pass ----------------------------------------------------------------------
    def run(self, hyp: dict[str, Any]) -> dict[str, Any]:
        report: dict[str, Any] = {"decisions": [], "demand": []}
        asset = self.state.latest_asset(hyp["id"], ASSET_KIND)
        report["demand"] = self.expand_on_demand(hyp, asset)
        if self.storefront() is None:
            report["decisions"].append("pricing needs a Stripe secret key or Lemon Squeezy (Gumroad prices are manual)")
            return report
        live = [a for a in self.state.list_assets(hyp["id"]) if a.get("checkout_url") and a["kind"] in (ASSET_KIND, "bundle")]
        seen_kinds: set[str] = set()
        for a in live:  # newest first: only the current version of each kind
            if a["kind"] in seen_kinds:
                continue
            seen_kinds.add(a["kind"])
            exp = self.ensure_experiment(a)
            if exp is None:
                continue
            stats = self.refresh(exp)
            partner = self.bundle_partner(a) if a["kind"] == ASSET_KIND else None
            open_bundle = any(b["kind"] == "bundle" and b.get("checkout_url") for b in live)
            d = decide(self.state.running_experiment(a["id"]), stats, self.history(a), self.state.clock(),
                       self.cfg.price_matrix, self.cfg.pricing_min_views, self.cfg.pricing_window_hours,
                       can_bundle=bool(partner) and not open_bundle, view_tracking=bool(self.state.get("view_tracking")))
            line = f"{a['title']}: {d.action} ({d.reason}) · views {stats['views']}, initiations {stats['initiations']}, orders {stats['orders']}, drop-off {stats['dropoff_pct']}%"
            if d.action in ("lower", "raise"):
                self.reprice(a, exp, d.price_cents, d.reason)  # type: ignore[arg-type]
                line += f" → ${d.price_cents / 100:.2f}"  # type: ignore[operator]
            elif d.action == "bundle" and partner:
                made = self.create_bundle(a, partner, max(a["price_cents"], partner["price_cents"]))
                self.state.update_experiment(exp["id"], status="ended", reason="bundled", ended_at=self.state.now())
                line += f" → bundle {made['checkout_url']}"
            elif d.action == "converge":
                self.state.update_experiment(exp["id"], status="converged", reason=d.reason)
            report["decisions"].append(line)
        return report


class PricingStrategy(Strategy):
    name = "pricing_engine"
    tasks = ("optimize_pricing",)

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        report = PricingEngine(ctx.tools).run(ctx.hypothesis)
        lines = report["demand"] + report["decisions"]
        return TaskResult(True, " | ".join(lines) or "no live datasets to price", report)
