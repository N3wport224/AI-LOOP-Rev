"""Sell more to people who already buy (Phases 160-164).

* **Phase 160, related datasets email** (``offer_related``): 5 to 12 days after a delivered order,
  one email listing up to 3 live products about the same technology the buyer doesn't own. It's a
  marketing email: the contact policy decides (one offer per person per ``announce_min_gap_days``,
  quiet mode, suppression), with unsubscribe and postal address. Once per order.
* **Phase 161, often bought together:** each product page lists up to 3 related products (same
  technology), the starter pack first when there is one.
* **Phase 162, sell more of what sells:** the factory boosts product ideas about technologies
  that sold in the last 30 days, so new products follow demand.
* **Phase 163, thank-you page offers:** after paying, buyers see the lifetime pass and the custom
  dataset offer next to the existing upgrades.
* **Phase 164, best sellers:** the home page shows the products with the most kept orders in the
  last 30 days.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import timedelta
from typing import Any

from strategies.base import Strategy, TaskContext, TaskResult

SENT_KEY = "related_offer_sent"
WINDOW = (5, 12)
PER_CYCLE = 30


def tech_of(state: Any, cfg: Any, asset: dict[str, Any]) -> str:
    """The technology a product is about: factory products carry it; datasets take their niche's
    first keyword that is a known technology."""
    from strategies.b2b_lead_aggregator import TECH_KEYWORDS

    row = state._one("SELECT filters FROM factory_products WHERE asset_id = ?", (asset["id"],)) if _has_factory(state) else None
    if row:
        return str(json.loads(row["filters"]).get("tech") or "")
    niche = str(asset.get("niche") or "")
    for n in cfg.niches:
        if n.get("name") == niche:
            return next((k for k in n.get("keywords", []) if k in TECH_KEYWORDS), "")
    return ""


def _has_factory(state: Any) -> bool:
    return bool(state._one("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'factory_products'"))


class Index:
    """Live products and their technology, computed once per run (a catalog of hundreds of products
    would otherwise cost a query per pair)."""

    def __init__(self, state: Any, cfg: Any):
        from strategies.revenue_models import live_products

        self.state, self.cfg = state, cfg
        self.live = [a for a in live_products(state) if a.get("checkout_url")]
        self.tech = {str(a.get("niche") or ""): tech_of(state, cfg, a) for a in self.live}

    def tech_for(self, asset: dict[str, Any]) -> str:
        niche = str(asset.get("niche") or "")
        return self.tech[niche] if niche in self.tech else tech_of(self.state, self.cfg, asset)

    def related(self, asset: dict[str, Any], exclude: set[str] | None = None, limit: int = 3) -> list[dict[str, Any]]:
        """Live products about the same technology (the pack first)."""
        tech = self.tech_for(asset)
        if not tech:
            return []
        exclude = (exclude or set()) | {str(asset.get("niche") or "")}
        out = [a for a in self.live if str(a.get("niche") or "") not in exclude and self.tech.get(str(a.get("niche") or "")) == tech]
        out.sort(key=lambda a: (not str(a.get("niche") or "").startswith("pack-"), a["title"]))
        return out[:limit]


def related(state: Any, cfg: Any, asset: dict[str, Any], exclude: set[str] | None = None, limit: int = 3) -> list[dict[str, Any]]:
    return Index(state, cfg).related(asset, exclude, limit)


def product_url(cfg: Any, asset: dict[str, Any]) -> str:
    base = str(cfg.pages_base_url or "").rstrip("/")
    return f"{base}/{asset['niche']}/" if base and asset.get("niche") else str(asset.get("checkout_url") or "")


# ------------------------------------------------------------------ Phase 160
class Upsells(Strategy):
    name = "upsells"
    tasks = ("offer_related",)

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        from tools import contact_policy as contact
        from tools.dispatcher import Email

        tools = ctx.tools
        state, cfg = tools.state, tools.config
        if not cfg.related_offers:
            return TaskResult(True, "related offers off", {"sent": 0})
        if not str(cfg.sender_postal_address or "").strip():
            return TaskResult(True, "related offers wait for your postal address", {"sent": 0})
        now = state.clock()
        newest = (now - timedelta(days=WINDOW[0])).isoformat(timespec="seconds")
        oldest = (now - timedelta(days=WINDOW[1])).isoformat(timespec="seconds")
        done = set(state.get(SENT_KEY) or [])
        sent = 0
        index = None
        for o in state._all("SELECT * FROM orders WHERE status = 'delivered' AND email IS NOT NULL AND asset_id IS NOT NULL "
                            "AND delivered_at <= ? AND delivered_at >= ? ORDER BY id", (newest, oldest)):
            if sent >= PER_CYCLE or o["id"] in done:
                continue
            done.add(o["id"])
            if contact.blocked(state, cfg, o["email"], "related"):
                continue
            asset = state.get_asset(int(o["asset_id"])) or {}
            owned = {str((state.get_asset(int(x["asset_id"])) or {}).get("niche") or "")
                     for x in state._all("SELECT asset_id FROM orders WHERE email = ? AND asset_id IS NOT NULL", (o["email"],))}
            index = index or Index(state, cfg)
            picks = index.related(asset, exclude=owned)
            if not picks:
                continue
            mailbox = cfg.unsubscribe_email or cfg.sender_email
            lines = ["Hi,", "", f"Since you got {asset.get('title', 'one of our datasets')}, these cover the same ground from "
                     "other angles:", ""]
            lines += [f"- {a['title']} (${int(a['price_cents']) / 100:.2f}): {product_url(cfg, a)}" for a in picks]
            lines += ["", "Thanks,", cfg.sender_name or "AutoMonetize", "", "--", "Don't want emails like this? Reply \"unsubscribe\".",
                      cfg.sender_postal_address]
            headers = {"List-Unsubscribe": f"<mailto:{mailbox}?subject=unsubscribe>"} if mailbox else {}
            try:
                tools.dispatcher.send_transactional(Email(to=o["email"], subject="More on the same topic", body="\n".join(lines),
                                                          kind="delivery", headers=headers), audit_key=f"related:{o['id']}")
            except Exception as exc:  # noqa: BLE001 - retried with the next order; this one is marked done
                state.log_error("upsells", f"related offer for order {o['id']} failed: {exc!r}")
                continue
            contact.record(state, o["email"], "related", str(o["id"]))
            sent += 1
        state.set(SENT_KEY, sorted(done)[-5000:])
        return TaskResult(True, f"related offers: {sent} sent", {"sent": sent})


# ------------------------------------------------------------------ Phase 161
def bought_together_html(index: Index, asset: dict[str, Any]) -> str:
    import html

    picks = index.related(asset)
    if not picks:
        return ""
    items = "".join(f'<li><a href="../{html.escape(str(a["niche"]))}/">{html.escape(a["title"])}</a>: '
                    f'${int(a["price_cents"]) / 100:.2f}</li>' for a in picks)
    return f"<h2>Often bought together</h2><ul>{items}</ul>"


# ------------------------------------------------------------------ Phase 162
def selling_techs(state: Any, cfg: Any, days: int = 30) -> Counter:
    since = (state.clock() - timedelta(days=days)).isoformat(timespec="seconds")
    out: Counter = Counter()
    for o in state._all("SELECT asset_id FROM orders WHERE asset_id IS NOT NULL AND occurred_at >= ? "
                        "AND status NOT IN ('refunded', 'disputed')", (since,)):
        asset = state.get_asset(int(o["asset_id"]))
        tech = tech_of(state, cfg, asset) if asset else ""
        if tech:
            out[tech] += 1
    return out


# ------------------------------------------------------------------ Phase 164
def best_sellers(state: Any, days: int = 30, limit: int = 5) -> list[dict[str, Any]]:
    since = (state.clock() - timedelta(days=days)).isoformat(timespec="seconds")
    rows = state._all("SELECT a.niche AS niche, COUNT(*) AS n FROM orders o JOIN assets a ON a.id = o.asset_id "
                      "WHERE o.occurred_at >= ? AND o.status NOT IN ('refunded', 'disputed') AND a.niche IS NOT NULL "
                      "GROUP BY a.niche ORDER BY n DESC, a.niche LIMIT ?", (since, limit))
    return [{"niche": r["niche"], "orders": int(r["n"])} for r in rows]


def best_sellers_html(state: Any, pages: list[Any]) -> str:
    import html

    by = {p.niche: p for p in pages}
    picks = [(by[b["niche"]], b["orders"]) for b in best_sellers(state) if b["niche"] in by]
    if not picks:
        return ""
    items = "".join(f'<li><a href="{html.escape(p.slug)}/">{html.escape(p.title)}</a> (${p.price_cents / 100:.2f})</li>'
                    for p, _ in picks)
    return f"<h2>Best sellers this month</h2><ol>{items}</ol>"
