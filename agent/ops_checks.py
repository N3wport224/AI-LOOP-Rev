"""Ops checks (Phases 105, 107, 108): things that quietly break an unattended Mac.

``ops_checks`` runs every cycle (cheap) and records what it found in kv ``ops``:

* **Job-source health** (Phase 105): a job board that returned nothing (or failed) for the last
  ``SOURCE_ZERO_RUNS`` runs in a row is reported, once a day per source, with its last error. A
  board that changes its API usually shows up here first, long before the datasets go stale.
* **Clock check** (Phase 107): once a day, the Mac's clock is compared with the ``Date`` header of
  an HTTPS response from GitHub. More than ``CLOCK_SKEW_SECONDS`` off and you get an alert: Stripe
  rejects webhook signatures more than 5 minutes old, and download links and reports use the clock.
* **Battery saver** (Phase 108): on a MacBook running on battery (``pmset -g batt``), the heavy tasks
  in ``HEAVY_TASKS`` (dataset builds, site builds, source discovery, code evolution) wait until it's
  plugged in again; sales, deliveries and email keep running. ``battery_saver = false`` turns it off.

All three appear in ``automonetize doctor`` and the Health tab. Phases 215-218 (factory health, database
growth, slow tasks) run here too: see ``agent/scale_checks.py``.
"""

from __future__ import annotations

import platform
import re
import subprocess
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable

from strategies.base import Strategy, TaskContext, TaskResult

KEY = "ops"
SOURCE_ZERO_RUNS = 3
CLOCK_SKEW_SECONDS = 120
CLOCK_URL = "https://api.github.com/"
HEAVY_TASKS = frozenset({"build_intel", "package_asset", "discover_sources", "build_site", "run_satellites", "evolve_code"})


# ------------------------------------------------------------------ Phase 105: job-source health
def source_health(state: Any, sources: list[str]) -> dict[str, dict[str, Any]]:
    out = {}
    for source in sources:
        runs = state.get(f"parser_health:{source}") or []
        zero = 0
        for run in reversed(runs):
            if int(run.get("leads") or 0) > 0:
                break
            zero += 1
        last = runs[-1] if runs else {}
        out[source] = {"ok": zero < SOURCE_ZERO_RUNS, "zero_runs": zero, "runs": len(runs),
                       "last_leads": int(last.get("leads") or 0), "last_error": str(last.get("error") or "")[:200],
                       "last_at": last.get("at", "")}
    return out


def _daily(state: Any, key: str) -> bool:
    """True (and remembered) at most once a day per key."""
    sent = state.get("ops_alerted") or {}
    last = sent.get(key)
    if last and state.clock() - datetime.fromisoformat(last) < timedelta(hours=24):
        return False
    sent[key] = state.now()
    state.set("ops_alerted", sent)
    return True


# ------------------------------------------------------------------ Phase 107: clock check
def clock_skew(http: Any, now: datetime) -> float | None:
    """Seconds the local clock is ahead (+) or behind (-) GitHub's; None if it can't be told."""
    try:
        resp = http.request("GET", CLOCK_URL, check_robots=False, attempts=1)
        server = parsedate_to_datetime(resp.headers.get("date", ""))
    except Exception:  # noqa: BLE001 - no answer means no verdict, never an alert
        return None
    if server is None:
        return None
    if server.tzinfo is None:
        server = server.replace(tzinfo=timezone.utc)
    return (now - server).total_seconds()


# ------------------------------------------------------------------ Phase 108: battery
def power_source(run: Callable[..., Any] = subprocess.run, system: str | None = None) -> dict[str, Any]:
    """{"on_battery": bool, "percent": int | None}. Never on battery off macOS or on a desktop Mac."""
    if (system or platform.system()) != "Darwin":
        return {"on_battery": False, "percent": None}
    try:
        out = run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=10).stdout or ""
    except Exception:  # noqa: BLE001
        return {"on_battery": False, "percent": None}
    pct = re.search(r"(\d{1,3})%", out)
    return {"on_battery": "'Battery Power'" in out, "percent": int(pct.group(1)) if pct else None}


def battery_saving(state: Any) -> bool:
    """Only a recent reading counts: if the checks stopped running, builds aren't held forever."""
    record = state.get(KEY) or {}
    if not record.get("battery_saving") or not record.get("at"):
        return False
    return state.clock() - datetime.fromisoformat(record["at"]) < timedelta(hours=3)


class OpsChecks(Strategy):
    name = "ops_checks"
    tasks = ("ops_checks",)

    def __init__(self, run: Callable[..., Any] = subprocess.run, system: str | None = None):
        self._run, self._system = run, system

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        tools, cfg, state = ctx.tools, ctx.tools.config, ctx.tools.state
        record = dict(state.get(KEY) or {})
        notes = []
        # 105: sources
        health = source_health(state, list(cfg.lead_sources))
        record["sources"] = health
        bad = [s for s, h in health.items() if not h["ok"]]
        for s in bad:
            if _daily(state, f"source:{s}"):
                h = health[s]
                state.log_error("ops_checks", f"Job source {s} returned no postings for {h['zero_runs']} runs in a row"
                                + (f" (last error: {h['last_error']})" if h["last_error"] else "")
                                + ". Usually the board changed its format; the other sources keep the data coming.", kind="alert")
        if bad:
            notes.append(f"source(s) down: {', '.join(bad)}")
        # 107: clock, once a day
        last = record.get("clock_checked_at")
        if not last or state.clock() - datetime.fromisoformat(last) >= timedelta(hours=24):
            skew = clock_skew(tools.http, datetime.now(timezone.utc))
            if skew is not None:
                record["clock_checked_at"], record["clock_skew_s"] = state.now(), round(skew, 1)
                if abs(skew) > CLOCK_SKEW_SECONDS and _daily(state, "clock"):
                    state.log_error("ops_checks", f"The Mac's clock is {abs(skew) / 60:.0f} min {'fast' if skew > 0 else 'slow'}. "
                                    "Stripe rejects webhook signatures more than 5 minutes off, so sales may not be "
                                    "recorded. Fix: System Settings → General → Date & Time → Set time automatically.",
                                    kind="alert")
        if abs(float(record.get("clock_skew_s") or 0)) > CLOCK_SKEW_SECONDS:
            notes.append(f"clock off by {record['clock_skew_s']:.0f}s")
        # 108: battery
        power = power_source(self._run, self._system)
        saving = bool(cfg.battery_saver and power["on_battery"])
        if saving != bool(record.get("battery_saving")):
            state.log_action(int(state.get("iteration", 0)), None, "battery", "ok",
                             "on battery: heavy builds wait for power" if saving else "plugged in: heavy builds resume")
        record.update(battery_saving=saving, power=power, at=state.now())
        if saving:
            notes.append(f"on battery ({power['percent']}%): heavy builds wait" if power["percent"] is not None
                         else "on battery: heavy builds wait")
        # 215, 217, 218: factory health, database growth, slow tasks
        from agent.scale_checks import check as scale_check

        scale, scale_notes = scale_check(state, cfg, _daily)
        record["scale"] = scale
        notes += scale_notes
        state.set(KEY, record)
        return TaskResult(True, "ops: " + ("; ".join(notes) or "all fine"), {"sources_down": len(bad), "battery_saving": saving})
