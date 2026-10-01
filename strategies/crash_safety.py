"""Crash safety (Phases 395-399): a Mac that loses power mid-build comes back clean.

File writes were already atomic (written to a temporary file, then renamed). This covers the rest:

* **Phase 395, leftover temporary files:** a write interrupted by a crash leaves a ``.tmp-*`` file
  next to its target. Housekeeping deletes those older than ``TMP_MAX_AGE_HOURS`` (6).
* **Phase 396, one factory run at a time:** the factory worker, the cycle's catch-up and
  ``automonetize factory --now`` (a separate process) share a lease in the database (table ``locks``,
  an atomic claim that expires after ``LEASE_SECONDS``), so two runs can never build the same product
  or open two checkouts for it.
* **Phase 397, half-made products:** a product folder under ``assets/`` that no product refers to (a
  build interrupted before it was recorded) is deleted after ``ORPHAN_HOURS`` (24).
* **Phase 398, products waiting without their download:** a product waiting for its checkout whose
  download file is missing is removed (and made again by the factory when its postings allow).
* **Phase 399, tested:** a test runs two factory runs at the same time and interrupts a build.
"""

from __future__ import annotations

import os
import secrets
import shutil
import time
from contextlib import contextmanager
from typing import Any, Iterator

TMP_MAX_AGE_HOURS = 6
LEASE_SECONDS = 900
ORPHAN_HOURS = 24
_SCHEMA = "CREATE TABLE IF NOT EXISTS locks (name TEXT PRIMARY KEY, owner TEXT NOT NULL, until REAL NOT NULL)"


# ------------------------------------------------------------------ Phase 395
def sweep_tmp(root: Any, now: float | None = None) -> int:
    now = now or time.time()
    removed = 0
    for path in root.rglob(".tmp-*"):
        try:
            if path.is_file() and now - path.stat().st_mtime > TMP_MAX_AGE_HOURS * 3600:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


# ------------------------------------------------------------------ Phase 396
def claim(state: Any, name: str, owner: str, seconds: int = LEASE_SECONDS, now: float | None = None) -> bool:
    """Atomically take the lease ``name`` if it's free or expired (or already ours)."""
    now = now if now is not None else time.time()
    state._exec(_SCHEMA)
    state._exec("INSERT INTO locks (name, owner, until) VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE SET "
                "owner = excluded.owner, until = excluded.until WHERE locks.until < ? OR locks.owner = excluded.owner",
                (name, owner, now + seconds, now))
    row = state._one("SELECT owner FROM locks WHERE name = ?", (name,))
    return bool(row) and row["owner"] == owner


def release(state: Any, name: str, owner: str) -> None:
    state._exec(_SCHEMA)
    state._exec("DELETE FROM locks WHERE name = ? AND owner = ?", (name, owner))


@contextmanager
def lease(state: Any, name: str) -> Iterator[bool]:
    """``with lease(state, "factory") as mine:`` -- ``mine`` is False when another run holds it."""
    owner = f"{os.getpid()}-{secrets.token_hex(4)}"
    mine = claim(state, name, owner)
    try:
        yield mine
    finally:
        if mine:
            release(state, name, owner)


# ------------------------------------------------------------------ Phases 397-398
def orphan_folders(state: Any, files: Any, now_ts: float | None = None) -> list[str]:
    """Folders' ages come from the file system, so they're measured with the real clock."""
    from strategies.product_factory import ensure

    ensure(state)
    if not files.exists("assets"):
        return []
    known = {r["slug"] for r in state._all("SELECT slug FROM factory_products")}
    known |= {str(r["niche"]) for r in state._all("SELECT DISTINCT niche FROM assets WHERE niche IS NOT NULL")}
    cutoff = (now_ts or time.time()) - ORPHAN_HOURS * 3600
    out = []
    for name in files.list_dir("assets"):
        path = files.resolve(f"assets/{name}")
        if not path.is_dir() or name in known:
            continue
        if path.stat().st_mtime < cutoff:
            out.append(name)
    return out


def recover(state: Any, files: Any, now_ts: float | None = None) -> dict[str, int]:
    from strategies.product_factory import ensure

    ensure(state)
    out = {}
    orphans = orphan_folders(state, files, now_ts)
    for name in orphans:
        shutil.rmtree(files.resolve(f"assets/{name}"), ignore_errors=True)
    if orphans:
        out["half-made product folders"] = len(orphans)
    broken = 0
    for r in state._all("SELECT f.slug AS slug, f.asset_id AS aid, a.path AS path FROM factory_products f "
                        "LEFT JOIN assets a ON a.id = f.asset_id WHERE f.status = 'staged'"):
        if not r["path"] or not files.exists(r["path"]):
            state._exec("DELETE FROM factory_products WHERE slug = ?", (r["slug"],))
            if r["aid"]:
                state._exec("DELETE FROM assets WHERE id = ? AND status = 'staged'", (r["aid"],))
            broken += 1
    if broken:
        out["waiting products without a download"] = broken
    return out
