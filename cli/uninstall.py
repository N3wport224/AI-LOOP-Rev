"""`automonetize uninstall` (Phase 69): stop the agent cleanly and remove what it installed.

By default it only:

1. takes a final backup ("pre-uninstall");
2. stops the agent (and the GUI is left to you);
3. removes the launchd jobs on macOS (``deploy/install_launchd.sh uninstall``), so nothing restarts
   at login.

Your data, backups, ``.env`` and the code stay where they are, and ``automonetize autostart``
brings it all back. Options:

* ``--deactivate-links``: also switch off every live Stripe Payment Link, so nobody can buy what the
  agent will no longer deliver. (Recommended if you're stopping for good.)
* ``--delete-data``: also delete the data folder and the backups (asks you to type DELETE). Stripe
  keeps its own records of every payment either way.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from cli.go_live import BOLD, GREEN, RED, ROOT, YELLOW, say


def main(argv: list[str], confirm: Callable[[str], str] = input, run: Callable[..., Any] = subprocess.run,
         system: str | None = None) -> int:
    from agent.backup import backup_root, make_backup
    from agent.setup_autonomous import load_env_into
    from cli.doctor import controller
    from tools.catalog import live_products

    config, state, ctl = controller(load_env_into(ROOT / ".env"))
    delete = "--delete-data" in argv
    say(BOLD, "Uninstalling the agent's background service." + (" Data will be DELETED." if delete else " Your data stays."))
    if "--yes" not in argv and confirm("Type UNINSTALL to continue: ").strip() != "UNINSTALL":
        say(YELLOW, "Cancelled. Nothing was changed.")
        return 1
    try:
        path = make_backup(config, ROOT / ".env", ROOT / "automonetize.toml", datetime.now(timezone.utc), label="pre-uninstall")
        say(GREEN, f"✔ Final backup: {path}")
    except Exception as exc:  # noqa: BLE001
        say(YELLOW, f"! Couldn't take a final backup ({exc}); continuing.")
    if ctl.supervisor_pid() or ctl.launchd_managed():
        ctl.kill()
        for _ in range(60):
            if not ctl.supervisor_pid():
                break
            time.sleep(0.5)
        say(GREEN, "✔ Agent stopped")
    if (system or platform.system()) == "Darwin":
        proc = run(["/bin/bash", str(ROOT / "deploy" / "install_launchd.sh"), "uninstall"], capture_output=True, text=True)
        say(GREEN if proc.returncode == 0 else RED, ("✔ " if proc.returncode == 0 else "✘ ") + "launchd jobs removed"
            + ("" if proc.returncode == 0 else f": {(proc.stderr or proc.stdout).strip()[:200]}"))
    if "--deactivate-links" in argv and config.stripe_secret_key:
        from tools import build_toolkit
        from tools.circuit_breaker import CircuitBreaker

        tools = build_toolkit(config, state, CircuitBreaker(1000, 1000, 1000))
        off = 0
        for p in live_products(state):
            ref = str((state.get_asset(p["id"]) or {}).get("product_ref") or "")
            if ref.startswith("plink_"):
                try:
                    tools.http.post(f"https://api.stripe.com/v1/payment_links/{ref}", check_robots=False, data={"active": "false"},
                                    headers={"Authorization": f"Bearer {config.stripe_secret_key}"})
                    state.update_asset(p["id"], status="retired")
                    off += 1
                except Exception as exc:  # noqa: BLE001
                    say(YELLOW, f"! couldn't deactivate {ref}: {exc}")
        say(GREEN, f"✔ {off} payment link(s) switched off in Stripe")
    if delete:
        if "--yes" not in argv and confirm("This deletes all agent data and backups. Type DELETE: ").strip() != "DELETE":
            say(YELLOW, "Data kept.")
        else:
            state.close()
            for folder in (Path(config.data_dir), backup_root(config)):
                shutil.rmtree(folder, ignore_errors=True)
            say(GREEN, "✔ Data and backups deleted")
    print("")
    print("What's left:")
    print(f"  code and settings: {ROOT} (delete the folder to remove them; .env holds your keys)")
    if not delete:
        print(f"  data: {config.data_dir}")
        print(f"  backups: {backup_root(config)}")
    print("  Stripe keeps every payment record. To start again: automonetize autostart")
    return 0
