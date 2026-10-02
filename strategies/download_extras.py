"""Richer downloads (Phases 270-274): every factory download is easier to use and to trust.

Added to each new product and each weekly refresh (starter packs keep their member folders and get
only the checksums):

* **Phase 270, a ready database:** ``data.sqlite``, the main CSV as a SQLite table (open it in any
  SQL tool, DB Browser for SQLite or Datasette), typed columns.
* **Phase 271, a JSON Schema:** ``schema.json`` describes every column of the main CSV (type, and
  whether it's ever empty), for data pipelines.
* **Phase 272, the licence in the box:** ``LICENSE.txt``: use the data inside your organisation,
  don't resell or republish the raw rows, credit the job boards for each posting.
* **Phase 273, how to cite it:** ``CITATION.cff``, for reports and research.
* **Phase 274, checksums:** ``SHA256SUMS`` lists every file's SHA-256, and the factory's hourly
  integrity check (Phase 243) also verifies them for ``VERIFY_PER_HOUR`` downloads, so a corrupted
  file is rebuilt like a missing one.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
import zipfile
from datetime import datetime
from typing import Any

PRIMARY = ("leads.csv", "companies.csv", "salaries.csv")
EXTRA_FILES = ["data.sqlite", "schema.json", "LICENSE.txt", "CITATION.cff", "SHA256SUMS"]
SUMS = "SHA256SUMS"
VERIFY_PER_HOUR = 20


def _primary(files: dict[str, bytes]) -> tuple[str, list[dict[str, str]], list[str]] | None:
    for name in PRIMARY:
        if name in files:
            text = files[name].decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(text))
            return name, list(reader), list(reader.fieldnames or [])
    return None


def _kind(values: list[str]) -> str:
    present = [v for v in values if v not in ("", None)]
    if not present:
        return "string"
    if all(v in ("True", "False", "true", "false") for v in present):
        return "boolean"
    try:
        [int(v) for v in present]
        return "integer"
    except ValueError:
        pass
    try:
        [float(v) for v in present]
        return "number"
    except ValueError:
        return "string"


# ------------------------------------------------------------------ Phase 270
def sqlite_bytes(table: str, rows: list[dict[str, str]], fields: list[str]) -> bytes:
    kinds = {f: _kind([r.get(f, "") for r in rows]) for f in fields}
    sql_type = {"integer": "INTEGER", "number": "REAL", "boolean": "INTEGER", "string": "TEXT"}
    conn = sqlite3.connect(":memory:")
    cols = ", ".join(f'"{f}" {sql_type[kinds[f]]}' for f in fields)
    conn.execute(f'CREATE TABLE "{table}" ({cols})')

    def value(f: str, v: str) -> Any:
        if v == "":
            return None
        k = kinds[f]
        return int(v) if k == "integer" else float(v) if k == "number" else int(v.lower() == "true") if k == "boolean" else v
    conn.executemany(f'INSERT INTO "{table}" VALUES ({", ".join("?" for _ in fields)})',
                     [[value(f, r.get(f, "")) for f in fields] for r in rows])
    conn.commit()
    data = conn.serialize()
    conn.close()
    return bytes(data)


# ------------------------------------------------------------------ Phase 271
def json_schema(title: str, rows: list[dict[str, str]], fields: list[str]) -> str:
    props = {}
    for f in fields:
        values = [r.get(f, "") for r in rows]
        kind = _kind(values)
        props[f] = {"type": [kind, "null"] if any(v == "" for v in values) else kind}
    return json.dumps({"$schema": "https://json-schema.org/draft/2020-12/schema", "title": title, "type": "array",
                       "items": {"type": "object", "properties": props, "required": [f for f in fields
                                                                                     if "null" not in props[f]["type"]]}},
                      indent=2)


# ------------------------------------------------------------------ Phases 272-273
def license_txt(cfg: Any, title: str, now: datetime) -> str:
    seller = cfg.sender_name or cfg.site_title or "the seller"
    return (f"{title}\nCopyright {now:%Y} {seller}.\n\nYou may use this dataset inside your organisation: analyse it, "
            "import it into your tools, share it with colleagues.\nYou may not resell, sublicense or republish the raw rows, "
            "in whole or in large part.\nEach row comes from a public job posting; the link to the original is included. "
            "Credit the job board when you quote a posting.\nThe data is provided as is, without warranty.\n")


def citation_cff(cfg: Any, title: str, version: int, now: datetime, url: str) -> str:
    seller = (cfg.sender_name or cfg.site_title or "Dataset publisher").replace('"', "'")
    lines = ["cff-version: 1.2.0", 'message: "If you use this dataset, please cite it as below."', 'type: dataset',
             f'title: "{title}"', f'version: "{version}"', f'date-released: "{now:%Y-%m-%d}"', "authors:",
             f'  - name: "{seller}"']
    if url:
        lines.append(f'url: "{url}"')
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ Phase 274
def sha256sums(files: dict[str, bytes]) -> str:
    return "".join(f"{hashlib.sha256(body).hexdigest()}  {name}\n" for name, body in sorted(files.items()) if name != SUMS)


def verify_zip(data: bytes) -> bool:
    """True when every file listed in SHA256SUMS is present with its hash (no list: nothing to check)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            sums = next((n for n in names if n.endswith("/" + SUMS) or n == SUMS), None)
            if sums is None:
                return True
            prefix = sums[: -len(SUMS)]
            for line in zf.read(sums).decode().splitlines():
                digest, _, name = line.partition("  ")
                if hashlib.sha256(zf.read(prefix + name)).hexdigest() != digest:
                    return False
    except (zipfile.BadZipFile, KeyError, OSError, ValueError):  # ValueError: a SHA256SUMS that isn't UTF-8
        return False
    return True


def enrich(made: dict[str, Any], cfg: Any, title: str, slug: str, version: int, kind: str, now: datetime) -> dict[str, bytes]:
    files = {name: (body if isinstance(body, bytes) else str(body).encode()) for name, body in made["files"].items()}
    if kind != "pack":
        primary = _primary(files)
        if primary:
            name, rows, fields = primary
            files["data.sqlite"] = sqlite_bytes(name.removesuffix(".csv"), rows, fields)
            files["schema.json"] = json_schema(title, rows, fields).encode()
        url = f"{cfg.pages_base_url.rstrip('/')}/{slug}/" if cfg.pages_base_url else ""
        files["LICENSE.txt"] = license_txt(cfg, title, now).encode()
        files["CITATION.cff"] = citation_cff(cfg, title, version, now, url).encode()
    files[SUMS] = sha256sums(files).encode()
    return files
