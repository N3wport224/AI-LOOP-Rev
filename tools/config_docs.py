"""Every setting, documented from the code (Phase 116).

``settings()`` reads the ``Config`` dataclass in ``agent/config.py``: each field's name, type,
default and the comment written next to it (or the section comment above it). ``automonetize
config-docs`` prints them; ``--markdown`` prints a table you can paste. Because it's read from the
code, it's never out of date.
"""

from __future__ import annotations

import ast
import re
from dataclasses import MISSING, fields
from pathlib import Path
from typing import Any

CONFIG_SRC = Path(__file__).resolve().parent.parent / "agent" / "config.py"
SECRET_HINT = re.compile(r"(key|token|secret|password)$")


def _comments(src: str) -> dict[str, tuple[str, str]]:
    """field -> (its own trailing comment, the section comment above its group)."""
    tree = ast.parse(src)
    lines = src.splitlines()
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Config")
    out: dict[str, tuple[str, str]] = {}
    section = ""
    prev_end = cls.body[0].lineno - 1 if cls.body else 0
    for node in cls.body:
        if not (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)):
            prev_end = getattr(node, "end_lineno", prev_end)
            continue
        between = [line.strip() for line in lines[prev_end:node.lineno - 1]]
        block: list[str] = []
        for b in reversed(between):  # the comment lines right above the field
            if not b.startswith("#"):
                break
            block.insert(0, b[1:].strip())
        before_block = between[:len(between) - len(block)]
        line = lines[node.lineno - 1]
        trailing = line.split("#", 1)[1].strip() if "#" in line and not line.strip().startswith("#") else ""
        header = len(block) == 1 and len(block[0]) < 40 and (not before_block or before_block[-1] == "")
        if header:
            section = block[0]
        elif block and not trailing:
            trailing = " ".join(block)
        out[node.target.id] = (trailing, section)
        prev_end = node.end_lineno or node.lineno
    return out


def settings() -> list[dict[str, Any]]:
    from agent.config import Config

    notes = _comments(CONFIG_SRC.read_text())
    out = []
    for f in fields(Config):
        if f.name.startswith("_"):
            continue
        if f.default is not MISSING:
            default: Any = f.default
        elif f.default_factory is not MISSING:  # type: ignore[misc]
            default = f.default_factory()  # type: ignore[misc]
        else:
            default = ""
        if SECRET_HINT.search(f.name) and default:
            default = "(set)"
        own, section = notes.get(f.name, ("", ""))
        out.append({"name": f.name, "type": str(f.type).replace("typing.", ""), "default": default, "help": own,
                    "section": section})
    return out


def _show(value: Any) -> str:
    text = repr(value) if not isinstance(value, Path) else str(value)
    return text if len(text) <= 60 else text[:57] + "..."


def as_text(rows: list[dict[str, Any]]) -> str:
    out, section = [], None
    for r in rows:
        if r["section"] != section:
            section = r["section"]
            out.append(f"\n[{section or 'General'}]")
        out.append(f"  {r['name']} = {_show(r['default'])}" + (f"   # {r['help']}" if r["help"] else ""))
    return "\n".join(out).lstrip("\n")


def as_markdown(rows: list[dict[str, Any]]) -> str:
    lines = ["| Setting | Default | What it does |", "|---|---|---|"]
    lines += [f"| `{r['name']}` | `{_show(r['default'])}` | {r['help'].replace('|', '/')} |" for r in rows]
    return "\n".join(lines)
