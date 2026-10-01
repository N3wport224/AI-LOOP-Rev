"""Version info (Phase 73): which code is running, so "is the update installed?" has an answer.

``info(root)`` reads the package version from ``pyproject.toml`` and the current commit (short sha,
date, subject, branch) from git, if available. Shown by ``automonetize version``, the control panel
status, the daily report footer and the "agent started" notice.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]


def info(root: Path = ROOT, run: Callable[..., Any] = subprocess.run) -> dict[str, str]:
    out = {"version": "", "commit": "", "date": "", "subject": "", "branch": ""}
    try:
        m = re.search(r'^version\s*=\s*"([^"]+)"', (root / "pyproject.toml").read_text(), re.M)
        out["version"] = m.group(1) if m else ""
    except OSError:
        pass
    try:
        proc = run(["git", "log", "-1", "--format=%h%x09%cs%x09%s"], cwd=str(root), capture_output=True, text=True, timeout=10)
        if proc.returncode == 0 and proc.stdout.strip():
            out["commit"], out["date"], out["subject"] = (proc.stdout.strip().split("\t") + ["", "", ""])[:3]
        branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(root), capture_output=True, text=True, timeout=10)
        out["branch"] = branch.stdout.strip() if branch.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        pass
    return out


def describe(v: dict[str, str]) -> str:
    if not v.get("commit"):
        return f"AutoMonetize {v.get('version') or '?'}"
    return f"AutoMonetize {v.get('version') or '?'} ({v['commit']}, {v['date']})"
