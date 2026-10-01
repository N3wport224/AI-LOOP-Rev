"""Dependency check (Phase 115): are the installed packages new enough?

After a ``git pull`` the code may need a newer library than the one installed (the requirement in
``pyproject.toml`` moved on but nobody re-ran ``pip install``). ``check()`` compares each
requirement with what's installed and returns what's missing or too old, with the one command that
fixes it. Shown in ``automonetize doctor``, the Health tab and ``automonetize deps``.
"""

from __future__ import annotations

import re
import tomllib
from importlib import metadata
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
FIX = "pip install -e '.[dev]' (in the AutoMonetize folder, after `am`)"
_REQ = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*(?:>=\s*([0-9][0-9A-Za-z.]*))?")


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", text.split("+")[0])[:4])


def requirements(pyproject: Path | None = None, extras: tuple[str, ...] = ("dev",)) -> list[tuple[str, str]]:
    """[(package, minimum)] from pyproject.toml ("" minimum when none is given)."""
    try:
        data = tomllib.loads((pyproject or ROOT / "pyproject.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return []
    project = data.get("project") or {}
    reqs = list(project.get("dependencies") or [])
    for extra in extras:
        reqs += list((project.get("optional-dependencies") or {}).get(extra) or [])
    out = []
    for r in reqs:
        m = _REQ.match(r)
        if m:
            out.append((m.group(1), m.group(2) or ""))
    return out


def check(pyproject: Path | None = None, installed: Callable[[str], str] = metadata.version,
          optional: tuple[str, ...] = ("pytest", "Pillow")) -> list[dict[str, Any]]:
    """Problems only: [{"package", "need", "have"}]; ``have`` is "" when not installed.
    Packages in ``optional`` (tests, image previews) only count when installed but too old."""
    problems = []
    for name, minimum in requirements(pyproject):
        try:
            have = installed(name)
        except metadata.PackageNotFoundError:
            if name not in optional:
                problems.append({"package": name, "need": f">={minimum}" if minimum else "any", "have": ""})
            continue
        if minimum and _version(have) < _version(minimum):
            problems.append({"package": name, "need": f">={minimum}", "have": have})
    return problems


def describe(problems: list[dict[str, Any]]) -> str:
    return "; ".join(f"{p['package']} {p['have'] or 'missing'} (needs {p['need']})" for p in problems)
