"""Task timings (Phase 113): which tasks take the time.

Every task's duration is already in the ``actions`` table. ``timings(state, days)`` summarises
them per task (runs, average, slowest, total) so a cycle that's getting slow has an obvious
culprit. Shown by ``automonetize timings`` and, for the three slowest, in Monday's report.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any


def timings(state: Any, days: int = 7) -> list[dict[str, Any]]:
    since = (state.clock() - timedelta(days=days)).isoformat(timespec="seconds")
    rows = state._all("SELECT name, COUNT(*) AS runs, AVG(duration) AS avg_s, MAX(duration) AS max_s, SUM(duration) AS total_s "
                      "FROM actions WHERE created_at >= ? AND duration IS NOT NULL AND status != 'skipped' AND name != 'cycle' "
                      "GROUP BY name ORDER BY total_s DESC", (since,))
    return [{"task": r["name"], "runs": int(r["runs"]), "avg_s": round(float(r["avg_s"] or 0), 2),
             "max_s": round(float(r["max_s"] or 0), 2), "total_s": round(float(r["total_s"] or 0), 1)} for r in rows]


def describe(rows: list[dict[str, Any]], limit: int = 3) -> str:
    if not rows:
        return ""
    top = ", ".join(f"{r['task']} {r['avg_s']:.1f}s avg (max {r['max_s']:.0f}s)" for r in rows[:limit])
    return f"Slowest tasks this week: {top}."


def table(rows: list[dict[str, Any]]) -> str:
    lines = [f"{'task':<28} {'runs':>6} {'avg s':>8} {'max s':>8} {'total min':>10}"]
    lines += [f"{r['task']:<28} {r['runs']:>6} {r['avg_s']:>8.2f} {r['max_s']:>8.1f} {r['total_s'] / 60:>10.1f}" for r in rows]
    return "\n".join(lines)
