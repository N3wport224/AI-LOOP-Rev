"""Marketing optimisation (Phases 190-194): measure every product and strategy, keep what works.

* **Phase 190, product funnel:** per product over 30 days: checkouts started (Stripe sessions on
  its link), kept orders and conversion. A product with ``FUNNEL_MIN_CHECKOUTS`` (5) checkouts and
  no sale has a problem (price, description, preview): it's flagged in Monday's report and on
  the to-do list.
* **Phase 191, title tests:** for products with enough traffic (``TITLE_TEST_MIN_CHECKOUTS``
  checkouts in 30 days), the page title alternates weekly between the original and a
  benefit-led variant ("<Tech> jobs<where>: N companies hiring now"); after both have run a week,
  the one with more checkouts per week stays. The Stripe product name never changes. Products with
  too little traffic aren't tested: the result would be noise.
* **Phase 192, paused strategies are visible:** when the engine rests a strategy (8 plays, no
  checkout), the daily report says so; it comes back by itself after 30 days.
* **Phase 193, return on your time:** for draft strategies (you post them), revenue per minute you
  spent (posted plays x the strategy's minutes), so the report can say which drafts are worth it.
* **Phase 194, one report:** ``automonetize marketing report``: funnel, title tests, strategy
  scores, return on time and paused strategies in one place.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from strategies.base import Strategy, TaskContext, TaskResult

FUNNEL_MIN_CHECKOUTS = 5
TITLE_TEST_MIN_CHECKOUTS = 10
TESTS = "title_tests"
WEEK = timedelta(days=7)


# ------------------------------------------------------------------ Phase 190
def funnel(state: Any, days: int = 30) -> list[dict[str, Any]]:
    from strategies.revenue_models import live_products

    since = (state.clock() - timedelta(days=days)).isoformat(timespec="seconds")
    out = []
    for a in live_products(state):
        ref = a.get("product_ref")
        if not ref:
            continue
        refs = [r["product_ref"] for r in state._all("SELECT DISTINCT product_ref FROM assets WHERE niche = ? AND product_ref IS NOT NULL",
                                                     (a["niche"],))]
        marks = ",".join("?" * len(refs))
        checkouts = int(state._one(f"SELECT COUNT(*) AS n FROM checkout_sessions WHERE product_ref IN ({marks}) AND updated_at >= ?",  # noqa: S608
                                   (*refs, since))["n"])
        orders = int(state._one("SELECT COUNT(*) AS n FROM orders o JOIN assets x ON x.id = o.asset_id WHERE x.niche = ? "
                                "AND o.occurred_at >= ? AND o.status NOT IN ('refunded', 'disputed')", (a["niche"], since))["n"])
        out.append({"niche": a["niche"], "title": a["title"], "checkouts": checkouts, "orders": orders,
                    "conversion": round(orders / checkouts, 3) if checkouts else None,
                    "leaky": checkouts >= FUNNEL_MIN_CHECKOUTS and not orders})
    return sorted(out, key=lambda r: (-r["checkouts"], r["title"]))


def leaky(state: Any) -> list[dict[str, Any]]:
    return [r for r in funnel(state) if r["leaky"]]


# ------------------------------------------------------------------ Phase 191
def variant_title(state: Any, niche: str, title: str) -> str:
    row = state._one("SELECT filters, rows, companies FROM factory_products WHERE slug = ?", (niche,)) if _factory(state) else None
    if not row:
        return title
    from strategies.product_factory import label

    f = json.loads(row["filters"])
    if f.get("type", "slice") != "slice":
        return title
    where = {"us": " in the US", "europe": " in Europe", "remote": " (remote)"}.get(f.get("region") or "", "")
    level = f"{f['level'].title()} " if f.get("level") else ""
    return f"{level}{label(f['tech'])} Jobs{where}: {int(row['companies'])} Companies Hiring Now"


def _factory(state: Any) -> bool:
    return bool(state._one("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'factory_products'"))


def _checkouts(state: Any, niche: str, start: str, end: str) -> int:
    refs = [r["product_ref"] for r in state._all("SELECT DISTINCT product_ref FROM assets WHERE niche = ? AND product_ref IS NOT NULL",
                                                 (niche,))]
    if not refs:
        return 0
    marks = ",".join("?" * len(refs))
    return int(state._one(f"SELECT COUNT(*) AS n FROM checkout_sessions WHERE product_ref IN ({marks}) AND updated_at >= ? "  # noqa: S608
                          "AND updated_at < ?", (*refs, start, end))["n"])


def step_title_tests(state: Any) -> dict[str, str]:
    """Start, switch or finish weekly A/B title tests. Returns {niche: what happened}."""
    tests = dict(state.get(TESTS) or {})
    now = state.clock()
    changes = {}
    for row in funnel(state):
        niche = row["niche"]
        t = tests.get(niche)
        if t is None:
            if row["checkouts"] >= TITLE_TEST_MIN_CHECKOUTS and variant_title(state, niche, row["title"]) != row["title"]:
                tests[niche] = {"phase": "B", "started": now.isoformat(timespec="seconds"),
                                "a_checkouts": _checkouts(state, niche, (now - WEEK).isoformat(timespec="seconds"),
                                                          now.isoformat(timespec="seconds"))}
                changes[niche] = "testing the variant title"
            continue
        if t["phase"] == "B" and now - datetime.fromisoformat(t["started"]) >= WEEK:
            b = _checkouts(state, niche, t["started"], now.isoformat(timespec="seconds"))
            winner = "B" if b > int(t["a_checkouts"]) else "A"
            tests[niche] = {**t, "phase": "done", "b_checkouts": b, "winner": winner, "finished": now.isoformat(timespec="seconds")}
            changes[niche] = f"kept {'the variant' if winner == 'B' else 'the original'} title ({t['a_checkouts']} vs {b} checkouts)"
    state.set(TESTS, tests)
    return changes


def display_title(state: Any, niche: str, title: str) -> str:
    t = (state.get(TESTS) or {}).get(niche)
    if t and (t["phase"] == "B" or (t["phase"] == "done" and t.get("winner") == "B")):
        return variant_title(state, niche, title)
    return title


# ------------------------------------------------------------------ Phase 193
def return_on_time(state: Any) -> list[dict[str, Any]]:
    from strategies.marketing_engine import PLAYBOOK, scores

    out = []
    for key, row in scores(state).items():
        s = PLAYBOOK[key]
        if s["mode"] != "draft":
            continue
        posted = int(state._one("SELECT COUNT(*) AS n FROM marketing_plays WHERE strategy = ? AND status = 'posted'", (key,))["n"])
        minutes = posted * int(s.get("minutes", 5))
        out.append({"name": s["name"], "posted": posted, "minutes": minutes, "revenue_cents": row["revenue_cents"],
                    "per_minute_cents": round(row["revenue_cents"] / minutes) if minutes else None})
    return sorted(out, key=lambda r: -(r["per_minute_cents"] or 0))


def describe(state: Any) -> list[str]:
    from strategies.marketing_engine import PLAYBOOK, paused

    lines = []
    leaks = leaky(state)
    if leaks:
        lines.append("Checkouts but no sales (check price, description, preview): "
                     + "; ".join(f"{r['title']} ({r['checkouts']} checkouts)" for r in leaks[:3]))
    best = [r for r in return_on_time(state) if r["per_minute_cents"]]
    if best:
        lines.append(f"Best use of your time: {best[0]['name']} (${best[0]['per_minute_cents'] / 100:.2f} per minute spent).")
    rest = paused(state)
    if rest:
        lines.append("Resting (no checkouts after 8 plays): " + ", ".join(PLAYBOOK[k]["name"] for k in rest if k in PLAYBOOK))
    return lines


def report(state: Any) -> str:
    from strategies.marketing_engine import scores

    lines = ["Product funnel (30 days):"]
    lines += [f"  {r['checkouts']:>4} checkouts {r['orders']:>3} orders  {r['title']}" + ("  ← no sales" if r["leaky"] else "")
              for r in funnel(state)[:15]] or ["  no products yet"]
    tests = state.get(TESTS) or {}
    if tests:
        lines += ["", "Title tests:"] + [f"  {k}: {v['phase']}" + (f", kept {'variant' if v.get('winner') == 'B' else 'original'}"
                                                                   if v["phase"] == "done" else "") for k, v in tests.items()]
    lines += ["", "Strategies (60 days):"]
    lines += [f"  {r['name']:<34} {r['plays']:>3} plays {r['checkouts']:>3} checkouts ${r['revenue_cents'] / 100:>8,.2f}"
              for r in sorted(scores(state).values(), key=lambda r: -r["revenue_cents"])]
    roi = return_on_time(state)
    if roi:
        lines += ["", "Return on your time (drafts you posted):"]
        lines += [f"  {r['name']:<34} {r['minutes']:>4} min  " + (f"${r['per_minute_cents'] / 100:.2f}/min" if r["per_minute_cents"]
                                                                  else "no sales yet") for r in roi]
    lines += [""] + describe(state)
    return "\n".join(lines)


class MarketingOptimizer(Strategy):
    name = "marketing_optimizer"
    tasks = ("optimize_marketing",)

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        state = ctx.tools.state
        last = state.get("marketing_optimized_at")
        if last and state.clock() - datetime.fromisoformat(last) < timedelta(hours=20):
            return TaskResult(True, f"marketing optimised {last[:16]}", {})
        changes = step_title_tests(state)
        state.set("marketing_optimized_at", state.now())
        leaks = leaky(state)
        return TaskResult(True, f"optimiser: {len(changes)} title test change(s); {len(leaks)} product(s) with checkouts but no sales",
                          {"title_changes": len(changes), "leaky": len(leaks)})
