"""What changed (Phases 275-279): each weekly refresh says what's new.

* **Phase 275, the diff:** when a product is refreshed, its new rows are compared with the previous
  version's (``diff``): companies added and gone, postings new and no longer listed. Rows are matched
  by posting link (or company, for company lists).
* **Phase 276, CHANGES.md:** the refreshed download includes the diff, with the companies by name.
* **Phase 277, remembered:** the last diff per product is kept (kv ``version_diff:<slug>``).
* **Phase 278, on the product page:** "Updated weekly. Last update (date): +N new postings, +M
  companies".
* **Phase 279, on the What's new page:** each week lists the refreshes that added something.
"""

from __future__ import annotations

import csv
import html
import io
import json
import zipfile
from typing import Any

KEY = "version_diff:"
PRIMARY = ("leads.csv", "companies.csv", "salaries.csv")


def _rows(files: dict[str, Any]) -> list[dict[str, str]]:
    for name in PRIMARY:
        body = files.get(name)
        if body is not None:
            text = body.decode("utf-8-sig") if isinstance(body, bytes) else str(body)
            return list(csv.DictReader(io.StringIO(text)))
    return []


def old_files(tools: Any, path: str) -> dict[str, bytes]:
    try:
        with zipfile.ZipFile(io.BytesIO(tools.files.read_bytes(path))) as zf:
            return {n.split("/", 1)[-1]: zf.read(n) for n in zf.namelist() if n.split("/", 1)[-1] in PRIMARY}
    except (OSError, zipfile.BadZipFile, KeyError):
        return {}


# ------------------------------------------------------------------ Phase 275
def diff(old: list[dict[str, str]], new: list[dict[str, str]]) -> dict[str, Any]:
    def key(r: dict[str, str]) -> str:
        return (r.get("url") or r.get("example_url") or "") if "url" in r else (r.get("company") or "").lower()

    def companies(rows: list[dict[str, str]]) -> dict[str, str]:
        return {(r.get("company") or "").strip().lower(): (r.get("company") or "").strip() for r in rows if r.get("company")}

    old_keys, new_keys = {key(r) for r in old}, {key(r) for r in new}
    old_cos, new_cos = companies(old), companies(new)
    return {"new_rows": len(new_keys - old_keys), "gone_rows": len(old_keys - new_keys),
            "companies_added": sorted(new_cos[k] for k in set(new_cos) - set(old_cos)),
            "companies_gone": sorted(old_cos[k] for k in set(old_cos) - set(new_cos)), "rows": len(new)}


# ------------------------------------------------------------------ Phase 276
def changes_md(title: str, d: dict[str, Any], version: int, date: str) -> str:
    def names(items: list[str]) -> str:
        return ", ".join(items[:50]) + (f" and {len(items) - 50} more" if len(items) > 50 else "") if items else "none"

    return (f"# What changed in {title}\n\nVersion {version}, {date}.\n\n- New rows: {d['new_rows']}\n"
            f"- Rows no longer listed: {d['gone_rows']}\n- Companies added ({len(d['companies_added'])}): "
            f"{names(d['companies_added'])}\n- Companies gone ({len(d['companies_gone'])}): {names(d['companies_gone'])}\n")


# ------------------------------------------------------------------ Phases 275-277 together
def on_refresh(tools: Any, slug: str, title: str, old_path: str, made: dict[str, Any], version: int) -> dict[str, Any] | None:
    """Adds CHANGES.md to ``made`` and remembers the diff. None when the old version can't be read."""
    old = _rows(old_files(tools, old_path))
    if not old:
        return None
    d = diff(old, _rows(made["files"]))
    date = tools.state.now()[:10]
    made["files"]["CHANGES.md"] = changes_md(title, d, version, date).encode()
    tools.state.set(KEY + slug, {**{k: v for k, v in d.items() if k not in ("companies_added", "companies_gone")},
                                 "companies_added": len(d["companies_added"]), "companies_gone": len(d["companies_gone"]),
                                 "at": tools.state.now(), "version": version})
    return d


# ------------------------------------------------------------------ Phase 278
def update_note(state: Any, slug: str) -> str:
    d = state.get(KEY + slug)
    if not d:
        return ""
    unit = "postings" if d.get("rows") is not None else "rows"
    bits = [f"+{d['new_rows']} new {unit}"] + ([f"+{d['companies_added']} companies"] if d.get("companies_added") else [])
    return (f'<p class="muted"><b>Updated weekly.</b> Last update ({html.escape(str(d["at"])[:10])}): '
            + html.escape(", ".join(bits)) + ".</p>")


# ------------------------------------------------------------------ Phase 279
def refreshes(state: Any, since: str) -> list[tuple[str, str, dict[str, Any]]]:
    out = []
    for row in state._all("SELECT key, value FROM kv WHERE key LIKE ?", (KEY + "%",)):
        d = json.loads(row["value"])
        if str(d.get("at") or "") >= since and (d.get("new_rows") or d.get("companies_added")):
            slug = row["key"][len(KEY):]
            title = (state._one("SELECT title FROM factory_products WHERE slug = ? AND status = 'live'", (slug,)) or {}).get("title")
            if title:
                out.append((slug, title, d))
    return out
