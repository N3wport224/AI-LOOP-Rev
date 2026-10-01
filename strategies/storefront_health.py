"""Storefront health: every few hours, check that each product can actually be bought and delivered.

``check_storefront`` runs every ``storefront_check_hours`` (6) and, for each product on sale:

* **Checkout**: the Stripe Payment Link is still active (read-only ``GET /v1/payment_links/<id>``).
  A link switched off in the dashboard, or a deleted product, otherwise fails silently.
* **Product page**: the public page (``lander_url``) answers. A broken GitHub Pages site loses
  every visitor from the share kit and articles.
* **Download**: the file a buyer receives exists and is a readable zip with something in it.

A newly broken product raises one alert email (through the owner reports); when it recovers you
get a short note. Nothing is changed automatically. The latest results are kept in kv
``storefront_health`` for ``automonetize doctor`` and the daily report.
"""

from __future__ import annotations

import zipfile
from datetime import datetime, timedelta
from typing import Any

from strategies.base import Strategy, TaskContext, TaskResult

KEY = "storefront_health"
STRIPE_API = "https://api.stripe.com/v1"
ZIP_KINDS = {"lead_directory", "bundle", "team_license"}


def check_link(tools: Any, ref: str) -> str | None:
    cfg = tools.config
    if not (ref or "").startswith("plink_") or not cfg.stripe_secret_key:
        return None
    try:
        link = tools.http.get(f"{STRIPE_API}/payment_links/{ref}", headers={"Authorization": f"Bearer {cfg.stripe_secret_key}"},
                              check_robots=False, attempts=2).json()
    except Exception as exc:  # noqa: BLE001 - reported as a problem, not raised
        return f"checkout link can't be read from Stripe ({type(exc).__name__})"
    return None if link.get("active") else "checkout link is switched off in Stripe"


def check_page(tools: Any, url: str) -> str | None:
    if not url or not url.startswith("https://"):
        return None
    try:
        tools.http.get(url, check_robots=False, attempts=2)
    except Exception as exc:  # noqa: BLE001
        return f"product page doesn't load ({getattr(exc, 'status', type(exc).__name__)})"
    return None


def check_file(tools: Any, asset: dict[str, Any]) -> str | None:
    if asset.get("kind") not in ZIP_KINDS:
        return None
    rel = str(asset.get("path") or "")
    try:
        if not rel or not tools.files.exists(rel):
            return "download file is missing"
        with zipfile.ZipFile(tools.files.resolve(rel)) as zf:
            if not zf.namelist() or zf.testzip() is not None:
                return "download file is empty or damaged"
    except Exception:  # noqa: BLE001 - a bad zip, unreadable file or a path outside the data folder
        return "download file is damaged or unreadable"
    return None


def inspect(tools: Any) -> list[dict[str, Any]]:
    from tools.catalog import live_products

    out = []
    for p in live_products(tools.state):
        asset = tools.state.get_asset(p["id"]) or {}
        problems = [msg for msg in (check_link(tools, str(asset.get("product_ref") or "")), check_page(tools, p.get("lander_url", "")),
                                    check_file(tools, asset)) if msg]
        out.append({"id": p["id"], "title": p["title"], "ok": not problems, "problems": problems})
    return out


def describe(health: dict[str, Any] | None) -> str:
    if not health:
        return "not checked yet"
    bad = [p for p in health.get("products", []) if not p["ok"]]
    if not bad:
        return f"all {len(health.get('products', []))} product(s) buyable (checked {str(health.get('checked_at', ''))[:16]})"
    return "; ".join(f"{p['title']}: {', '.join(p['problems'])}" for p in bad)


class StorefrontHealth(Strategy):
    name = "storefront_health"
    tasks = ("check_storefront",)

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        tools, cfg, state = ctx.tools, ctx.tools.config, ctx.tools.state
        previous = state.get(KEY) or {}
        last = previous.get("checked_at")
        if last and state.clock() - datetime.fromisoformat(last) < timedelta(hours=float(cfg.storefront_check_hours)):
            return TaskResult(True, f"storefront checked {last[:16]}", {})
        results = inspect(tools)
        was_bad = {p["id"]: set(p["problems"]) for p in previous.get("products", []) if not p["ok"]}
        it = int(state.get("iteration", 0))
        for p in results:
            new = set(p["problems"]) - was_bad.get(p["id"], set())
            if new:
                state.log_error("storefront_health", f"\"{p['title']}\" can't be bought or delivered properly: {', '.join(sorted(new))}. "
                                                     "Check it in Stripe / on your site; `automonetize doctor` shows the status.", kind="alert")
            elif p["ok"] and p["id"] in was_bad:
                state.log_action(it, None, "storefront_health", "ok", f"{p['title']} works again")
        state.set(KEY, {"checked_at": state.now(), "products": results})
        bad = sum(1 for p in results if not p["ok"])
        return TaskResult(True, f"storefront: {len(results) - bad} ok, {bad} with problems", {"ok": len(results) - bad, "bad": bad})
