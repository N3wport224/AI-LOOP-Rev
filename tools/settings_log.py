"""Settings change log (Phase 91): what changed in the configuration, and when.

At the first cycle after each start, the agent compares its settings with the last ones it saw
and records the difference ("dry_run: true → false", "owner_email changed"), so "it worked
yesterday" has an answer. Secrets are never written: only whether they're set and whether they
changed. kv ``settings_snapshot`` and ``settings_history`` (newest 100); shown by
``automonetize settings-log``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import fields
from typing import Any

SNAPSHOT = "settings_snapshot"
HISTORY = "settings_history"
SECRET_RE = re.compile(r"key|secret|token|password|passwd|topic|heartbeat_url", re.I)


def snapshot(config: Any) -> dict[str, str]:
    out = {}
    for f in fields(config):
        value = getattr(config, f.name)
        if SECRET_RE.search(f.name):
            out[f.name] = "set:" + hashlib.sha256(str(value).encode()).hexdigest()[:8] if value else "empty"
        else:
            text = value if isinstance(value, str) else json.dumps(value, default=str, sort_keys=True)
            out[f.name] = str(text)[:200]
    return out


def diff(old: dict[str, str], new: dict[str, str]) -> list[str]:
    changes = []
    for name in sorted(set(old) | set(new)):
        a, b = old.get(name), new.get(name)
        if a == b or a is None:  # a new setting (from an update) isn't a change you made
            continue
        if SECRET_RE.search(name):
            changes.append(f"{name}: " + ("removed" if b == "empty" else "set" if a == "empty" else "changed"))
        else:
            changes.append(f"{name}: {a} → {b}")
    return changes


def record(state: Any, config: Any) -> list[str]:
    new = snapshot(config)
    old = state.get(SNAPSHOT)
    state.set(SNAPSHOT, new)
    if not old:
        return []
    changes = diff(old, new)
    if changes:
        history = list(state.get(HISTORY) or [])
        history.append({"at": state.now(), "changes": changes})
        state.set(HISTORY, history[-100:])
        state.log_action(int(state.get("iteration", 0)), None, "settings", "ok", "; ".join(changes)[:500])
    return changes
