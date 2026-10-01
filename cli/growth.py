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


def connections_main(argv: list[str]) -> int:
    from tools import build_toolkit
    from tools.circuit_breaker import CircuitBreaker
    from tools.connections import check_all, summary

    config, state, _ = _setup()
    conns = check_all(config, build_toolkit(config, state, CircuitBreaker(100, 100, 100)).http)
    marks = {"ok": (GREEN, "✔"), "fail": (RED, "✘"), "skip": (YELLOW, "-")}
    for c in conns:
        colour, mark = marks[c.status]
        say(colour, f"{mark} {c.name}: {c.detail}" + (f"  → {c.fix}" if c.status != "ok" and c.fix else ""))
    say(BOLD, summary(conns))
    return 1 if any(c.status == "fail" for c in conns) else 0


def expense_main(argv: list[str]) -> int:
    """`automonetize expense add AMOUNT "what" [--date YYYY-MM-DD] [--category c]` | `expense list [YYYY-MM]` | `expense delete ID`."""
    from strategies.bookkeeping import period_bounds, tz_of
    from tools import expenses

    config, state, _ = _setup()
    if argv[:1] == ["add"] and len(argv) >= 3:
        opts = dict(zip(argv[3::2], argv[4::2]))
        try:
            cents = expenses.parse_amount(argv[1])
            day = opts.get("--date") or state.clock().astimezone(tz_of(config)).date().isoformat()
            eid = expenses.add(state, cents, argv[2], day, opts.get("--category", "general"))
        except ValueError as exc:
            say(RED, f"Not recorded: {exc}")
            return 1
        say(GREEN, f"✔ Expense #{eid}: ${cents / 100:,.2f} on {day} ({argv[2]})")
        return 0
    if argv[:1] == ["delete"] and len(argv) == 2 and argv[1].isdigit():
        ok = expenses.delete(state, int(argv[1]))
        say(GREEN if ok else RED, f"✔ Expense #{argv[1]} deleted" if ok else f"No expense #{argv[1]}")
        return 0 if ok else 1
    if argv[:1] == ["list"]:
        period = argv[1] if len(argv) > 1 else state.clock().astimezone(tz_of(config)).strftime("%Y-%m")
        start, end = period_bounds(period, tz_of(config))
        rows = expenses.between(state, start.date().isoformat(), end.date().isoformat())
        for e in rows:
            print(f"  #{e['id']:<4} {e['spent_on']}  ${e['amount_cents'] / 100:>9,.2f}  {e['category']:<10} {e['description']}")
        say(BOLD, f"{len(rows)} expense(s) in {period}: ${sum(e['amount_cents'] for e in rows) / 100:,.2f}")
        return 0
    say(RED, 'Use: automonetize expense add 12.00 "domain renewal" [--date 2026-09-03] [--category tools] | list [YYYY-MM] | delete ID')
    return 1


def goal_main(argv: list[str]) -> int:
    """`automonetize goal DOLLARS`: change the daily net revenue goal."""
    from agent.setup_autonomous import load_env_into
    from agent.tunnel import update_env_file
    from cli.go_live import restart

    config, state, _ = _setup()
    if not argv or not re.fullmatch(r"\d{1,6}(\.\d{1,2})?", argv[0]):
        print(f"The daily goal is ${config.daily_target_cents / 100:,.2f}. Change it with: automonetize goal 15")
        return 0 if not argv else 1
    cents = round(float(argv[0]) * 100)
    update_env_file(ROOT / ".env", {"AUTOMONETIZE_DAILY_TARGET_CENTS": str(cents)})
    state.set("goal_suggestion", None)
    say(GREEN, f"✔ Daily goal set to ${cents / 100:,.2f}")
    restart(load_env_into(ROOT / ".env"))
    return 0


def settings_log_main(argv: list[str]) -> int:
    from tools.settings_log import HISTORY

    _, state, _ = _setup()
    history = state.get(HISTORY) or []
    if not history:
        print("No settings changes recorded yet (they're compared at each start).")
    for h in history[-20:]:
        say(BOLD, h["at"][:16])
        for c in h["changes"]:
            print(f"  {c}")
    return 0


def export_all_main(argv: list[str]) -> int:
    from datetime import datetime, timezone

    from tools.export_all import export_all

    config, state, _ = _setup()
    path, counts = export_all(state, config.data_dir, datetime.now(timezone.utc))
    say(GREEN, f"✔ Exported {sum(counts.values())} rows from {len(counts)} tables to {path}")
    return 0


def forecast_main(argv: list[str]) -> int:
    """`automonetize forecast` (Phase 125)."""
    from strategies.money_insight import describe_forecast, forecast

    config, state, _ = _setup()
    print(describe_forecast(forecast(state, config)))
    return 0


def price_history_main(argv: list[str]) -> int:
    """`automonetize price-history` (Phase 126)."""
    from strategies.money_insight import money, price_history

    _, state, _ = _setup()
    rows = price_history(state)
    if not rows:
        print("No price changes recorded yet.")
    for r in rows:
        print(f"{r['at'][:16].replace('T', ' ')}  {r['title'] or 'asset ' + str(r['asset_id'])}: "
              f"{money(r['old_cents'])} → {money(r['new_cents'])}")
    return 0


def deps_main(argv: list[str]) -> int:
    """`automonetize deps` (Phase 115)."""
    from tools.deps import FIX, check, describe, requirements

    problems = check()
    if problems:
        say(YELLOW, f"Out of date: {describe(problems)}")
        print(f"Fix: {FIX}")
        return 1
    say(GREEN, f"✔ All {len(requirements())} requirements are installed and new enough.")
    return 0


def config_docs_main(argv: list[str]) -> int:
    """`automonetize config-docs [--markdown]` (Phase 116)."""
    from tools.config_docs import as_markdown, as_text, settings

    print(as_markdown(settings()) if "--markdown" in argv else as_text(settings()))
    return 0


def timings_main(argv: list[str]) -> int:
    """`automonetize timings [--days N]` (Phase 113)."""
    from tools.timings import table, timings

    days = int(argv[argv.index("--days") + 1]) if "--days" in argv else 7
    _, state, _ = _setup()
    rows = timings(state, days)
    print(table(rows) if rows else f"No task runs recorded in the last {days} days.")
    return 0


def features_main(argv: list[str]) -> int:
    from tools.features import overview

    config, state, _ = _setup()
    marks = {"on": (GREEN, "✔ on     "), "off": (YELLOW, "- off    "), "waiting": (YELLOW, "… waiting")}
    group = ""
    for f in overview(config, state):
        if f["group"] != group:
            group = f["group"]
            say(BOLD, f"\n{group}")
        colour, mark = marks[f["status"]]
        say(colour, f"  {mark}  {f['name']}" + (f"  ({f['detail']})" if f["detail"] else ""))
    return 0


def books_main(argv: list[str] | None = None) -> int:
    from strategies.bookkeeping import previous_month, save_books, tz_of

    argv = sys.argv[1:] if argv is None else argv
    config, state, files = _setup()
    month = argv[0] if argv else previous_month(state.clock().astimezone(tz_of(config)))
    if not re.fullmatch(r"20\d\d(-(0[1-9]|1[0-2]))?", month):
        say(RED, "Give a month (YYYY-MM) or a year (YYYY), e.g. automonetize books 2026-09")
        return 1
    from strategies.bookkeeping import summary_lines

    path, _, totals = save_books(state, config, files, month)
    say(GREEN, f"✔ {month}: {totals['rows']} revenue entries")
    for line in summary_lines(totals, config):
        print(f"  {line}")
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
    """`automonetize quiet on [--days N]|off`: hold or release all marketing email (Phase 112: for N days)."""
    from tools.contact_policy import quiet, set_quiet

    days = None
    if "--days" in argv:
        i = argv.index("--days")
        try:
            days = float(argv[i + 1])
            if not 0 < days <= 365:
                raise ValueError
        except (IndexError, ValueError):
            say(RED, "--days needs a number of days between 1 and 365, e.g. automonetize quiet on --days 7")
            return 2
        argv = argv[:i] + argv[i + 2:]
    _, state, _ = _setup()
    if argv[:1] == ["on"]:
        set_quiet(state, True, "turned on from the terminal" + (f" for {days:g} day(s)" if days else ""), days=days)
        q = quiet(state) or {}
        until = f" until {q['until'][:16].replace('T', ' ')} UTC" if q.get("until") else ""
        say(GREEN, f"✔ Quiet mode on{until}: no marketing email goes out. Purchases and support replies still do.")
    elif argv[:1] == ["off"]:
        set_quiet(state, False)
        say(GREEN, "✔ Quiet mode off: marketing email goes out again, within the usual limits.")
    else:
        q = quiet(state)
        until = f" until {q['until'][:16].replace('T', ' ')} UTC" if q and q.get("until") else ""
        print(f"Quiet mode is {'on since ' + q['since'][:16] + until if q else 'off'}. "
              "Use: automonetize quiet on [--days N] | off")
    return 0


def commands_main(argv: list[str]) -> int:
    """`automonetize commands [--new]`: the email command code."""
    from agent.owner_commands import code, help_text

    config, state, _ = _setup()
    code(state, new="--new" in argv)
    print(help_text(config, state))
    return 0
