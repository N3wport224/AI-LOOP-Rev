"""Release gate: a new dataset version only replaces the one on sale if it looks healthy.

Buyers always get the newest version, so a bad build (a job board changed its format and half the
rows vanished, or titles came back empty) would go straight to paying customers. Before the
packager builds a new version, ``gate()`` compares it with the version on sale:

* **Rows dropped** by more than ``release_gate_max_drop`` (50%) → held. If the drop is still there
  after ``release_gate_hold_days`` (3) it's treated as real (the market shrank) and released.
* **Rows missing a company or job title** above 20% → held until fixed (a parser problem;
  self-evolution or an update fixes it). Never released automatically.

While a version is held, the previous one stays on sale unchanged. You get one alert when a hold
starts; releases are logged. State: kv ``release_gate:<niche>``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

BLANK_LIMIT = 0.2


def problems(previous_count: int, leads: list[dict[str, Any]], max_drop: float) -> tuple[list[str], bool]:
    """(reasons to hold, whether time may release the hold)."""
    reasons, releasable = [], True
    if previous_count and len(leads) < previous_count * (1 - max_drop):
        reasons.append(f"rows dropped from {previous_count} to {len(leads)}")
    blank = sum(1 for r in leads if not (str(r.get("company") or "").strip() and str(r.get("title") or "").strip()))
    if leads and blank / len(leads) > BLANK_LIMIT:
        reasons.append(f"{blank * 100 // len(leads)}% of rows have no company or job title")
        releasable = False
    return reasons, releasable


def gate(state: Any, cfg: Any, niche: str, latest: dict[str, Any] | None, leads: list[dict[str, Any]]) -> str | None:
    """None to build and release the new version; otherwise why it's held."""
    key = f"release_gate:{niche}"
    held = state.get(key)
    it = int(state.get("iteration", 0))
    reasons, releasable = problems(int((latest or {}).get("lead_count") or 0), leads, float(cfg.release_gate_max_drop))
    if not latest or not latest.get("checkout_url"):
        reasons = [r for r in reasons if "dropped" not in r]  # nothing on sale yet to protect
    if not reasons:
        if held:
            state.set(key, None)
            state.log_action(it, None, "release_gate", "ok", f"{niche}: new version looks healthy again, released")
        return None
    since = datetime.fromisoformat(held["since"]) if held else state.clock()
    if releasable and state.clock() - since >= timedelta(days=float(cfg.release_gate_hold_days)):
        state.set(key, None)
        state.log_action(it, None, "release_gate", "ok", f"{niche}: released after {cfg.release_gate_hold_days} days "
                                                          f"({'; '.join(reasons)}): the change looks real")
        return None
    reason = "; ".join(reasons)
    if not held:
        state.log_error("release_gate", f"A new version of the {niche} dataset looks wrong ({reason}). Buyers keep getting "
                                        "the previous version until it looks right.", kind="alert")
    state.set(key, {"since": since.isoformat(timespec="seconds"), "count": len(leads), "reason": reason})
    return reason


def held(state: Any) -> dict[str, dict[str, Any]]:
    out = {}
    for row in state._all("SELECT key FROM kv WHERE key LIKE 'release_gate:%'"):
        value = state.get(row["key"])
        if value:
            out[row["key"].split(":", 1)[1]] = value
    return out
