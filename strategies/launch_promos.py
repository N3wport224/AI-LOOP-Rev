"""Launch codes: a short, real discount when a new niche goes on sale.

``create_launch_promos`` gives each newly released niche (see ``release_announcer.new_releases``)
one Stripe promotion code, ``launch_discount_pct`` (20%) off that product only, valid for
``launch_promo_days`` (7) from the release. The code appears in the new-release emails and the
share kit with a link that applies it at checkout. Stripe enforces the expiry, so the deadline in
the copy is true.

Live mode only (in dry run nothing is created in Stripe). Off with ``launch_promos = false``.
State: kv ``launch_codes`` = {niche: code record}.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from strategies.base import Strategy, TaskContext, TaskResult

KEY = "launch_codes"


def active_code(state: Any, niche: str) -> dict[str, Any] | None:
    rec = (state.get(KEY) or {}).get(niche)
    if not rec or not rec.get("code"):
        return None
    return rec if int(rec["expires_at"]) > int(state.clock().timestamp()) else None


def offer_line(promo: dict[str, Any]) -> str:
    until = datetime.fromtimestamp(int(promo["expires_at"]), tz=timezone.utc).strftime("%b %d")
    return f"Launch offer: {promo['percent_off']}% off with code {promo['code']} until {until} (the link applies it)."


class LaunchPromos(Strategy):
    name = "launch_promos"
    tasks = ("create_launch_promos",)

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        from strategies.freshness_guard import niche_of
        from strategies.release_announcer import new_releases
        from tools.promo import Promos, code_text

        tools, cfg, state = ctx.tools, ctx.tools.config, ctx.tools.state
        if not cfg.launch_promos:
            return TaskResult(True, "launch codes off", {"created": 0})
        fresh = new_releases(state)
        codes: dict[str, Any] = dict(state.get(KEY) or {})
        todo = {n: p for n, p in fresh.items() if n not in codes}
        if not todo:
            return TaskResult(True, "no new releases needing a launch code", {"created": 0})
        if not tools.dispatcher.live or not cfg.stripe_secret_key:
            return TaskResult(True, "launch codes are created in live mode only", {"created": 0})
        promos = Promos(tools.http, cfg.stripe_secret_key)
        created = 0
        for niche, product in sorted(todo.items()):
            asset = state.get_asset(product["id"]) or {}
            ref = str(asset.get("product_ref") or "")
            if not ref.startswith("plink_") or niche_of(state, asset) != niche:
                continue
            expires = datetime.fromisoformat(product["released_at"]) + timedelta(days=int(cfg.launch_promo_days))
            if expires <= state.clock():
                codes[niche] = {"code": "", "skipped": "release too old"}
                continue
            try:
                rec = promos.create(ref, code_text("LAUNCH", niche), int(cfg.launch_discount_pct), int(expires.timestamp()),
                                    name=f"Launch {niche}")
            except Exception as exc:  # noqa: BLE001 - retried next cycle
                state.log_error("launch_promos", f"couldn't create a launch code for {niche}: {exc!r}")
                continue
            codes[niche] = rec
            created += 1
            state.log_action(int(state.get("iteration", 0)), None, "launch_promo", "ok",
                             f"{niche}: {rec['code']} ({rec['percent_off']}% off until {expires:%Y-%m-%d})")
        state.set(KEY, codes)
        return TaskResult(True, f"launch codes: {created} created", {"created": created})
