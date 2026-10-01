""""Agent started" notice (Phase 72): proof, after every start, that it's really running.

The first cycle after the agent starts (a reboot, an update, ``automonetize autostart``) sends you
one short email (and a phone notification, if set up): which Mac, which version, whether every
outside service answers (``tools/connections.py``), and how many to-do items are left. At most one
every ``NOTICE_HOURS`` (6), so a restart loop can't flood you. Part of ``agent/``.
"""

from __future__ import annotations

import os
import platform
from datetime import datetime, timedelta
from typing import Any

KEY = "start_notice_at"
NOTICE_HOURS = 6


def maybe_notify(tools: Any, check: Any = None) -> bool:
    from agent.self_update import NO_UPDATE_ENV
    from strategies.owner_todo import todo
    from tools import connections
    from tools.dispatcher import Email
    from tools.notify import push
    from tools.version import describe, info

    state, cfg = tools.state, tools.config
    if os.environ.get(NO_UPDATE_ENV) and check is None:
        return False
    last = state.get(KEY)
    if last and state.clock() - datetime.fromisoformat(last) < timedelta(hours=NOTICE_HOURS):
        return False
    to = cfg.owner_email or cfg.sender_email
    state.set(KEY, state.now())
    if not to:
        return False
    try:
        conns = (check or connections.check_all)(cfg, tools.http)
        items = todo(state, cfg)
        version = describe(info())
        lines = [f"The agent started on {platform.node() or 'your Mac'} ({version}).", "",
                 "Connections: " + connections.summary(conns)]
        lines += [f"- {c.name}: {c.detail}" + (f" → {c.fix}" if c.status == "fail" and c.fix else "")
                  for c in conns if c.status != "skip"]
        lines += ["", f"Things only you can do: {len(items)}" + (" (run `automonetize todo`)" if items else ""), "", "AutoMonetize"]
        bad = sum(1 for c in conns if c.status == "fail")
        subject = "✅ AutoMonetize is running" if not bad else f"⚠️ AutoMonetize started, {bad} connection problem(s)"
        tools.dispatcher.send_transactional(Email(to=to, subject=subject, body="\n".join(lines), kind="delivery"),
                                            audit_key=f"start:{state.now()}")
        push(tools.http, cfg, subject.replace("✅ ", "").replace("⚠️ ", ""), connections.summary(conns), state=tools.state)
        return True
    except Exception as exc:  # noqa: BLE001 - a notice must never stop the first cycle
        state.log_error("startup_notice", f"couldn't send the start notice: {exc!r}")
        return False
