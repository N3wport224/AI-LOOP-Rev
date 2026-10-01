"""Phase 400: one number for how the agent is doing.

``automonetize doctor`` runs dozens of checks. The health score sums them up: each check that is
fine counts fully, a warning counts half, a failure counts nothing, scaled to 0-100. Failures also
cap the score at ``FAIL_CAP`` (69), so a broken essential can't hide behind many green checks.

Shown at the end of ``automonetize doctor``, in the control panel's Health tab (``score`` in
``GET /api/health``) and in Monday's report (the last score, kv ``health_score``).
"""

from __future__ import annotations

from typing import Any, Iterable

KEY = "health_score"
FAIL_CAP = 69
WEIGHT = {"ok": 1.0, "warn": 0.5, "fail": 0.0}


def score(statuses: Iterable[str]) -> int:
    items = list(statuses)
    if not items:
        return 100
    value = round(100 * sum(WEIGHT.get(s, 0.5) for s in items) / len(items))
    return min(value, FAIL_CAP) if "fail" in items else value


def label(value: int) -> str:
    if value >= 90:
        return "healthy"
    if value >= 70:
        return "fine, a few things to look at"
    if value >= 50:
        return "needs attention"
    return "needs you now"


def record(state: Any, findings: Iterable[Any]) -> dict[str, Any]:
    items = list(findings)
    value = score(getattr(f, "status", None) or f["status"] for f in items)
    out = {"score": value, "label": label(value), "at": state.now(),
           "fails": sum(1 for f in items if (getattr(f, "status", None) or f["status"]) == "fail"),
           "warns": sum(1 for f in items if (getattr(f, "status", None) or f["status"]) == "warn")}
    try:
        state.set(KEY, out)
    except Exception:  # noqa: BLE001 - a read-only moment must not break doctor
        pass
    return out


def weekly_line(state: Any) -> str:
    s = state.get(KEY)
    if not s:
        return ""
    return f"Health score: {s['score']}/100 ({s['label']}), from the last check on {str(s['at'])[:10]}."
