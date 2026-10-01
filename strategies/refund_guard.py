"""A product people return stops being promoted (Phases 370-374).

Refunds and disputes are recorded by ``sync_refunds`` (refunds.py). This looks at them per product:

* **Phase 370, per product:** orders, refunds and disputes in the last ``DAYS`` (90) days for each
  product (``stats``), all versions of a product counted together.
* **Phase 371, the hold:** a product with at least ``MIN_RETURNS`` (3) refunds or disputes making up
  ``HOLD_RATE`` (20%) or more of its orders is held: the agent stops promoting it (share kit,
  release and offer emails, marketing plays, best sellers) and you get one alert. It stays on sale
  and nothing is refunded or taken down automatically: those stay your decisions.
* **Phase 372, everywhere it's promoted:** held products are left out of ``promotable`` (freshness
  guard) and the marketing engine's featured products.
* **Phase 373, released again:** when the rate falls below ``RELEASE_RATE`` (10%) the hold is lifted
  by itself and noted; ``automonetize product SLUG release`` lifts it at once.
* **Phase 374, visible:** doctor lists held products; Monday's report has one line.

Runs inside ``ops_checks`` every cycle (one query).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

KEY = "promotion_holds"
DAYS = 90
MIN_RETURNS = 3
HOLD_RATE = 0.20
RELEASE_RATE = 0.10


# ------------------------------------------------------------------ Phase 370
def stats(state: Any, days: int = DAYS) -> dict[str, dict[str, Any]]:
    since = (state.clock() - timedelta(days=days)).isoformat(timespec="seconds")
    rows = state._all("SELECT a.niche AS niche, COUNT(*) AS n, SUM(CASE WHEN o.status IN ('refunded', 'disputed') THEN 1 ELSE 0 "
                      "END) AS bad FROM orders o JOIN assets a ON a.id = o.asset_id WHERE o.occurred_at >= ? "
                      "AND a.niche IS NOT NULL GROUP BY a.niche", (since,))
    return {r["niche"]: {"orders": int(r["n"]), "returns": int(r["bad"] or 0),
                         "rate": round(int(r["bad"] or 0) / int(r["n"]), 3) if int(r["n"]) else 0.0} for r in rows}


def holds(state: Any) -> dict[str, dict[str, Any]]:
    return dict(state.get(KEY) or {})


def held(state: Any) -> set[str]:
    return set(holds(state))


def _title(state: Any, niche: str) -> str:
    row = state._one("SELECT title FROM assets WHERE niche = ? ORDER BY id DESC LIMIT 1", (niche,))
    return str((row or {}).get("title") or niche)


# ------------------------------------------------------------------ Phases 371, 373
def evaluate(state: Any) -> dict[str, list[str]]:
    current = holds(state)
    added, released = [], []
    for niche, s in stats(state).items():
        if niche not in current and s["returns"] >= MIN_RETURNS and s["rate"] >= HOLD_RATE:
            current[niche] = {"since": state.now(), "rate": s["rate"], "returns": s["returns"]}
            added.append(niche)
            state.log_error("refund_guard", f"\"{_title(state, niche)}\": {s['returns']} of {s['orders']} orders refunded or "
                                            f"disputed in {DAYS} days ({s['rate']:.0%}). The agent stopped promoting it; it's "
                                            "still on sale. Check the description and the file, then fix or retire it "
                                            f"(automonetize product {niche} release|retire).", kind="alert")
        elif niche in current and s["rate"] < RELEASE_RATE:
            current.pop(niche)
            released.append(niche)
            state.log_action(int(state.get("iteration", 0)), None, "refund_guard", "ok",
                             f"\"{_title(state, niche)}\": returns down to {s['rate']:.0%}, promoted again")
    if added or released:
        state.set(KEY, current)
    return {"held": added, "released": released}


def release(state: Any, niche: str) -> bool:
    current = holds(state)
    if niche not in current:
        return False
    current.pop(niche)
    state.set(KEY, current)
    return True


# ------------------------------------------------------------------ Phase 374
def describe(state: Any) -> list[str]:
    return [f"{_title(state, n)}: {h['returns']} returns ({h['rate']:.0%}) since {str(h['since'])[:10]}"
            for n, h in holds(state).items()]


def weekly_line(state: Any) -> str:
    items = describe(state)
    return ("Not promoted because buyers returned them: " + "; ".join(items) + ".") if items else ""
