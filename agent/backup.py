"""Nightly backups and one-command restore.

* **What:** the agent database (``agent_state.db``: orders, revenue, customers, API keys' hashes,
  leads), the evolution log, ``.env`` (your keys, kept mode 600) and ``automonetize.toml``.
* **Where:** outside the project folder, so deleting or re-cloning the folder doesn't take the
  backups with it: ``~/Library/Application Support/AutoMonetize/backups`` on macOS
  (``~/.local/share/automonetize/backups`` elsewhere), or ``backup_dir``.
* **When:** once a day by the ``backup_data`` task (part of the agent, not of ``strategies/``), and on
  demand with ``automonetize backup``. Kept for ``backup_keep_days`` (14) days.
* **How:** SQLite's online backup API, so a copy taken while the agent runs is consistent.
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
        try:
            path = make_backup(cfg, self.workdir / ".env", self.workdir / "automonetize.toml", now)
            removed = prune(cfg, now)
        except Exception as exc:  # noqa: BLE001 - a full disk must not stop the agent; it alerts instead
            state.log_error("backup", f"daily backup failed: {exc!r}", kind="alert")
            return TaskResult(True, f"backup failed: {exc!r}"[:200], {})
        state.set("last_backup_at", now.isoformat(timespec="seconds"))
        return TaskResult(True, f"backup saved to {path}" + (f"; {removed} old backup(s) removed" if removed else ""), {"path": str(path)})
