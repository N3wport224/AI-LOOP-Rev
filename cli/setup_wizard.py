"""`automonetize setup` (Phase 70): one guided command for everything that's still missing.

It looks at what's already done and only asks about the rest, in the order that matters most:

1. Python version (3.11+).
2. Real payments: runs ``go-live`` if you're still in test mode or dry run.
3. Your postal address (required in every customer email) and the email address for reports.
4. Marketing: runs ``connect-marketing`` (public site and articles).
5. Phone notifications (``phone``) and the heartbeat (``heartbeat``), both optional.
6. Start at login (macOS launchd) so a reboot doesn't stop it.
7. A connections check and what's left on the to-do list.

Every step can be skipped with Enter and the whole thing can be run again at any time: finished
steps are just ticked off.
"""

from __future__ import annotations

import platform
import re
import sys
from typing import Callable

from cli.go_live import BOLD, GREEN, ROOT, YELLOW, say

Ask = Callable[[str], str]


def yes(ask: Ask, question: str) -> bool:
    return ask(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def main(argv: list[str] | None = None, ask: Ask = input, steps: dict[str, Callable[[], int]] | None = None,
         system: str | None = None) -> int:
    from agent.setup_autonomous import load_env_into
    from agent.tunnel import update_env_file
    from cli.doctor import controller

    steps = steps or {}
    env_file = ROOT / ".env"
    say(BOLD, "AutoMonetize setup: only what's missing, most important first. Press Enter to skip any step.\n")

    # 1. Python
    if sys.version_info < (3, 11):
        say(YELLOW, f"! Python {platform.python_version()}: 3.11 or newer is needed (brew install python@3.12).")
        return 1
    say(GREEN, f"✔ Python {platform.python_version()}")

    def cfg():
        return controller(load_env_into(env_file))

    config, state, ctl = cfg()

    # 2. Real payments
    live = str(config.stripe_secret_key or "").startswith(("sk_live_", "rk_live_")) and not config.dry_run
    if live:
        say(GREEN, "✔ Real payments are on")
    elif yes(ask, "Payments are in test mode. Switch to real payments now (needs your live Stripe key)?"):
        from cli.go_live import main as go_live

        (steps.get("go_live") or go_live)()
        config, state, ctl = cfg()

    # 3. Postal address and report email
    if str(config.sender_postal_address or "").strip():
        say(GREEN, f"✔ Postal address: {config.sender_postal_address}")
    else:
        print("Customer emails must include a postal address (US CAN-SPAM law). A PO box or a virtual mailbox works.")
        for _ in range(3):
            address = ask("Postal address (street, city, postcode, country), or Enter to skip: ").strip()
            if not address:
                break
            if len(address) >= 10 and re.search(r"\d", address):
                update_env_file(env_file, {"CAN_SPAM_POSTAL_ADDRESS": address})
                say(GREEN, "✔ Postal address saved")
                break
            say(YELLOW, "That doesn't look like a full address (it needs a street number and a city).")
    if config.owner_email:
        say(GREEN, f"✔ Reports go to {config.owner_email}")
    else:
        email = ask(f"Where should reports and alerts go? [Enter = {config.sender_email or 'skip'}] ").strip()
        if email and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            update_env_file(env_file, {"OWNER_EMAIL": email})
            say(GREEN, f"✔ Reports go to {email}")

    # 4. Marketing
    if config.github_pages_repo and config.pages_base_url:
        say(GREEN, f"✔ Public site: {config.pages_base_url}")
    elif yes(ask, "Put your products on a free public website and turn on articles (needs a Dev.to key and a GitHub token)?"):
        from cli.connect_marketing import main as connect

        (steps.get("connect_marketing") or connect)()

    # 5. Phone and heartbeat
    from tools.notify import enabled

    if enabled(config):
        say(GREEN, "✔ Phone notifications are on")
    elif yes(ask, "Get a phone notification for every sale and alert (free ntfy app, no account)?"):
        from cli.growth import phone_main

        (steps.get("phone") or phone_main)()
    if config.heartbeat_url:
        say(GREEN, "✔ Heartbeat is on")
    else:
        url = ask("Heartbeat: paste a healthchecks.io ping URL to be emailed if the Mac stops (Enter to skip): ").strip()
        if url:
            from cli.growth import heartbeat_main

            (steps.get("heartbeat") or (lambda: heartbeat_main([url])))()

    # 6. Start at login
    config, state, ctl = cfg()
    if (system or platform.system()) == "Darwin":
        if ctl.launchd_managed():
            say(GREEN, "✔ Starts by itself after a reboot")
        elif yes(ask, "Start the agent automatically at login, and restart it if it stops?"):
            from cli.doctor import Doctor

            say(GREEN, "✔ " + ((steps.get("autostart") or Doctor(config, state, ctl).enable_autostart)() or "autostart on"))
        print("  Keep the Mac awake: sudo pmset -a sleep 0 disksleep 0 autorestart 1")

    # 7. Connections and what's left
    from strategies.owner_todo import as_text, todo
    from tools import build_toolkit
    from tools.circuit_breaker import CircuitBreaker
    from tools.connections import check_all, summary

    config, state, ctl = cfg()
    conns = (steps.get("connections") or (lambda: check_all(config, build_toolkit(config, state, CircuitBreaker(100, 100, 100)).http)))()
    say(BOLD, "\nConnections: " + summary(conns))
    for c in conns:
        if c.status == "fail":
            print(f"  ✘ {c.name}: {c.detail} → {c.fix}")
    items = todo(state, config)
    if items:
        say(BOLD, "\nStill to do (most valuable first):")
        print(as_text(items))
    else:
        say(GREEN, "\nAll set. The agent runs by itself from here.")
    return 0
