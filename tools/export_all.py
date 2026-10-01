"""Full data export (Phase 93): everything the agent knows, in plain files you can open anywhere.

``automonetize export-all`` writes ``data/exports/all-YYYYMMDD-HHMM.zip`` (mode 600): one CSV per
database table (orders, revenue, customers' subscriptions, leads, expenses, logs...), the key-value
settings store as JSON, and a README. Your keys stay in ``.env`` and aren't included; API keys are
stored only as hashes anyway. Useful for an accountant, a spreadsheet, or moving elsewhere.
"""

from __future__ import annotations

import csv
import io
import json
import os
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any


def export_all(state: Any, data_dir: Path, now: datetime) -> tuple[Path, dict[str, int]]:
    tables = [r["name"] for r in state._all("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                                            "ORDER BY name")]
    counts: dict[str, int] = {}
    target = Path(data_dir) / "exports" / f"all-{now:%Y%m%d-%H%M}.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for table in tables:
            if table == "kv":
                continue
            rows = state._all(f'SELECT * FROM "{table}"')
            counts[table] = len(rows)
            buf = io.StringIO()
            cols = [c["name"] for c in state._all(f'PRAGMA table_info("{table}")')]
            w = csv.writer(buf)
            w.writerow(cols)
            for r in rows:
                w.writerow([r.get(c) for c in cols])
            zf.writestr(f"tables/{table}.csv", buf.getvalue())
        private = {"owner_command_code"}  # lets someone control the agent by email: never leaves the Mac
        kv = {r["key"]: json.loads(r["value"]) for r in state._all("SELECT key, value FROM kv ORDER BY key") if r["key"] not in private}
        counts["kv"] = len(kv)
        zf.writestr("settings_store.json", json.dumps(kv, indent=2, default=str))
        zf.writestr("README.txt", f"AutoMonetize data export, {now:%Y-%m-%d %H:%M}.\n\nOne CSV per table in tables/; the agent's "
                                  "key-value store in settings_store.json. Your keys (.env) are not included.\n")
    os.chmod(target, 0o600)
    return target, counts
