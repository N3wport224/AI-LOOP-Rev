"""`automonetize backup [list]` and `automonetize restore NAME`."""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

from cli.go_live import BOLD, GREEN, RED, ROOT, YELLOW, say


def main(argv: list[str] | None = None, confirm=input) -> int:
    from agent.backup import list_backups, make_backup, restore_files
    from agent.setup_autonomous import load_env_into
    from cli.doctor import controller

    argv = sys.argv[1:] if argv is None else argv
    os.chdir(ROOT)
    config, state, ctl = controller(load_env_into(ROOT / ".env"))
    toml = ROOT / "automonetize.toml"
    if argv[:1] == ["list"]:
        rows = list_backups(config)
        if not rows:
            say(YELLOW, "No backups yet. Make one with: automonetize backup")
        for b in rows:
            print(f"  {b['name']}   {b['bytes'] / 1024:,.0f} KB   {', '.join(b['files'])}")
        return 0
    if argv[:1] == ["restore"]:
        if len(argv) < 2:
            say(RED, "Which one? See them with: automonetize backup list")
            return 1
        name = argv[1]
        if not any(b["name"] == name for b in list_backups(config)):
            say(RED, f"No backup named {name}. See them with: automonetize backup list")
            return 1
        say(BOLD, f"This replaces the agent's data with the backup {name} (your current data is backed up first).")
        if "--yes" not in argv and confirm("Type RESTORE to continue: ").strip() != "RESTORE":
            say(YELLOW, "Cancelled. Nothing was changed.")
            return 1
        was_running = bool(ctl.supervisor_pid() or ctl.launchd_managed())
        if was_running:
            ctl.kill()  # stops the supervisor (and the launchd job) so nothing writes during the copy
            for _ in range(120):
                if not ctl.supervisor_pid():
                    break
                time.sleep(0.5)
        safety = make_backup(config, ROOT / ".env", toml, datetime.now(timezone.utc), label="pre-restore")
        say(GREEN, f"✔ Current data saved first: {safety.name}")
        restored = restore_files(config, name, ROOT / ".env", toml)
        say(GREEN, f"✔ Restored: {', '.join(restored)}")
        if was_running:
            config, state, ctl = controller(load_env_into(ROOT / ".env"))
            say(GREEN, "✔ " + ctl.start().get("message", "agent started"))
        return 0
    path = make_backup(config, ROOT / ".env", toml, datetime.now(timezone.utc), label="manual")
    state.set("last_backup_at", state.now())
    say(GREEN, f"✔ Backup saved: {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
