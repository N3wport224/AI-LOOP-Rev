"""Owner reports: the agent tells you what happened, so you never have to check on it.

Every cycle (the ``report_owner`` task), to ``owner_email`` (default: the sender address):

* **New sales**: one email per cycle listing each new order and subscription: product, amount,
  buyer. The first run only records where it starts, so you don't get the whole history.
* **Alerts**: anything the agent flagged for a human (a quarantine, an evolution rollback, a
  delivery it couldn't make), at most one email per cycle.
Preferences (Phase 63): ``sale_alerts`` = each | daily (only in the digest) | off, and
``owner_digest`` = daily | weekly (Mondays) | off. With ``NTFY_TOPIC`` set, every sale and alert
also buzzes your phone (``tools/notify.py``, ``automonetize phone``).

* **Daily digest**, once a day after ``owner_digest_hour`` (in ``subscription_timezone``):
  yesterday's and the week's revenue against the $10/day goal, MRR, what's on sale (with links),
  free leads, outreach drafts waiting for you, and problems in the last 24 hours (one line per
  distinct problem with a count, Phase 109). On Mondays it
  also carries the week's share kit (ready-to-paste posts, ``strategies/share_kit.py``) and the
  slowest tasks (``tools/timings.py``). It has an HTML version (Phase 110) and, with
  ``ntfy_topic`` set, a one-line summary on your phone (Phase 111, ``digest_push``).
* **Milestones** (Phase 114, ``strategies/milestones.py``): first sale, first goal day, $100 and $1,000.

Pointers only advance after a send succeeds, so a failed email is retried next cycle. In dry run
the emails are only written to the audit log.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from strategies.base import Strategy, TaskContext, TaskResult
from tools.dispatcher import Email


def money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _shape(message: str) -> str:
    """The message with numbers, ids and quoted values blanked, so repeats of one problem group together."""
    import re

    text = re.sub(r"'[^']*'|\"[^\"]*\"", "…", message)
    return re.sub(r"\d+", "#", text)[:160]


def grouped_problems(state: Any, since: str, limit: int = 8) -> list[dict[str, Any]]:
    """Phase 109: the last day's failures and alerts, one line per distinct problem with a count
    (newest example shown), most frequent first, instead of the same error repeated."""
    rows = state._all("SELECT source, message, created_at FROM errors WHERE kind IN ('operational_failure', 'alert') "
                      "AND created_at >= ? ORDER BY id DESC LIMIT 500", (since,))
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        key = (r["source"], _shape(r["message"]))
        if key in groups:
            groups[key]["n"] += 1
        else:
            groups[key] = {"source": r["source"], "message": r["message"], "n": 1, "last": r["created_at"]}
    return sorted(groups.values(), key=lambda g: -g["n"])[:limit]  # stable: ties stay newest first


def problem_line(p: dict[str, Any]) -> str:
    times = f" (×{p['n']})" if p["n"] > 1 else ""
    return f"- {p['source']}{times}: {p['message'][:200]}"


def local_now(state: Any, cfg: Any) -> datetime:
    try:
        tz = ZoneInfo(cfg.subscription_timezone or "UTC")
    except Exception:  # noqa: BLE001 - a bad timezone name falls back to UTC
        tz = ZoneInfo("UTC")
    return state.clock().astimezone(tz)


def _max_id(state: Any, table: str, where: str = "") -> int:
    return int(state._one(f"SELECT COALESCE(MAX(id), 0) AS n FROM {table} {where}")["n"])


class OwnerReports(Strategy):
    name = "owner_reports"
    tasks = ("report_owner",)

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        tools, cfg, state = ctx.tools, ctx.tools.config, ctx.tools.state
        to = cfg.owner_email or cfg.sender_email
        if not cfg.owner_reports or not to:
            return TaskResult(True, "owner reports off (set owner_email or the sender email)", {"sent": 0})
        if state.get("owner_report_started") is None:  # first run: start from now, no history flood
            state.set("owner_last_order_id", _max_id(state, "orders"))
            state.set("owner_last_subscriber_id", _max_id(state, "subscribers"))
            state.set("owner_last_alert_id", _max_id(state, "errors", "WHERE kind = 'alert'"))
            state.set("owner_report_started", state.now())
        sent = []
        from strategies.milestones import announce

        for name, step in (("sales", self.sales), ("alerts", self.alerts), ("milestones", announce), ("whats_new", self.whats_new),
                           ("digest", self.digest)):
            try:
                if step(tools, to):
                    sent.append(name)
            except Exception as exc:  # noqa: BLE001 - reports must never fail the cycle; retried next time
                state.log_error("owner_reports", f"{name} email failed: {exc!r}")
        return TaskResult(True, f"owner reports: {', '.join(sent) or 'nothing new'}", {"sent": len(sent), "kinds": sent})

    # -- new sales ----------------------------------------------------------------------------
    def sales(self, tools: Any, to: str) -> bool:
        state = tools.state
        last_order = int(state.get("owner_last_order_id") or 0)
        last_sub = int(state.get("owner_last_subscriber_id") or 0)
        orders = state._all("SELECT * FROM orders WHERE id > ? ORDER BY id", (last_order,))
        subs = state._all("SELECT * FROM subscribers WHERE id > ? AND tier = 'paid' ORDER BY id", (last_sub,))
        if not orders and not subs:
            return False
        lines, total = [], 0
        for o in orders:
            asset = state.get_asset(o["asset_id"]) if o.get("asset_id") else None
            total += int(o["gross_cents"])
            lines.append(f"- {money(int(o['gross_cents']))}  {asset['title'] if asset else 'order'}  (buyer: {o.get('email') or 'n/a'}, "
                         f"status: {o['status']})")
        for s in subs:
            total += int(s["price_cents"] or 0)
            lines.append(f"- {money(int(s['price_cents'] or 0))}/{s.get('interval') or 'month'}  new subscriber "
                         f"({s.get('email') or 'n/a'})")
        n = len(orders) + len(subs)
        subject = f"💰 New sale: {money(total)}" if n == 1 else f"💰 {n} new sales: {money(total)}"
        from tools.notify import push

        first = state.get_asset(orders[0]["asset_id"]) if orders and orders[0].get("asset_id") else None
        push(tools.http, tools.config, subject.replace("💰 ", ""), (first or {}).get("title") or "subscription",
             tags="moneybag")
        if tools.config.sale_alerts != "each":  # "daily": the digest has them; "off": no sale emails at all
            if orders:
                state.set("owner_last_order_id", int(orders[-1]["id"]))
            if subs:
                state.set("owner_last_subscriber_id", int(subs[-1]["id"]))
            return False
        today = tools.revenue.daily_summary()
        body = "\n".join(["Good news, the agent just made a sale.", "", *lines, "",
                          f"Today so far: {money(today['net_cents'])} net of your {money(today['target_cents'])}/day goal.",
                          "", "Delivery is automatic; nothing for you to do.", "", "AutoMonetize"])
        tools.dispatcher.send_transactional(Email(to=to, subject=subject, body=body, kind="delivery"),
                                            audit_key=f"owner:sales:{last_order}:{last_sub}")
        if orders:
            state.set("owner_last_order_id", int(orders[-1]["id"]))
        if subs:
            state.set("owner_last_subscriber_id", int(subs[-1]["id"]))
        return True

    # -- what's new (Phase 73) ----------------------------------------------------------------------
    def whats_new(self, tools: Any, to: str) -> bool:
        """After a self-update passes its canary: one email with what changed."""
        state = tools.state
        news = state.get("whats_new")
        if not news or news.get("sent"):
            return False
        changes = news.get("changes") or ["(no details)"]
        body = "\n".join([f"The agent installed an update by itself ({news['target'][:12]}) and it has run cleanly since. "
                          "What changed:", "", *[f"- {c}" for c in changes[:20]], "",
                          "Nothing for you to do. `automonetize version` shows what's running.", "", "AutoMonetize"])
        tools.dispatcher.send_transactional(Email(to=to, subject=f"✨ AutoMonetize updated ({len(changes)} change(s))", body=body,
                                                  kind="delivery"), audit_key=f"owner:whats_new:{news['target']}")
        state.set("whats_new", {**news, "sent": True})
        return True

    # -- alerts -----------------------------------------------------------------------------------
    def alerts(self, tools: Any, to: str) -> bool:
        state = tools.state
        last = int(state.get("owner_last_alert_id") or 0)
        rows = state._all("SELECT * FROM errors WHERE kind = 'alert' AND id > ? ORDER BY id LIMIT 20", (last,))
        if not rows:
            return False
        from tools.alert_mute import is_muted

        loud = [r for r in rows if not is_muted(state, r["source"])]
        if not loud:  # all muted (Phase 137): recorded, shown in doctor, no email
            state.set("owner_last_alert_id", int(rows[-1]["id"]))
            return False
        last_id, rows = int(rows[-1]["id"]), loud
        from tools.notify import push

        push(tools.http, tools.config, f"AutoMonetize needs attention ({len(rows)})", rows[0]["message"][:300], priority="high",
             tags="warning")
        body = "\n".join(["The agent flagged something for you:", "",
                          *[f"- {r['created_at'][:16]} {r['source']}: {r['message'][:400]}" for r in rows], "",
                          "Details: run `am` then `automonetize doctor` in Terminal.", "", "AutoMonetize"])
        tools.dispatcher.send_transactional(
            Email(to=to, subject=f"⚠️ AutoMonetize needs attention ({len(rows)})", body=body, kind="delivery"),
            audit_key=f"owner:alerts:{last}")
        state.set("owner_last_alert_id", last_id)
        return True

    # -- daily digest ---------------------------------------------------------------------------------
    def digest(self, tools: Any, to: str) -> bool:
        cfg, state = tools.config, tools.state
        try:
            tz = ZoneInfo(cfg.subscription_timezone or "UTC")
        except Exception:  # noqa: BLE001 - a bad timezone name falls back to UTC
            tz = ZoneInfo("UTC")
        local = state.clock().astimezone(tz)
        day = local.date().isoformat()
        if local.hour < int(cfg.owner_digest_hour) or state.get("owner_digest_date") == day:
            return False
        if cfg.owner_digest == "off" or (cfg.owner_digest == "weekly" and local.weekday() != 0):
            return False
        body = self.digest_body(tools, local)
        subject = f"📊 AutoMonetize daily report: {local:%a %d %b}"
        from tools.report_html import text_to_html

        tools.dispatcher.send_transactional(Email(to=to, subject=subject, body=body, kind="delivery",
                                                  html=text_to_html(body, subject[2:])), audit_key=f"owner:digest:{day}")
        state.set("owner_digest_date", day)
        if cfg.digest_push:
            from tools.notify import push

            push(tools.http, cfg, "AutoMonetize daily", self.push_line(tools), tags="bar_chart")
        return True

    @staticmethod
    def push_line(tools: Any) -> str:
        """Phase 111: the report in one line for the phone."""
        state = tools.state
        history = tools.revenue.history(7)
        yesterday = int(history[-2]["net_cents"]) if len(history) > 1 else 0
        week = sum(int(d.get("net_cents", 0)) for d in history)
        since = (state.clock() - timedelta(hours=24)).isoformat(timespec="seconds")
        alerts = int(state._one("SELECT COUNT(*) AS n FROM errors WHERE kind = 'alert' AND created_at >= ?", (since,))["n"])
        from strategies.owner_todo import todo

        todos = len(todo(state, tools.config))
        return (f"Yesterday {money(yesterday)} · 7 days {money(week)} · "
                f"{alerts} alert(s) · {todos} thing(s) only you can do")

    @staticmethod
    def digest_body(tools: Any, local: datetime) -> str:
        from dashboard.analytics import compute
        from strategies.finance import describe
        from strategies.freshness_guard import stale_titles
        from strategies.goal_pacing import describe as describe_pace
        from strategies.storefront_health import KEY as HEALTH
        from tools.catalog import live_products

        cfg, state = tools.config, tools.state
        history = tools.revenue.history(7)
        week = sum(int(d.get("net_cents", 0)) for d in history)
        yesterday = next((d for d in history if d.get("date") == (local.date() - timedelta(days=1)).isoformat()), {})
        try:
            report = compute(state, cfg, "30d")
            mrr = int((report.get("recurring") or report.get("subscriptions") or {}).get("mrr_cents", 0))
        except Exception:  # noqa: BLE001 - analytics must not block the report
            mrr = 0
        products = live_products(state)
        leads = state.free_subscriber_counts()
        drafts = state.outreach_counts().get("pending_review", 0)
        since = (state.clock() - timedelta(hours=24)).isoformat(timespec="seconds")
        problems = grouped_problems(state, since)
        cycles = int(state._one("SELECT COUNT(DISTINCT cycle) AS n FROM actions WHERE created_at >= ?", (since,))["n"])
        from strategies.money_insight import describe_forecast, forecast
        from strategies.owner_todo import as_text as todo_text, todo

        owner_items = todo(state, cfg)
        lines = [
            *(["Only you can do these (most valuable first):", todo_text(owner_items), ""] if owner_items else []),
            f"Yesterday: {money(int(yesterday.get('net_cents', 0)))} net (goal {money(cfg.daily_target_cents)}/day)",
            f"Last 7 days: {money(week)} net · MRR {money(mrr)}",
            describe(state.get("stripe_finance")),
            describe_pace(state.get("goal_pace")),
            describe_forecast(forecast(state, cfg)),
            f"Agent: {cycles} cycles in the last 24 hours",
            "",
            f"On sale ({len(products)}):" if products else "On sale: nothing yet. Run `automonetize go-live` (see the README).",
            *[f"- {p['title']}  {money(p['price_cents'])}  {p['url']}" for p in products],
            "",
            f"Free-sample leads: {leads.get('active', 0)} confirmed, {leads.get('pending', 0)} pending",
        ]
        from strategies.seasonal_sale import active_sale

        sale = active_sale(state)
        if sale:
            lines.append(f"Sale running: {sale['percent_off']}% off with {sale['code']} until "
                         f"{datetime.fromtimestamp(int(sale['expires_at']), tz=timezone.utc):%b %d}.")
        broken = [p for p in (state.get(HEALTH) or {}).get("products", []) if not p["ok"]]
        if broken:
            lines += ["", "Can't be bought right now:", *[f"- {p['title']}: {', '.join(p['problems'])}" for p in broken]]
        from strategies.release_gate import held

        for niche, hold in held(state).items():
            lines.append(f"Update held for {niche} ({hold['reason']}): buyers get the previous version.")
        stale = stale_titles(state)
        if stale:
            lines.append(f"Not being promoted (no new job postings lately): {', '.join(stale)}")
        if drafts:
            lines.append(f"Sales emails waiting for your OK: {drafts} (control panel → Outreach)")
        if problems:
            lines += ["", "Problems in the last 24 hours:", *[problem_line(p) for p in problems]]
        if not (cfg.github_pages_repo and cfg.pages_base_url):
            lines += ["", "Tip: `automonetize connect-marketing` puts your products on a public website and turns on articles."]
        if local.weekday() == 0:
            from strategies.customer_requests import summary as request_summary
            from strategies.ratings import summary as rating_summary

            stars = rating_summary(state)
            if stars:
                lines += ["", stars]

            asks = request_summary(state)
            if asks:
                lines += ["", asks]
            from strategies.money_insight import abandoned, describe_abandoned, describe_trends, niche_trends

            for line in (describe_trends(niche_trends(state)), describe_abandoned(abandoned(state))):
                if line:
                    lines += ["", line]
            from tools.timings import describe as describe_timings
            from tools.timings import timings

            slow = describe_timings(timings(state))
            if slow:
                lines += ["", slow]
            from tools.customers import describe as describe_customers
            from tools.customers import report as customer_report

            lines += ["", describe_customers(customer_report(state))]
        kit = state.get("share_kit")
        if local.weekday() == 0 and kit and kit.get("posts"):
            from strategies.share_kit import as_text

            lines += ["", "This week's share kit: copy, paste, post (each link tracks which channel sold):", "", as_text(kit)]
        else:
            lines += ["", "Share your links: every visitor is a chance at a sale (ready-made posts: control panel → Share)."]
        if cfg.owner_commands and cfg.sender_email:
            from agent.owner_commands import help_text

            lines += ["", help_text(cfg, state)]
        from tools.version import describe as describe_version
        from tools.version import info as version_info

        lines += ["", describe_version(version_info())]
        return "\n".join(lines)
