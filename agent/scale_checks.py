"""Ops at scale (Phases 215-219): a catalog that keeps growing, watched.

These run inside ``ops_checks`` (every cycle, cheap) and show in ``automonetize doctor``:

* **Phase 215, factory health:** the factory worker records each tick (kv ``factory_tick``). With
  the factory on, no tick for ``FACTORY_STALL_INTERVALS`` intervals (at least 30 minutes), or no new
  product for ``NO_PRODUCT_HOURS``, is reported, once a day, with the factory's last reason.
* **Phase 216, indexes:** the queries a large catalog runs most (orders by asset, assets by niche,
  factory products by status, recent actions) use indexes (``agent/state.py``).
* **Phase 217, database growth:** the database size is sampled once a day (kv ``db_growth``, 30
  days). Growth over ``DB_GROWTH_MB_DAY`` per day, or a database over ``DB_WARN_MB``, is reported once
  a day: usually a log that housekeeping isn't pruning.
* **Phase 218, slow tasks:** a task whose last ``SLOW_RUNS`` runs each took longer than
  ``SLOW_SECONDS`` (a site build with thousands of pages, say) is reported once a day.
* **Phase 219, scale test:** ``tests/test_scale_checks.py`` builds a catalog of hundreds of
  products and checks the hot paths stay fast and indexed.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

FACTORY_KEY = "factory_tick"
FACTORY_STALL_INTERVALS = 3
NO_PRODUCT_HOURS = 24
GROWTH_KEY = "db_growth"
DB_GROWTH_MB_DAY = 200
DB_WARN_MB = 2000
SLOW_SECONDS = 300
SLOW_RUNS = 3


# ------------------------------------------------------------------ Phase 215
def record_factory_tick(state: Any, ok: bool, why: str) -> None:
    state.set(FACTORY_KEY, {"at": state.now(), "ok": ok, "why": why})


def factory_health(state: Any, cfg: Any) -> dict[str, Any] | None:
    """None when the factory is off; otherwise ``{"ok", "detail", "fix"}``."""
    if not cfg.product_factory:
        return None
    from strategies.product_factory import MADE, catalog

    now = state.clock()
    tick = state.get(FACTORY_KEY) or {}
    stall = timedelta(seconds=max(1800, FACTORY_STALL_INTERVALS * int(cfg.factory_interval_seconds)))
    if tick and now - datetime.fromisoformat(tick["at"]) > stall:
        return {"ok": False, "detail": f"the factory hasn't run since {tick['at'][:16]}",
                "fix": "restart the agent (automonetize stop, then am); see the Logs tab for product_factory errors"}
    if tick and not tick.get("ok"):
        return {"ok": False, "detail": f"last factory run failed: {tick.get('why', '')}"[:200],
                "fix": "see the Logs tab; it retries every interval"}
    made = state.get(MADE) or ""
    counts = catalog(state)
    if made and now - datetime.fromisoformat(made) > timedelta(hours=NO_PRODUCT_HOURS):
        return {"ok": False, "detail": f"no new product since {made[:16]} ({tick.get('why') or 'no new slice yet'}); "
                                       f"catalog {counts}",
                "fix": "usually waiting for more postings: check Job sources above"}
    return {"ok": True, "detail": f"catalog {counts}" + (f"; last run {tick['at'][:16]}" if tick else ""), "fix": ""}


# ------------------------------------------------------------------ Phase 217
def sample_db(state: Any, size_bytes: int | None = None) -> dict[str, Any]:
    """Once a day: remember the database size; returns ``{"mb", "per_day_mb", "warn"}``."""
    if size_bytes is None:
        path = Path(state.db_path)
        size_bytes = sum(p.stat().st_size for p in (path, Path(f"{path}-wal")) if p.exists())
    samples = list(state.get(GROWTH_KEY) or [])
    now = state.clock()
    if not samples or now - datetime.fromisoformat(samples[-1]["at"]) >= timedelta(hours=23):
        samples = (samples + [{"at": state.now(), "bytes": int(size_bytes)}])[-30:]
        state.set(GROWTH_KEY, samples)
    mb = samples[-1]["bytes"] / 1e6
    per_day = 0.0
    if len(samples) >= 2:
        first, last = samples[max(0, len(samples) - 8)], samples[-1]
        days = (datetime.fromisoformat(last["at"]) - datetime.fromisoformat(first["at"])).total_seconds() / 86400
        per_day = (last["bytes"] - first["bytes"]) / 1e6 / days if days >= 0.9 else 0.0
    warn = ""
    if per_day > DB_GROWTH_MB_DAY:
        warn = f"the database grows {per_day:,.0f} MB a day ({mb:,.0f} MB now)"
    elif mb > DB_WARN_MB:
        warn = f"the database is {mb:,.0f} MB"
    return {"mb": round(mb, 1), "per_day_mb": round(per_day, 1), "warn": warn}


# ------------------------------------------------------------------ Phase 218
def slow_tasks(state: Any) -> dict[str, float]:
    """Tasks whose last ``SLOW_RUNS`` runs (in the past 2 days) each took over ``SLOW_SECONDS``: name → slowest."""
    since = (state.clock() - timedelta(days=2)).isoformat(timespec="seconds")
    rows = state._all("SELECT name, duration FROM actions WHERE created_at >= ? AND status = 'ok' ORDER BY id DESC",
                      (since,))
    runs: dict[str, list[float]] = {}
    for r in rows:
        runs.setdefault(r["name"], []).append(float(r["duration"] or 0))
    return {name: round(max(d[:SLOW_RUNS]), 1) for name, d in runs.items()
            if len(d) >= SLOW_RUNS and min(d[:SLOW_RUNS]) > SLOW_SECONDS}


def check(state: Any, cfg: Any, daily: Any) -> tuple[dict[str, Any], list[str]]:
    """Run all three; alert once a day each. Returns (record for kv ``ops``, notes)."""
    notes = []
    factory = factory_health(state, cfg)
    if factory and not factory["ok"]:
        notes.append("factory: " + factory["detail"])
        if daily(state, "factory"):
            state.log_error("ops_checks", f"Product factory: {factory['detail']}. {factory['fix']}", kind="alert")
    db = sample_db(state)
    if db["warn"]:
        notes.append(db["warn"])
        if daily(state, "db_growth"):
            state.log_error("ops_checks", f"{db['warn'][0].upper()}{db['warn'][1:]}. Housekeeping prunes old logs nightly; "
                                          "if this keeps up, see automonetize doctor.", kind="alert")
    slow = slow_tasks(state)
    for name, secs in slow.items():
        notes.append(f"{name} slow ({secs / 60:.0f} min)")
        if daily(state, f"slow:{name}"):
            hint = " A very large site: consider fewer hub pages." if name == "build_site" else ""
            state.log_error("ops_checks", f"Task {name} took over {SLOW_SECONDS // 60} minutes on each of its last "
                                          f"{SLOW_RUNS} runs (up to {secs / 60:.0f} min), delaying everything after it.{hint}",
                            kind="alert")
    return {"factory": factory, "db": db, "slow": slow}, notes
