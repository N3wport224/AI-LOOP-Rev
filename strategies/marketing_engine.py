"""Marketing strategy engine (Phases 170-174): a steady stream of traffic plays, kept or dropped by results.

* **Phase 170, playbook:** ``PLAYBOOK`` lists every traffic strategy the agent knows. Each one turns
  the current catalog (new products, best sellers, fresh numbers) into concrete *plays*:
  * **auto** plays run by themselves (articles, feeds, teasers, SEO pages, the newsletter...);
  * **draft** plays are ready-to-post texts for places that don't allow bots or need your
    account (Reddit, LinkedIn, X, Hacker News, Indie Hackers, Product Hunt, newsletter swaps). You
    post them; ``automonetize marketing done <id>`` records it.
  Every play's link carries ``utm_source=<channel>&utm_campaign=mkt<id>``, so its sales are
  counted (campaigns are cleaned to ``[a-z0-9_]`` on the way back from Stripe).
* **Phase 171, scheduler** (``plan_marketing``, daily): each strategy that is due (``every_days``)
  and not paused gets a play; at most ``marketing_drafts_per_day`` drafts are queued so you're
  never flooded, picked by score.
* **Phase 172, scoring:** per strategy, over 60 days: plays, checkouts started and kept orders
  from its channel/campaigns, revenue per play, plus an exploration bonus for strategies with few
  plays (UCB). Strategies with 8+ plays and no checkout at all are paused for 30 days (Phase 192
  makes it visible).
* **Phase 173, weekly calendar:** every Monday, the next 7 days of plays (``automonetize marketing
  plan``) in the report.
* **Phase 174, daily line:** the daily report says what ran yesterday, which drafts wait for you
  and which strategy earns most.

State: table ``marketing_plays``, kv ``marketing_paused``, ``marketing_calendar``.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta
from typing import Any, Callable

from strategies.base import Strategy, TaskContext, TaskResult

_SCHEMA = """CREATE TABLE IF NOT EXISTS marketing_plays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy TEXT NOT NULL,
    channel TEXT NOT NULL,
    mode TEXT NOT NULL,                 -- auto | draft
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    link TEXT,
    status TEXT NOT NULL,               -- done | queued | posted | skipped
    created_at TEXT NOT NULL,
    done_at TEXT
)"""
PAUSED = "marketing_paused"
CALENDAR = "marketing_calendar"
PAUSE_AFTER_PLAYS = 8
PAUSE_DAYS = 30
SCORE_DAYS = 60


def ensure(state: Any) -> None:
    state._exec(_SCHEMA)


# ------------------------------------------------------------------ catalog helpers
def featured(state: Any, cfg: Any, n: int = 3) -> list[dict[str, Any]]:
    """Products worth promoting: best sellers first, then the newest."""
    from strategies.revenue_models import live_products
    from strategies.upsells import best_sellers

    live = [a for a in live_products(state) if a.get("checkout_url")]
    best = [b["niche"] for b in best_sellers(state, limit=n)]
    ranked = [a for niche in best for a in live if a.get("niche") == niche]
    ranked += sorted((a for a in live if a not in ranked), key=lambda a: a["created_at"], reverse=True)
    return ranked[:n]


def product_url(cfg: Any, asset: dict[str, Any]) -> str:
    from strategies.sales_channels import product_url as url

    return url(cfg, asset)


def tagged(url: str, channel: str, play_id: int) -> str:
    from tools.attribution import add_utm

    return add_utm(url, channel, "marketing", f"mkt{play_id}")


# ------------------------------------------------------------------ Phase 170: playbook
def _draft_texts(kind: str, product: dict[str, Any], link: str, rows: int) -> tuple[str, str]:
    title = product["title"]
    n = f"{rows:,}" if rows else "hundreds of"
    texts = {
        "reddit": (f"[Data] {title}: {n} current postings, analysed",
                   f"I pull public job postings every day and group them by stack. This week: {n} postings for \"{title}\". "
                   f"Free 5-row preview and the full list here: {link}\n\nHappy to share the method. (Check the subreddit's "
                   "self-promotion rules first; data posts with the method usually go down best.)"),
        "linkedin": (f"{title}", f"Who's hiring right now? I track public job postings daily. {n} current postings in "
                     f"\"{title}\", with company, stack and links to each role.\n\nPreview: {link}\n\n#hiring #recruiting #data"),
        "x": (f"{title}", f"{n} companies-and-roles in one file: \"{title}\". Updated weekly from public postings. "
              f"Free preview: {link}"),
        "hn": ("Show HN: hiring datasets built from public job postings, updated weekly",
               f"I built an agent that collects public job postings and turns them into small datasets by stack, region and "
               f"seniority. Example: \"{title}\" ({n} postings). Preview: {link}\n\nFeedback on the data welcome."),
        "indiehackers": (f"Building a data business on autopilot: {title}",
                         f"Milestone post: the agent now makes a new dataset every few minutes from public postings. "
                         f"Latest: \"{title}\" ({n} postings). {link}\n\nWhat would you want sliced next?"),
        "producthunt": ("Product Hunt launch: hiring datasets by tech, region and seniority",
                        f"Tagline: Who's hiring, sliced your way.\nFirst comment: datasets built from public job postings, "
                        f"refreshed weekly. Example: \"{title}\". {link}\nSchedule for 12:01am PT on a Tuesday-Thursday."),
        "newsletter_swap": ("Pitch: a data snippet for your newsletter",
                            f"Hi, I run a small hiring-data project. I can give your readers a free data snippet "
                            f"(e.g. \"{title}\", {n} current postings) with a link: {link}. Would that fit an upcoming issue? "
                            "(Send only to newsletters you know accept pitches; never bulk-send.)"),
    }
    return texts[kind]


def _draft_maker(kind: str) -> Callable[[Any, int], dict[str, Any] | None]:
    def make(tools: Any, play_id: int) -> dict[str, Any] | None:
        picks = featured(tools.state, tools.config, 1)
        if not picks:
            return None
        product = picks[0]
        link = tagged(product_url(tools.config, product), kind, play_id)
        title, body = _draft_texts(kind, product, link, int(product.get("lead_count") or 0))
        return {"title": title, "body": body, "link": link}
    return make


def _auto(module: str, func: str) -> Callable[[Any, int], dict[str, Any] | None]:
    """An auto play: calls ``module.func(tools)``, which returns a one-line summary or "" (nothing to do)."""
    def make(tools: Any, play_id: int) -> dict[str, Any] | None:
        import importlib

        summary = getattr(importlib.import_module(module), func)(tools)
        return {"title": summary, "body": summary, "link": ""} if summary else None
    return make


PLAYBOOK: dict[str, dict[str, Any]] = {
    # drafts: you post them (needs your account or a human by the community's rules)
    "reddit_data_post": {"name": "Reddit data post", "channel": "reddit", "mode": "draft", "every_days": 4, "minutes": 5,
                         "make": _draft_maker("reddit")},
    "linkedin_post": {"name": "LinkedIn post", "channel": "linkedin", "mode": "draft", "every_days": 2, "minutes": 3,
                      "make": _draft_maker("linkedin")},
    "x_post": {"name": "X post", "channel": "x", "mode": "draft", "every_days": 1, "minutes": 2, "make": _draft_maker("x")},
    "show_hn": {"name": "Show HN", "channel": "hn", "mode": "draft", "every_days": 90, "minutes": 10, "make": _draft_maker("hn")},
    "indiehackers": {"name": "Indie Hackers post", "channel": "indiehackers", "mode": "draft", "every_days": 14, "minutes": 10,
                     "make": _draft_maker("indiehackers")},
    "producthunt": {"name": "Product Hunt launch", "channel": "producthunt", "mode": "draft", "every_days": 180, "minutes": 30,
                    "make": _draft_maker("producthunt")},
    "newsletter_swap": {"name": "Newsletter pitch", "channel": "newsletter_swap", "mode": "draft", "every_days": 7, "minutes": 5,
                        "make": _draft_maker("newsletter_swap")},
    # auto: run by the agent (more are added by the SEO, content and distribution phases)
    "devto_articles": {"name": "Dev.to / Hashnode articles", "channel": "devto", "mode": "tracked", "every_days": 0},
    "rss": {"name": "RSS feeds", "channel": "rss", "mode": "tracked", "every_days": 0},
    "github_teasers": {"name": "GitHub teasers and gists", "channel": "github", "mode": "tracked", "every_days": 0},
    "release_emails": {"name": "New-release emails", "channel": "announce", "mode": "tracked", "every_days": 0},
    "affiliates": {"name": "Affiliates", "channel": "aff", "mode": "tracked", "every_days": 0},
}


def register(key: str, name: str, channel: str, every_days: float, module: str, func: str, measurable: bool = True) -> None:
    """Later phases add auto strategies here (SEO, content, distribution). ``measurable=False``:
    its effect can't be tied to sales (organic search, the blog), so it's never paused for lack of them."""
    PLAYBOOK[key] = {"name": name, "channel": channel, "mode": "auto", "every_days": every_days, "make": _auto(module, func),
                     "measurable": measurable}


def register_draft(key: str, name: str, channel: str, every_days: float, module: str, func: str, minutes: int = 5) -> None:
    import importlib

    def make(tools: Any, play_id: int) -> dict[str, Any] | None:
        return getattr(importlib.import_module(module), func)(tools, play_id)

    PLAYBOOK[key] = {"name": name, "channel": channel, "mode": "draft", "every_days": every_days, "minutes": minutes, "make": make}


register("blog_post", "Blog post on your site", "blog", 1, "strategies.content_engine", "write_post", measurable=False)
register("weekly_roundup", "Weekly new-datasets roundup (syndicated)", "devto", 7, "strategies.content_engine", "weekly_roundup")
register_draft("weekly_thread", "Weekly X / LinkedIn thread", "x", 7, "strategies.content_engine", "weekly_thread")


# ------------------------------------------------------------------ Phase 172: scoring
def scores(state: Any, days: int = SCORE_DAYS) -> dict[str, dict[str, Any]]:
    ensure(state)
    since = (state.clock() - timedelta(days=days)).isoformat(timespec="seconds")
    total_plays = 0
    out = {}
    for key, s in PLAYBOOK.items():
        plays = int(state._one("SELECT COUNT(*) AS n FROM marketing_plays WHERE strategy = ? AND created_at >= ? "
                               "AND status IN ('done', 'posted')", (key, since))["n"])
        if s["mode"] == "draft":  # only this strategy's own plays (several strategies may share a channel)
            ids = [f"mkt{r['id']}" for r in state._all("SELECT id FROM marketing_plays WHERE strategy = ?", (key,))] or ["-"]
            like, args = f"AND campaign IN ({','.join('?' * len(ids))})", (s["channel"], *ids, since)
        else:
            like, args = "", (s["channel"], since)
        orders = state._one(f"SELECT COUNT(*) AS n, COALESCE(SUM(gross_cents), 0) AS gross FROM orders WHERE channel = ? {like} "  # noqa: S608
                            "AND occurred_at >= ? AND status NOT IN ('refunded', 'disputed')", args)
        checkouts = int(state._one(f"SELECT COUNT(*) AS n FROM checkout_sessions WHERE channel = ? {like} AND updated_at >= ?",
                                   args)["n"])
        total_plays += plays
        out[key] = {"name": s["name"], "mode": s["mode"], "plays": plays, "orders": int(orders["n"]),
                    "revenue_cents": int(orders["gross"]), "checkouts": checkouts}
    for key, row in out.items():
        n = max(1, row["plays"])
        mean = row["revenue_cents"] / n
        row["score"] = round(mean + 500 * math.sqrt(2 * math.log(max(2, total_plays)) / n), 1)  # UCB: try the untried
    return out


def paused(state: Any) -> dict[str, str]:
    now = state.clock()
    return {k: v for k, v in (state.get(PAUSED) or {}).items() if datetime.fromisoformat(v) > now}


def pause_losers(state: Any) -> list[str]:
    """Phase 192: 8+ plays and not even a checkout started → rest for 30 days."""
    current = dict(state.get(PAUSED) or {})
    newly = []
    for key, row in scores(state).items():
        if PLAYBOOK[key]["mode"] == "tracked" or not PLAYBOOK[key].get("measurable", True) or key in paused(state):
            continue
        if row["plays"] >= PAUSE_AFTER_PLAYS and not row["checkouts"] and not row["orders"]:
            current[key] = (state.clock() + timedelta(days=PAUSE_DAYS)).isoformat(timespec="seconds")
            newly.append(key)
    if newly:
        state.set(PAUSED, current)
    return newly


# ------------------------------------------------------------------ Phase 171: scheduler
def last_play(state: Any, key: str) -> str | None:
    ensure(state)
    row = state._one("SELECT MAX(created_at) AS t FROM marketing_plays WHERE strategy = ?", (key,))
    return row["t"] if row else None


def due(state: Any, key: str) -> bool:
    s = PLAYBOOK[key]
    if s["mode"] == "tracked" or key in paused(state):
        return False
    last = last_play(state, key)
    return not last or state.clock() - datetime.fromisoformat(last) >= timedelta(days=float(s["every_days"]))


def add_play(state: Any, key: str, mode: str, status: str) -> int:
    ensure(state)
    cur = state._exec("INSERT INTO marketing_plays (strategy, channel, mode, title, body, status, created_at) "
                      "VALUES (?,?,?,?,?,?,?)", (key, PLAYBOOK[key]["channel"], mode, "", "", status, state.now()))
    return int(cur.lastrowid)


def plan(tools: Any, rng: random.Random | None = None) -> dict[str, list[str]]:
    state, cfg = tools.state, tools.config
    ensure(state)
    rng = rng or random.Random()
    ran, drafted, skipped = [], [], []
    for key in [k for k, s in PLAYBOOK.items() if s["mode"] == "auto" and due(state, k)]:
        pid = add_play(state, key, "auto", "done")
        try:
            out = PLAYBOOK[key]["make"](tools, pid)
        except Exception as exc:  # noqa: BLE001 - one channel's failure must not stop the others
            state.log_error("marketing", f"{key} failed: {exc!r}")
            out = None
        if out:
            state._exec("UPDATE marketing_plays SET title = ?, body = ?, link = ?, done_at = ? WHERE id = ?",
                        (out["title"][:300], out["body"][:4000], out.get("link") or "", state.now(), pid))
            ran.append(key)
        else:
            state._exec("DELETE FROM marketing_plays WHERE id = ?", (pid,))  # nothing to do today: not a play
    today = state.clock().date().isoformat()
    queued_today = int(state._one("SELECT COUNT(*) AS n FROM marketing_plays WHERE mode = 'draft' AND substr(created_at, 1, 10) = ?",
                                  (today,))["n"])
    room = max(0, int(cfg.marketing_drafts_per_day) - queued_today)
    candidates = [k for k, s in PLAYBOOK.items() if s["mode"] == "draft" and due(state, k)]
    board = scores(state)
    weights = [max(1.0, board[k]["score"]) for k in candidates]
    while candidates and room:
        key = rng.choices(candidates, weights=weights)[0]
        i = candidates.index(key)
        candidates.pop(i)
        weights.pop(i)
        pid = add_play(state, key, "draft", "queued")
        out = PLAYBOOK[key]["make"](tools, pid)
        if not out:
            state._exec("DELETE FROM marketing_plays WHERE id = ?", (pid,))
            skipped.append(key)
            continue
        state._exec("UPDATE marketing_plays SET title = ?, body = ?, link = ? WHERE id = ?",
                    (out["title"][:300], out["body"][:4000], out["link"], pid))
        drafted.append(key)
        room -= 1
    return {"ran": ran, "drafted": drafted, "skipped": skipped}


def queue(state: Any, limit: int = 20) -> list[dict[str, Any]]:
    ensure(state)
    return state._all("SELECT * FROM marketing_plays WHERE status = 'queued' ORDER BY id LIMIT ?", (limit,))


def mark(state: Any, play_id: int, status: str) -> bool:
    ensure(state)
    return bool(state._exec("UPDATE marketing_plays SET status = ?, done_at = ? WHERE id = ? AND status = 'queued'",
                            (status, state.now(), play_id)).rowcount)


# ------------------------------------------------------------------ Phase 173: calendar
def calendar(state: Any, days: int = 7) -> list[tuple[str, str]]:
    """[(date, strategy name)] for the coming week, from each strategy's rhythm."""
    start = state.clock().date()
    out = []
    for key, s in PLAYBOOK.items():
        if s["mode"] == "tracked" or key in paused(state):
            continue
        every = max(1.0, float(s["every_days"]))
        last = last_play(state, key)
        nxt = (datetime.fromisoformat(last).date() + timedelta(days=every)) if last else start
        nxt = max(nxt, start)
        while (nxt - start).days < days:
            out.append((nxt.isoformat(), s["name"] + (" (you post)" if s["mode"] == "draft" else "")))
            nxt += timedelta(days=every)
    return sorted(out)


# ------------------------------------------------------------------ Phase 174: report line
def describe(state: Any) -> str:
    ensure(state)
    since = (state.clock() - timedelta(days=1)).isoformat(timespec="seconds")
    ran = int(state._one("SELECT COUNT(*) AS n FROM marketing_plays WHERE mode = 'auto' AND created_at >= ?", (since,))["n"])
    waiting = queue(state, 100)
    board = [r for r in scores(state, 30).values() if r["revenue_cents"]]
    best = max(board, key=lambda r: r["revenue_cents"]) if board else None
    line = f"Marketing: {ran} automatic play(s) in the last day; {len(waiting)} draft(s) waiting for you"
    if waiting:
        line += f" (`automonetize marketing`, first: {waiting[0]['title'][:60]})"
    if best:
        line += f"; best strategy this month: {best['name']} (${best['revenue_cents'] / 100:,.2f})"
    return line + "."


class MarketingEngine(Strategy):
    name = "marketing_engine"
    tasks = ("plan_marketing",)

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        tools = ctx.tools
        state = tools.state
        if not tools.config.marketing_engine:
            return TaskResult(True, "marketing engine off", {})
        last = state.get("marketing_planned_at")
        if last and state.clock() - datetime.fromisoformat(last) < timedelta(hours=20):
            return TaskResult(True, f"marketing planned {last[:16]}", {})
        newly_paused = pause_losers(state)
        out = plan(tools)
        state.set("marketing_planned_at", state.now())
        if state.clock().weekday() == 0:
            state.set(CALENDAR, {"at": state.now(), "items": calendar(state)})
        summary = (f"marketing: ran {', '.join(out['ran']) or 'nothing'}; drafted {', '.join(out['drafted']) or 'nothing'}"
                   + (f"; paused {', '.join(newly_paused)} (no checkouts after {PAUSE_AFTER_PLAYS} plays)" if newly_paused else ""))
        return TaskResult(True, summary[:400], {"ran": len(out["ran"]), "drafted": len(out["drafted"])})
