"""Nightly backups and one-command restore.

* **What:** the agent database (``agent_state.db``: orders, revenue, customers, API keys' hashes,
  leads), the evolution log, ``.env`` (your keys, kept mode 600) and ``automonetize.toml``.
* **Where:** outside the project folder, so deleting or re-cloning the folder doesn't take the
  backups with it: ``~/Library/Application Support/AutoMonetize/backups`` on macOS
  (``~/.local/share/automonetize/backups`` elsewhere), or ``backup_dir``.
* **When:** once a day by the ``backup_data`` task (part of the agent, not of ``strategies/``), and on
  demand with ``automonetize backup``. Kept for ``backup_keep_days`` (14) days.
* **How:** SQLite's online backup API, so a copy taken while the agent runs is consistent.
* **Checked:** before the daily backup, ``PRAGMA quick_check`` runs on each database. A failed check
  raises an alert with the restore command and keeps every old backup (nothing is pruned).
* **Restore-tested** (Phase 133): once a week the newest backup is restored into a temporary
  folder and opened (``verify_backup``); a backup that doesn't open raises an alert.
* **Before risky changes:** self-update and self-evolution take a "pre-update" / "pre-evolution"
  backup first (``safety_backup``).
* **Restore:** ``automonetize restore <name>`` stops the agent, keeps a "pre-restore" backup of the
  current state, copies the backup back and starts the agent again.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from strategies.base import Strategy, TaskContext, TaskResult

DBS = ("agent_state.db", "evolution_log.db")


def backup_root(config: Any) -> Path:
    if getattr(config, "backup_dir", ""):
        return Path(config.backup_dir).expanduser()
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "AutoMonetize" / "backups"
    return Path.home() / ".local" / "share" / "automonetize" / "backups"


def _copy_db(src: Path, dst: Path) -> None:
    with sqlite3.connect(src) as source, sqlite3.connect(dst) as target:
        source.backup(target)
    source.close()
    target.close()


def integrity(path: Path) -> str:
    """SQLite's quick_check: "ok", or what's wrong."""
    if not Path(path).exists():
        return "ok"
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = [r[0] for r in conn.execute("PRAGMA quick_check").fetchall()]
    except sqlite3.DatabaseError as exc:  # too damaged to even check
        return str(exc)
    finally:
        conn.close()
    return "ok" if rows == ["ok"] else "; ".join(map(str, rows[:5]))


def safety_backup(config: Any, root: Path, now: datetime, label: str) -> Path | None:
    """A backup before a risky change (self-update, self-evolution). Never raises: returns None on failure."""
    try:
        return make_backup(config, root / ".env", root / "automonetize.toml", now, label=label)
    except Exception:  # noqa: BLE001 - the change is still verified and reversible by git; a backup is extra safety
        return None


def make_backup(config: Any, env_file: Path | None, toml_file: Path | None, now: datetime, label: str = "") -> Path:
    root = backup_root(config)
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    name = now.strftime("%Y%m%d-%H%M%S") + (f"-{label}" if label else "")
    target = root / name
    target.mkdir(mode=0o700)
    files = []
    for db in DBS:
        src = Path(config.data_dir) / db
        if src.exists():
            _copy_db(src, target / db)
            files.append(db)
    for src in (env_file, toml_file):
        if src and Path(src).exists():
            dst = target / Path(src).name
            shutil.copy2(src, dst)
            os.chmod(dst, 0o600)
            files.append(Path(src).name)
    (target / "manifest.json").write_text(json.dumps({"at": now.isoformat(timespec="seconds"), "files": files,
                                                      "data_dir": str(Path(config.data_dir).resolve())}, indent=2))
    return target


def list_backups(config: Any) -> list[dict[str, Any]]:
    root = backup_root(config)
    out = []
    for d in sorted(root.glob("*"), reverse=True) if root.exists() else []:
        try:
            manifest = json.loads((d / "manifest.json").read_text())
        except (OSError, ValueError):
            continue
        size = sum(f.stat().st_size for f in d.iterdir() if f.is_file())
        out.append({"name": d.name, "at": manifest.get("at"), "files": manifest.get("files", []), "bytes": size})
    return out


def prune(config: Any, now: datetime) -> int:
    root = backup_root(config)
    cutoff = now - timedelta(days=int(config.backup_keep_days))
    removed = 0
    backups = list_backups(config)
    newest = backups[0]["name"] if backups else None
    for b in backups:
        try:
            when = datetime.fromisoformat(b["at"])
        except (TypeError, ValueError):
            continue
        if when.tzinfo is None and cutoff.tzinfo is not None:
            when = when.replace(tzinfo=cutoff.tzinfo)
        if when < cutoff and b["name"] != newest:  # always keep the newest
            shutil.rmtree(root / b["name"], ignore_errors=True)
            removed += 1
    return removed


def restore_files(config: Any, name: str, env_file: Path, toml_file: Path | None) -> list[str]:
    """Copy a backup back in place. The agent must be stopped (the CLI takes care of it)."""
    src = backup_root(config) / name
    if not (src / "manifest.json").exists() or "/" in name or name.startswith("."):
        raise FileNotFoundError(f"no backup named {name!r}")
    restored = []
    for db in DBS:
        if (src / db).exists():
            # Through SQLite's backup API, never a file copy: it stays consistent with the WAL even
            # if a connection is still open.
            _copy_db(src / db, Path(config.data_dir) / db)
            restored.append(db)
    if (src / env_file.name).exists():
        shutil.copy2(src / env_file.name, env_file)
        os.chmod(env_file, 0o600)
        restored.append(env_file.name)
    if toml_file and (src / toml_file.name).exists():
        shutil.copy2(src / toml_file.name, toml_file)
        restored.append(toml_file.name)
    return restored


VERIFY_DAYS = 7


def verify_backup(config: Any, name: str | None = None) -> dict[str, Any]:
    """Phase 133: restore the newest backup into a temporary folder and check it really opens: the
    database passes SQLite's check and its tables can be read. A backup nobody ever restored is a hope,
    not a backup. Never touches the live data."""
    import tempfile

    backups = list_backups(config)
    if not backups:
        return {"ok": False, "name": "", "detail": "no backup yet"}
    name = name or backups[0]["name"]
    src = backup_root(config) / name
    with tempfile.TemporaryDirectory() as tmp:
        counts: dict[str, int] = {}
        for db in DBS:
            if not (src / db).exists():
                continue
            copy = Path(tmp) / db
            shutil.copy2(src / db, copy)
            check = integrity(copy)
            if check != "ok":
                return {"ok": False, "name": name, "detail": f"{db}: {check}"}
            conn = sqlite3.connect(copy)
            try:
                tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                                     "AND name NOT LIKE 'sqlite_%'")]
                for t in tables:
                    counts[f"{db}:{t}"] = int(conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0])
            except sqlite3.DatabaseError as exc:
                return {"ok": False, "name": name, "detail": f"{db}: {exc}"}
            finally:
                conn.close()
        if not counts:
            return {"ok": False, "name": name, "detail": "the backup holds no database"}
        orders = counts.get("agent_state.db:orders")
    return {"ok": True, "name": name, "detail": f"{len(counts)} tables readable" + (f", {orders} orders" if orders is not None
                                                                                   else "")}


class Backups(Strategy):
    name = "backups"
    tasks = ("backup_data",)

    def __init__(self, workdir: Path | None = None):
        self.workdir = workdir or Path(__file__).resolve().parents[1]

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        cfg, state = ctx.tools.config, ctx.tools.state
        if not cfg.backups_enabled:
            return TaskResult(True, "backups off", {})
        now = state.clock()
        last = state.get("last_backup_at")
        if last and now - datetime.fromisoformat(last) < timedelta(hours=23):
            return TaskResult(True, f"last backup {last[:16]}", {})
        problems = {db: integrity(Path(cfg.data_dir) / db) for db in DBS}
        broken = {db: why for db, why in problems.items() if why != "ok"}
        if broken:
            # Don't prune: the older backups may be the only healthy copies left.
            newest = (list_backups(cfg) or [{}])[0].get("name", "")
            state.log_error("backup", f"database check failed ({broken}). Restore the last good copy with "
                                      f"`automonetize restore {newest or '<name>'}` (`automonetize backup list` shows them).",
                            kind="alert")
            state.set("last_backup_at", now.isoformat(timespec="seconds"))  # alert once a day, not every cycle
            return TaskResult(True, f"database check failed: {broken}"[:200], {"integrity": "failed"})
        try:
            path = make_backup(cfg, self.workdir / ".env", self.workdir / "automonetize.toml", now)
            removed = prune(cfg, now)
        except Exception as exc:  # noqa: BLE001 - a full disk must not stop the agent; it alerts instead
            state.log_error("backup", f"daily backup failed: {exc!r}", kind="alert")
            return TaskResult(True, f"backup failed: {exc!r}"[:200], {})
        state.set("last_backup_at", now.isoformat(timespec="seconds"))
        checked = state.get("backup_verified") or {}
        if not checked.get("at") or now - datetime.fromisoformat(checked["at"]) >= timedelta(days=VERIFY_DAYS):
            try:
                result = verify_backup(cfg, path.name)
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "name": path.name, "detail": repr(exc)}
            state.set("backup_verified", {**result, "at": now.isoformat(timespec="seconds")})
            if not result["ok"]:
                state.log_error("backup", f"the weekly restore test of backup {result['name']} failed: {result['detail']}. "
                                          "Check the disk (Disk Utility → First Aid).", kind="alert")
        return TaskResult(True, f"backup saved to {path}" + (f"; {removed} old backup(s) removed" if removed else ""), {"path": str(path)})
