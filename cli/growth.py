"""Owner commands: `share`, `pace`, `todo`, `offers`, `books`, `heartbeat`, `privacy`, `phone`, `quiet`, `commands`."""

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


def offers_main(argv: list[str] | None = None) -> int:
    from strategies.offer_tuner import report

    config, state, _ = _setup()
    say(BOLD, "Offer      discount  emails  sales  rate    since")
    for r in report(state, config):
        rate = f"{r['rate'] * 100:.1f}%" if r["rate"] is not None else "-"
        print(f"{r['kind']:<10} {r['pct']:>6}%  {r['sent']:>6}  {r['sold']:>5}  {rate:<6}  {r['since'][:10]}")
    return 0


def customers_main(argv: list[str]) -> int:
    from tools.customers import describe, export_csv, report

    _, state, files = _setup()
    if "--report" in argv:
        r = report(state)
        say(BOLD, describe(r))
        for ch, v in r["by_channel"].items():
            print(f"  first came from {ch:<12} {v['customers']:>4} customer(s), lifetime value ${v['ltv_cents'] / 100:,.2f}")
        for month, v in r["by_month"].items():
            print(f"  first bought in {month}  {v['customers']:>4} customer(s), lifetime value ${v['ltv_cents'] / 100:,.2f}")
        return 0
    path, n = export_csv(state, files)
    say(GREEN, f"✔ {n} customer(s) written to {path}")
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


def privacy_main(argv: list[str], confirm=input) -> int:
    """`automonetize privacy export EMAIL` | `automonetize privacy forget EMAIL [--yes]`."""
    import json

    from tools.privacy import export, forget, placeholder

    if len(argv) < 2 or argv[0] not in ("export", "forget") or "@" not in argv[1]:
        say(RED, "Use: automonetize privacy export someone@example.com  (or: forget someone@example.com)")
        return 1
    config, state, files = _setup()
    email = argv[1].strip().lower()
    if argv[0] == "export":
        data = export(state, config, email)
        path = files.write_text(f"exports/privacy/{placeholder(email)[8:20]}.json", json.dumps(data, indent=2, default=str))
        say(GREEN, f"✔ Everything stored about {email}: {path}")
        print("  Send that file to them if they asked for a copy.")
        return 0
    say(BOLD, f"This erases {email} from the agent's data (sales records stay, anonymised). It can't be undone.")
    if "--yes" not in argv and confirm("Type FORGET to continue: ").strip() != "FORGET":
        say(YELLOW, "Cancelled. Nothing was changed.")
        return 1
    done = forget(state, config, email)
    say(GREEN, f"✔ Erased {email}. They will never be emailed again.")
    for what, n in done.items():
        print(f"  {what}: {n}")
    print("  Backups still hold the old data until they age out (14 days).")
    return 0


def phone_main(argv: list[str] | None = None, transport=None) -> int:
    """`automonetize phone`: phone notifications for sales and alerts (ntfy, free, no account)."""
    from agent.setup_autonomous import load_env_into
    from agent.tunnel import update_env_file
    from cli.go_live import restart
    from tools import build_toolkit
    from tools.circuit_breaker import CircuitBreaker
    from tools.notify import enabled, new_topic, push

    config, state, _ = _setup()
    if not enabled(config):
        config.ntfy_topic = new_topic()
        update_env_file(ROOT / ".env", {"NTFY_TOPIC": config.ntfy_topic})
    tools = build_toolkit(config, state, CircuitBreaker(100, 100, 100), transport=transport, sleep=lambda s: None)
    ok = push(tools.http, config, "AutoMonetize is connected", "You'll get a buzz here for every sale and anything that needs you.",
              tags="tada")
    say(BOLD, "Phone notifications, 2 minutes:")
    print("  1. Install the free ntfy app (App Store or Google Play).")
    print(f"  2. In the app: + (subscribe) → topic name: {config.ntfy_topic}")
    print("     Keep this name private: anyone with it can read your notifications.")
    if ok:
        say(GREEN, "✔ A test notification was sent: it appears in the app once you've subscribed.")
    else:
        say(YELLOW, "Couldn't reach ntfy.sh right now; the agent will keep trying for each sale.")
    restart(load_env_into(ROOT / ".env"))
    return 0


def quiet_main(argv: list[str]) -> int:
    """`automonetize quiet on|off`: hold or release all marketing email."""
    from tools.contact_policy import quiet, set_quiet

    _, state, _ = _setup()
    if argv[:1] == ["on"]:
        set_quiet(state, True, "turned on from the terminal")
        say(GREEN, "✔ Quiet mode on: no marketing email goes out. Purchases and support replies still do.")
    elif argv[:1] == ["off"]:
        set_quiet(state, False)
        say(GREEN, "✔ Quiet mode off: marketing email goes out again, within the usual limits.")
    else:
        q = quiet(state)
        print(f"Quiet mode is {'on since ' + q['since'][:16] if q else 'off'}. Use: automonetize quiet on | off")
    return 0


def commands_main(argv: list[str]) -> int:
    """`automonetize commands [--new]`: the email command code."""
    from agent.owner_commands import code, help_text

    config, state, _ = _setup()
    code(state, new="--new" in argv)
    print(help_text(config, state))
    return 0
