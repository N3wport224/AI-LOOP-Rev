"""Milestones (Phase 114): a short note when something big happens for the first time.

Checked by the owner reports every cycle; each milestone is sent once (kv ``milestones``):

* **first sale**: with what to do next (share the link that sold, ask for a quote);
* **first goal day**: the first day at or above ``daily_target_cents`` net;
* **$100** and **$1,000** total net.

Only verified revenue counts (the same numbers as the daily report). Also buzzes your phone when
``ntfy_topic`` is set.
"""

from __future__ import annotations

from typing import Any

KEY = "milestones"


def money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def reached(state: Any, cfg: Any, today_net: int) -> list[tuple[str, str, str]]:
    """[(key, subject, body)] for milestones reached and not yet announced."""
    done = set(state.get(KEY) or [])
    row = state._one("SELECT COUNT(*) AS n, COALESCE(SUM(net_cents), 0) AS net FROM revenue WHERE verified = 1 AND net_cents > 0")
    n, total = int(row["n"]), int(row["net"])
    out = []
    if n >= 1 and "first_sale" not in done:
        out.append(("first_sale", "🎉 Your first sale!",
                    "The agent made its first sale. Real money, from a real buyer, with nobody in the loop.\n\n"
                    "Two things that help the next ones come sooner:\n"
                    "- Share the product link once more where you got the first buyer (control panel → Share has ready posts).\n"
                    "- Leave the agent running: it follows the buyer up and asks how it was."))
    if today_net >= int(cfg.daily_target_cents) > 0 and "first_goal_day" not in done:
        out.append(("first_goal_day", f"🎯 Goal reached: {money(today_net)} today",
                    f"Today is the first day at or above your {money(int(cfg.daily_target_cents))}/day goal."))
    for cents, key in ((10_000, "total_100"), (100_000, "total_1000")):
        if total >= cents and key not in done:
            out.append((key, f"🏁 {money(cents)} earned in total", f"Verified net revenue has passed {money(cents)}: "
                                                                     f"{money(total)} from {n} sale(s)."))
    return out


def announce(tools: Any, to: str) -> bool:
    from tools.dispatcher import Email
    from tools.notify import push

    state, cfg = tools.state, tools.config
    if state.get(KEY) is None:  # first run on an existing install: don't celebrate history
        today = tools.revenue.daily_summary()["net_cents"]
        state.set(KEY, [k for k, _, _ in reached(state, cfg, today)])
        return False
    news = reached(state, cfg, int(tools.revenue.daily_summary()["net_cents"]))
    for key, subject, body in news:
        tools.dispatcher.send_transactional(Email(to=to, subject=subject, body=body + "\n\nAutoMonetize", kind="delivery"),
                                            audit_key=f"owner:milestone:{key}")
        push(tools.http, cfg, subject, body.split("\n")[0][:200], tags="tada")
        state.set(KEY, sorted(set(state.get(KEY) or []) | {key}))
    return bool(news)
