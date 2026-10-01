"""Housekeeping: keep a Mac that runs for months tidy, without ever losing anything that matters.

``housekeeping`` runs once a day (part of ``agent/``):

* **Logs:** actions older than ``log_keep_days`` (90) and errors older than twice that are deleted
  (alerts are kept as long as errors). Handled webhook events older than 90 days, and the contact
  log after 400 days (the offer rules only look back a year), go too.
* **Data retention** (Phase 117): job postings not seen for ``lead_keep_days`` (365) are deleted
  (datasets already built keep their copy), and free-sample sign-ups that never confirmed their
  address within ``unconfirmed_signup_days`` (30) are deleted with their send history: data
  nobody needs isn't kept.
* **Service logs** (Phase 132): launchd's ``~/Library/Logs/automonetize/*.log`` files over 20 MB
  keep their last 2 MB; ``data/agent.log`` rotates by itself at 10 MB (Phase 131).
* **Email audit log:** once ``dispatched_audit.log`` passes 5 MB it is gzipped to
  ``dispatched_audit-YYYYMMDD.log.gz``; archives older than a year are deleted.
* **Old dataset versions:** the newest ``keep_versions`` (3) versions of each dataset are kept;
  older zip files and version folders are deleted *unless an order points at them* (order
  recovery re-sends exactly what was bought), or another product still uses the file. The database
  rows stay, so sales history is complete.
* **Database:** ``PRAGMA optimize`` keeps queries fast. Once a month (Phase 106) ``VACUUM`` gives
  the space of deleted rows back to the disk and defragments the file (a few seconds; skipped
  when free disk is under twice the database size, since VACUUM needs a temporary copy).

Every run records what it removed in kv ``housekeeping``.
"""

from __future__ import annotations

import gzip
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from strategies.base import Strategy, TaskContext, TaskResult

KEY = "housekeeping"
AUDIT_ROTATE_BYTES = 5 * 1024 * 1024
ARCHIVE_KEEP_DAYS = 365
CONTACT_KEEP_DAYS = 400
WEBHOOK_KEEP_DAYS = 90
VACUUM_DAYS = 30
LAUNCHD_LOG_MAX = 20 * 1024 * 1024
LAUNCHD_LOG_KEEP = 2 * 1024 * 1024


def trim_launchd_logs(log_dir: Path | None = None) -> int:
    """Phase 132: launchd appends the agent's stdout/stderr to ~/Library/Logs/automonetize forever.
    A file over 20 MB keeps only its last 2 MB (rewritten in place: launchd's append handle keeps
    working). Returns how many files were trimmed."""
    log_dir = log_dir or Path.home() / "Library" / "Logs" / "automonetize"
    trimmed = 0
    for path in sorted(log_dir.glob("*.log")) if log_dir.is_dir() else []:
        try:
            size = path.stat().st_size
            if size <= LAUNCHD_LOG_MAX:
                continue
            with open(path, "r+b") as fh:
                fh.seek(size - LAUNCHD_LOG_KEEP)
                fh.readline()  # start at a whole line
                tail = fh.read()
                fh.seek(0)
                fh.write(b"[older lines trimmed by housekeeping]\n" + tail)
                fh.truncate()
            trimmed += 1
        except OSError:
            continue
    return trimmed


def maybe_vacuum(state: Any, db_path: Path, free_bytes: int | None = None) -> int:
    """Phase 106: VACUUM once a month. Returns bytes reclaimed (0 when skipped or nothing changed)."""
    last = state.get("vacuum_at")
    if last and state.clock() - datetime.fromisoformat(last) < timedelta(days=VACUUM_DAYS):
        return 0
    db = Path(db_path)
    size = db.stat().st_size if db.exists() else 0
    free = shutil.disk_usage(str(db.parent)).free if free_bytes is None and db.exists() else (free_bytes or 0)
    if not size or free < 2 * size:
        return 0
    try:
        state._exec("VACUUM")
    except sqlite3.OperationalError as exc:  # busy (another process reading): try again tomorrow, don't fail housekeeping
        state.set("vacuum_at", (state.clock() - timedelta(days=VACUUM_DAYS - 1)).isoformat(timespec="seconds"))
        state.log_error("housekeeping", f"database compaction postponed: {exc}")
        return 0
    state.set("vacuum_at", state.now())
    return max(0, size - db.stat().st_size)


def prune_logs(state: Any, cfg: Any) -> dict[str, int]:
    from tools import contact_policy as contact
    from tools import download_links

    now = state.clock()

    def before(days: float) -> str:
        return (now - timedelta(days=days)).isoformat(timespec="seconds")

    contact.ensure(state)
    keep = int(cfg.log_keep_days)
    return {
        "actions": state._exec("DELETE FROM actions WHERE created_at < ?", (before(keep),)).rowcount,
        "errors": state._exec("DELETE FROM errors WHERE created_at < ?", (before(2 * keep),)).rowcount,
        "webhook events": state._exec("DELETE FROM webhook_events WHERE received_at < ? AND status != 'processing'",
                                      (before(WEBHOOK_KEEP_DAYS),)).rowcount,
        "contact log": state._exec("DELETE FROM contact_log WHERE sent_at < ?", (before(CONTACT_KEEP_DAYS),)).rowcount,
        "download links": download_links.prune(state),
        "old job postings": state._exec("DELETE FROM leads WHERE last_seen < ?", (before(int(cfg.lead_keep_days)),)).rowcount,
        "unconfirmed sign-ups": _prune_unconfirmed(state, before(int(cfg.unconfirmed_signup_days))),
    }


def _prune_unconfirmed(state: Any, cutoff: str) -> int:
    ids = [r["id"] for r in state._all("SELECT id FROM subscribers WHERE tier = 'free' AND subscription_status = 'pending' "
                                       "AND started_at < ?", (cutoff,))]
    for sid in ids:
        state._exec("DELETE FROM subscription_deliveries WHERE subscriber_id = ?", (sid,))
        state._exec("DELETE FROM subscribers WHERE id = ?", (sid,))
    return len(ids)


def rotate_audit(cfg: Any, now: datetime) -> int:
    from tools.dispatcher import AUDIT_FILE

    data = Path(cfg.data_dir)
    path = data / AUDIT_FILE
    rotated = 0
    if path.exists() and path.stat().st_size > AUDIT_ROTATE_BYTES:
        target = data / f"{path.stem}-{now:%Y%m%d}{path.suffix}.gz"
        with open(path, "rb") as src, gzip.open(target, "ab") as dst:
            shutil.copyfileobj(src, dst)
        path.write_bytes(b"")
        target.chmod(0o600)
        rotated = 1
    cutoff = now - timedelta(days=ARCHIVE_KEEP_DAYS)
    for old in data.glob(f"{Path(AUDIT_FILE).stem}-*.gz"):
        try:
            if datetime.strptime(old.name.split("-")[-1][:8], "%Y%m%d").replace(tzinfo=now.tzinfo) < cutoff:
                old.unlink()
        except ValueError:
            continue
    return rotated


def prune_versions(state: Any, files: Any, keep: int) -> list[str]:
    """Delete files of old dataset versions nobody bought and nothing else uses. Returns removed paths."""
    from strategies.freshness_guard import niche_of

    assets = state.list_assets()  # newest first
    sold = {r["asset_id"] for r in state._all("SELECT DISTINCT asset_id FROM orders WHERE asset_id IS NOT NULL")}
    rank: dict[tuple[str, str], int] = {}
    protected: set[str] = set()
    candidates = []
    for a in assets:
        key = (niche_of(state, a), a["kind"])
        rank[key] = rank.get(key, 0) + 1
        if a["kind"] != "lead_directory" or rank[key] <= keep or a["id"] in sold:
            protected.add(str(a["path"]))
        else:
            candidates.append(a)
    removed = []
    for a in candidates:
        path = str(a["path"])
        if path in protected or not path or not files.exists(path):
            continue
        files.resolve(path).unlink()
        version_dir = f"assets/{niche_of(state, a)}/v{a['version']}"
        if files.exists(version_dir):
            shutil.rmtree(files.resolve(version_dir), ignore_errors=True)
        removed.append(path)
    return removed


class Housekeeping(Strategy):
    name = "housekeeping"
    tasks = ("housekeeping",)

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        tools, cfg, state = ctx.tools, ctx.tools.config, ctx.tools.state
        last = (state.get(KEY) or {}).get("at")
        if last and state.clock() - datetime.fromisoformat(last) < timedelta(hours=23):
            return TaskResult(True, f"housekeeping done {last[:16]}", {})
        try:
            pruned = prune_logs(state, cfg)
            rotated = rotate_audit(cfg, state.clock())
            removed = prune_versions(state, tools.files, int(cfg.keep_versions))
            from strategies.catalog_hygiene import prune_retired

            retired_dirs = prune_retired(state, tools.files)  # Phase 213
            state._exec("PRAGMA optimize")
            reclaimed = maybe_vacuum(state, Path(state.db_path))
            trimmed = trim_launchd_logs()
        except Exception as exc:  # noqa: BLE001 - tidying must never stop the agent
            state.log_error("housekeeping", f"housekeeping failed: {exc!r}")
            return TaskResult(True, f"housekeeping failed: {exc!r}"[:200], {})
        record = {"at": state.now(), "rows": pruned, "audit_rotated": rotated, "old_versions": removed,
                  "vacuum_reclaimed_bytes": reclaimed, "logs_trimmed": trimmed}
        state.set(KEY, record)
        parts = [f"{n} {k}" for k, n in pruned.items() if n] + ([f"{len(removed)} old version file(s)"] if removed else []) \
            + (["audit log archived"] if rotated else []) \
            + ([f"{retired_dirs} retired product folder(s)"] if retired_dirs else []) \
            + ([f"database compacted ({reclaimed / 1e6:.1f} MB freed)"] if reclaimed >= 100_000 else [])
        return TaskResult(True, "housekeeping: " + (", ".join(parts) or "nothing to tidy"), {"removed": len(removed), **pruned})
