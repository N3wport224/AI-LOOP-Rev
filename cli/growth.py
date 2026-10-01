"""`automonetize share` (this week's ready-to-paste posts) and `automonetize books [YYYY-MM]`."""

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
