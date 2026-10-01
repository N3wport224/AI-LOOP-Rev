"""Owner email commands (Phase 61): run the agent from your phone's mail app.

Send an email **from your owner address** (``owner_email``, or the sender address) **to the
sender address** with a subject like::

    AM STATUS 7F3A9C

The last word is your command code, shown at the bottom of every daily report; mail without it,
or from any other address, is ignored. Commands:

* ``STATUS``: today's sales, pace, what's on sale, anything wrong;
* ``TODO``: the things only you can do;
* ``PAUSE`` / ``RESUME``: stop and restart the agent's cycles (deliveries keep working);
* ``QUIET`` / ``LOUD``: hold all marketing email / let it go out again (purchases and support
  always go out);
* ``HELP``: this list.

The agent replies by email to confirm. Commands are read at the start of every cycle, even while
paused, so ``RESUME`` always works. The mailbox is opened read-only. Part of ``agent/``, so
self-evolution can't change it. ``automonetize commands`` shows the code; ``--new`` replaces it.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta
from typing import Any

CODE_KEY = "owner_command_code"
SEEN = "owner_commands_handled"
POLL_KEY = "owner_commands_polled_at"
POLL_MINUTES = 5
COMMANDS = ("status", "todo", "report", "drafts", "pause", "resume", "quiet", "loud", "help")
# "AM QUIET 7 CODE": an optional number of days (Phase 136) between the command and the code.
SUBJECT_RE = re.compile(r"^\s*(?:(?:re|fwd?)\s*:\s*)*am\s+([a-z]+)(?:\s+(\d{1,3}))?\s+([A-Z0-9]{6})\b", re.I)


def code(state: Any, new: bool = False) -> str:
    current = state.get(CODE_KEY)
    if new or not current:
        current = secrets.token_hex(3).upper()
        state.set(CODE_KEY, current)
    return current


def owner_address(cfg: Any) -> str:
    return str(cfg.owner_email or cfg.sender_email or "").strip().lower()


def help_text(cfg: Any, state: Any) -> str:
    return (f"Control the agent by email: write to {cfg.sender_email} from {owner_address(cfg)} with the subject "
            f"\"AM <COMMAND> {code(state)}\". Commands: STATUS, TODO, REPORT (today's full report), DRAFTS (posts to copy), "
            "PAUSE, RESUME, "
            f"QUIET (hold marketing email; \"AM QUIET 7 {code(state)}\" for 7 days), LOUD (let it go out again), HELP.")


def status_text(tools: Any) -> str:
    from strategies.goal_pacing import describe as pace
    from tools.catalog import live_products
    from tools.contact_policy import paused

    state = tools.state
    today = tools.revenue.daily_summary()
    lines = [f"Today: ${today['net_cents'] / 100:,.2f} net of your ${today['target_cents'] / 100:,.2f}/day goal",
             pace(state.get("goal_pace")),
             f"On sale: {len(live_products(state))} product(s)",
             "Engine: " + ("PAUSED" if state.get("paused") else "running")]
    hold = paused(state)
    if hold:
        lines.append(f"Marketing email: held ({hold['reason']})")
    since = (state.clock() - timedelta(hours=24)).isoformat(timespec="seconds")
    problems = state._all("SELECT source, message FROM errors WHERE kind = 'alert' AND created_at >= ? ORDER BY id DESC LIMIT 3",
                          (since,))
    lines += ["Alerts in the last 24 hours:"] + [f"- {p['source']}: {p['message'][:200]}" for p in problems] if problems \
        else ["No alerts in the last 24 hours."]
    return "\n".join(lines)


def execute(tools: Any, command: str, engine: Any = None, days: int | None = None) -> str:
    from strategies.owner_todo import as_text, todo
    from tools.contact_policy import set_quiet

    state, cfg = tools.state, tools.config
    if command == "status":
        return status_text(tools)
    if command == "todo":
        return as_text(todo(state, cfg))
    if command == "pause":
        if engine is not None:
            engine.pause("paused by email command")
        else:
            state.set("paused", {"reason": "paused by email command", "at": state.now()})
        return "Paused. Deliveries and the webhook keep working. Send AM RESUME <code> to continue."
    if command == "resume":
        if engine is not None:
            engine.unpause()
        else:
            state.set("paused", None)
        return "Resumed: cycles run again."
    if command == "drafts":  # Phase 204: post from your phone
        from strategies.marketing_engine import PLAYBOOK, queue

        items = queue(state, 10)
        if not items:
            return "No drafts waiting. New ones arrive daily."
        parts = [f"#{p['id']} {PLAYBOOK.get(p['strategy'], {}).get('name', p['strategy'])}: {p['title']}\n\n{p['body']}" for p in items]
        return ("Drafts to post (after posting, mark them in the control panel's Marketing tab or with "
                "`automonetize marketing done <id>`):\n\n" + "\n\n----------\n\n".join(parts))
    if command == "report":
        from strategies.owner_reports import OwnerReports, local_now

        return OwnerReports.digest_body(tools, local_now(state, cfg))
    if command == "quiet":
        span = days if days and 0 < days <= 365 else None
        set_quiet(state, True, "turned on by email command" + (f" for {span} day(s)" if span else ""), days=span)
        return (f"Quiet mode on{f' for {span} day(s)' if span else ''}: no marketing email goes out. Purchases and support "
                "still do. Send AM LOUD <code> to undo.")
    if command == "loud":
        set_quiet(state, False)
        return "Quiet mode off: marketing email goes out again (within the usual limits)."
    return help_text(cfg, state)


def poll(tools: Any, engine: Any = None, scan: Any = None) -> int:
    """Read and run new commands. Returns how many ran. Never raises."""
    from tools.dispatcher import Email
    from tools.inbox import imap_settings, scan_mail

    import os

    from agent.self_update import NO_UPDATE_ENV

    state, cfg = tools.state, tools.config
    try:
        if not cfg.owner_commands or (scan is None and os.environ.get(NO_UPDATE_ENV)):  # sandboxes never read a real mailbox
            return 0
        last = state.get(POLL_KEY)
        if last and state.clock() - datetime.fromisoformat(last) < timedelta(minutes=POLL_MINUTES):
            return 0
        state.set(POLL_KEY, state.now())
        host, user, password = imap_settings(cfg)
        owner = owner_address(cfg)
        if not (host and user and password and owner):
            return 0
        since = (state.clock() - timedelta(days=2)).strftime("%d-%b-%Y")
        messages = (scan or scan_mail)(host, user, password, lambda s: s == owner, since, unseen_only=False)
        handled = list(state.get(SEEN) or [])
        seen = set(handled)
        secret = code(state)
        ran = 0
        for m in messages:
            if m["message_id"] in seen:
                continue
            handled.append(m["message_id"])
            seen.add(m["message_id"])
            match = SUBJECT_RE.match(m["subject"] or "")
            if not match or match.group(3).upper() != secret:
                continue
            command = match.group(1).lower()
            reply = execute(tools, command if command in COMMANDS else "help", engine,
                            int(match.group(2)) if match.group(2) else None)
            state.log_action(int(state.get("iteration", 0)), None, "owner_command", "ok", command)
            tools.dispatcher.send_transactional(
                Email(to=owner, subject=f"AM {command.upper()}: done" if command in COMMANDS else "AM: commands", body=reply,
                      kind="delivery"), audit_key=f"owner_command:{m['message_id']}")
            ran += 1
        state.set(SEEN, handled[-500:])
        return ran
    except Exception as exc:  # noqa: BLE001 - a mailbox problem must never stop a cycle
        state.log_error("owner_commands", f"couldn't read commands: {exc!r}")
        return 0
