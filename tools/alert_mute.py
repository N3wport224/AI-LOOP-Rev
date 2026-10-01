"""Mute an alert source (Phase 137): stop emails about something you already know about.

``automonetize mute <source> [--days N]`` (default 7 days) stops alert emails and phone buzzes from
that source, e.g. ``ops_checks`` while you're fixing a job board, or ``site_audit`` during a
redesign. The alerts are still recorded and still shown by ``doctor`` and the Logs tab; the mute ends
by itself. ``automonetize mute --list`` / ``unmute <source>``. State: kv ``muted_alerts``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

KEY = "muted_alerts"


def mute(state: Any, source: str, days: float = 7) -> str:
    until = (state.clock() + timedelta(days=days)).isoformat(timespec="seconds")
    muted = dict(state.get(KEY) or {})
    muted[source] = until
    state.set(KEY, muted)
    return until


def unmute(state: Any, source: str) -> bool:
    muted = dict(state.get(KEY) or {})
    found = muted.pop(source, None) is not None
    state.set(KEY, muted)
    return found


def active(state: Any) -> dict[str, str]:
    now = state.clock()
    return {s: u for s, u in (state.get(KEY) or {}).items() if datetime.fromisoformat(u) > now}


def is_muted(state: Any, source: str) -> bool:
    return source in active(state)


def sources(state: Any, days: int = 30) -> list[str]:
    since = (state.clock() - timedelta(days=days)).isoformat(timespec="seconds")
    return [r["source"] for r in state._all("SELECT DISTINCT source FROM errors WHERE kind = 'alert' AND created_at >= ? "
                                             "ORDER BY source", (since,))]
