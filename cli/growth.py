"""`automonetize share`, `pace`, `books [YYYY-MM]` and `heartbeat [URL]`."""

from __future__ import annotations

import os
import re
import sys

from cli.go_live import BOLD, GREEN, RED, ROOT, YELLOW, say


def _setup():
    from agent.setup_autonomous import load_env_into
    from cli.doctor import controller
    from tools.file_io import SandboxedFileIO

    os.chdir(ROOT)
    config, state, _ = controller(load_env_into(ROOT / ".env"))
    return config, state, SandboxedFileIO(config.data_dir)


def share_main(argv: list[str] | None = None) -> int:
    from strategies.share_kit import KEY, as_text, build_kit

    _, state, files = _setup()
    kit = build_kit(state, files)  # always fresh here: it only reads the database
    state.set(KEY, kit)
    if not kit["posts"]:
        say(YELLOW, as_text(kit))
        return 0
    say(BOLD, "Copy one of these and post it from your own account. Each link tracks which channel sold.\n")
    print(as_text(kit))
    return 0


def pace_main(argv: list[str] | None = None) -> int:
    from strategies.goal_pacing import KEY, compute_pace, describe

    config, state, _ = _setup()
    pace = compute_pace(state, config)
    state.set(KEY, pace)
    say(GREEN if (pace.get("progress") or 0) >= 1 else YELLOW, describe(pace))
    return 0


def todo_main(argv: list[str] | None = None) -> int:
    from strategies.owner_todo import as_text, todo

    config, state, _ = _setup()
    items = todo(state, config)
    say(GREEN if not items else BOLD, "Only you can do these (most valuable first):" if items else as_text(items))
    if items:
        print(as_text(items))
    return 0


def books_main(argv: list[str] | None = None) -> int:
    from strategies.bookkeeping import previous_month, save_books, tz_of

    argv = sys.argv[1:] if argv is None else argv
    config, state, files = _setup()
    month = argv[0] if argv else previous_month(state.clock().astimezone(tz_of(config)))
    if not re.fullmatch(r"20\d\d-(0[1-9]|1[0-2])", month):
        say(RED, "Give the month as YYYY-MM, e.g. automonetize books 2026-09")
        return 1
    path, _, totals = save_books(state, config, files, month)
    say(GREEN, f"✔ {month}: {totals['rows']} entries · gross ${totals['gross_cents'] / 100:,.2f} · "
               f"fees ${totals['fee_cents'] / 100:,.2f} · net ${totals['net_cents'] / 100:,.2f}")
    print(f"  Saved to {path}")
    return 0


def heartbeat_main(argv: list[str] | None = None, transport=None) -> int:
    """`automonetize heartbeat [URL]`: test a ping URL, save it to .env and restart the agent."""
    from agent.setup_autonomous import load_env_into
    from agent.tunnel import update_env_file
    from cli.go_live import restart
    from tools import build_toolkit
    from tools.circuit_breaker import CircuitBreaker

    argv = sys.argv[1:] if argv is None else argv
    config, state, _ = _setup()
    if not argv:
        if config.heartbeat_url:
            say(GREEN, f"Heartbeat is on: {config.heartbeat_url}")
        else:
            say(YELLOW, "No heartbeat yet. It emails you if the agent stops (Mac off, asleep or offline). One-time setup:")
            print("  1. Go to https://healthchecks.io and sign up (free).")
            print("  2. Add a check: Period 1 hour, Grace 1 hour. Copy its ping URL.")
            print("  3. Run: automonetize heartbeat https://hc-ping.com/YOUR-ID")
        return 0
    url = argv[0].strip()
    if not url.startswith("https://") or " " in url:
        say(RED, "That doesn't look like a ping URL (it starts with https://).")
        return 1
    try:
        tools = build_toolkit(config, state, CircuitBreaker(100, 100, 100), transport=transport, sleep=lambda s: None)
        tools.http.get(url, check_robots=False, attempts=2)
    except Exception as exc:  # noqa: BLE001
        say(RED, f"The URL didn't answer ({exc}). Check you copied the whole ping URL.")
        return 1
    update_env_file(ROOT / ".env", {"HEALTHCHECK_URL": url})
    say(GREEN, "✔ Ping received and saved. You'll get an email from healthchecks.io if the agent goes quiet.")
    restart(load_env_into(ROOT / ".env"))
    return 0
