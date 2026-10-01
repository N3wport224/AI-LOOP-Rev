"""Adaptive factory pace (Phases 355-359): faster when it pays, never slower than you asked.

``factory_interval_seconds`` (10 minutes) is a promise: at least one new product that often whenever
there's something worth making. With ``factory_adaptive`` on, the factory may go faster:

* **Phase 355, the queue:** each time the factory picks a product it notes how many more good
  candidates are waiting (kv ``factory_queue``).
* **Phase 356, speed up when it pays:** with at least ``QUEUE_FAST`` (20) candidates waiting, products
  selling (a factory sale in the last 7 days) and the catalog under ``ROOM`` (80%) of
  ``factory_max_live``, the interval halves, down to ``MIN_INTERVAL`` (5 minutes).
* **Phase 357, never slower:** the effective interval is never longer than ``factory_interval_seconds``.
  (The factory still waits when there's nothing new worth making, or when products pile up without a
  checkout: Phase 242.)
* **Phase 358, used everywhere:** the factory worker and the cycle's catch-up both use the effective
  interval.
* **Phase 359, visible:** doctor and the control panel show the current pace and why.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

QUEUE = "factory_queue"
QUEUE_FAST = 20
ROOM = 0.8
MIN_INTERVAL = 300


def note_queue(state: Any, waiting: int) -> None:
    state.set(QUEUE, {"waiting": int(waiting), "at": state.now()})


def _selling(state: Any) -> bool:
    since = (state.clock() - timedelta(days=7)).isoformat(timespec="seconds")
    row = state._one("SELECT COUNT(*) AS n FROM orders o JOIN assets a ON a.id = o.asset_id WHERE a.kind = 'micro' "
                     "AND o.occurred_at >= ? AND o.status NOT IN ('refunded', 'disputed')", (since,))
    return int(row["n"]) > 0


def effective(state: Any, cfg: Any) -> dict[str, Any]:
    """{"seconds", "why"}: never more than factory_interval_seconds."""
    from strategies.product_factory import catalog

    base = max(60, int(cfg.factory_interval_seconds))
    if not getattr(cfg, "factory_adaptive", True):
        return {"seconds": base, "why": "fixed pace (factory_adaptive is off)"}
    waiting = int((state.get(QUEUE) or {}).get("waiting") or 0)
    live = catalog(state).get("live", 0)
    room = live < ROOM * int(cfg.factory_max_live)
    if waiting >= QUEUE_FAST and room and _selling(state):
        fast = max(MIN_INTERVAL, base // 2)
        return {"seconds": min(base, fast), "why": f"{waiting} good products waiting and products are selling"}
    reasons = []
    if waiting < QUEUE_FAST:
        reasons.append(f"{waiting} products waiting")
    if not room:
        reasons.append("the catalog is near its cap")
    return {"seconds": base, "why": "your pace" + (f" ({'; '.join(reasons)})" if reasons else "")}


def describe(state: Any, cfg: Any) -> str:
    e = effective(state, cfg)
    minutes = e["seconds"] / 60
    return f"one product every {minutes:g} min: {e['why']}"
