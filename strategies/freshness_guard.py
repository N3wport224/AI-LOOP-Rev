"""Stale-data guard: never promote a dataset whose source has stopped updating.

The datasets are sold as "companies hiring right now", so data that quietly stopped refreshing (a
job board changed its format, a feed went offline) is a refund waiting to happen.
``guard_freshness`` checks, for each dataset on sale, when its niche last saw a posting:

* No new or re-seen posting for ``stale_after_days`` (7) → the product is marked stale and you get
  one alert. Stale products are left out of the share kit and the new-release emails, so the
  agent doesn't advertise them. They stay on sale (people who find them still get what the page
  describes, with its "data updated" date); taking them down stays your call.
* When postings flow again, the mark is removed and a note is logged.

``stale_products`` (kv) holds ``{asset_id: since}``; ``is_stale(state, asset_id)`` is the check.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from strategies.base import Strategy, TaskContext, TaskResult

KEY = "stale_products"
CHECKED_KINDS = {"lead_directory"}


def niche_of(state: Any, asset: dict[str, Any]) -> str:
    if asset.get("niche"):
        return str(asset["niche"])
    hyp = state.get_hypothesis(asset["hypothesis_id"]) if asset.get("hypothesis_id") else None
    return str(((hyp or {}).get("params") or {}).get("niche") or "")


def is_stale(state: Any, asset_id: int) -> bool:
    return str(asset_id) in (state.get(KEY) or {})


def promotable(state: Any) -> list[dict[str, Any]]:
    """Products on sale that the agent may advertise: everything except stale datasets."""
    from tools.catalog import live_products

    marks = state.get(KEY) or {}
    return [p for p in live_products(state) if str(p["id"]) not in marks]


def _age_days(state: Any, when: str | None) -> float | None:
    if not when:
        return None
    try:
        dt = datetime.fromisoformat(when)
    except ValueError:
        return None
    return (state.clock() - dt).total_seconds() / 86400


class FreshnessGuard(Strategy):
    name = "freshness_guard"
    tasks = ("guard_freshness",)

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        from tools.catalog import live_products

        cfg, state = ctx.tools.config, ctx.tools.state
        limit = float(cfg.stale_after_days)
        marks: dict[str, str] = dict(state.get(KEY) or {})
        current: dict[str, str] = {}
        it = int(state.get("iteration", 0))
        for p in live_products(state):
            if p["kind"] not in CHECKED_KINDS:
                continue
            asset = state.get_asset(p["id"]) or {}
            niche = niche_of(state, asset)
            last = state.niche_last_seen(niche) if niche else None
            age = _age_days(state, last)
            if age is None or age < limit:
                if str(p["id"]) in marks:
                    state.log_action(it, None, "freshness", "ok", f"{p['title']}: data is flowing again")
                continue
            current[str(p["id"])] = marks.get(str(p["id"])) or state.now()
            if str(p["id"]) not in marks:
                state.log_error("freshness_guard", f"\"{p['title']}\" hasn't had new job postings for {age:.0f} days "
                                                   f"(last {str(last)[:10]}). The agent stopped promoting it; its source may "
                                                   "have changed. `automonetize doctor` shows the status.", kind="alert")
        state.set(KEY, current)
        return TaskResult(True, f"freshness: {len(current)} stale product(s)", {"stale": len(current)})


def stale_titles(state: Any) -> list[str]:
    marks = state.get(KEY) or {}
    titles = []
    for aid in marks:
        asset = state.get_asset(int(aid))
        if asset:
            titles.append(asset["title"])
    return titles

