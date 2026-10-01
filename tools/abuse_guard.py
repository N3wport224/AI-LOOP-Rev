"""Public forms that bots can't wear down (Phases 345-349).

Used by the site's forms that post to the agent (request a dataset, leave your company out):

* **Phase 345, a honeypot:** each form has a hidden ``website`` field people never see or fill. A
  submission that fills it gets the normal "thanks" page and is quietly dropped.
* **Phase 346, no links in free text:** a request or note containing a web address or an email
  address is turned away with a plain message ("describe it in words"): that's where spam lives.
* **Phase 347, one budget for all forms:** besides each form's own limit, one network may send at
  most ``GLOBAL_PER_HOUR`` (20) form posts an hour in total.
* **Phase 348, persistent offenders wait:** a network that collects ``BLOCK_AFTER`` (10) turned-away
  posts in an hour is blocked from the forms for ``BLOCK_HOURS`` (24) hours.
* **Phase 349, counted:** turned-away posts are counted per day and reason (kv ``abuse_stats``, 14
  days) and doctor shows today's.

State lives in memory on the app (a restart resets the blocks); the daily counts are saved.
"""

from __future__ import annotations

import re
import threading
import time
from collections import defaultdict, deque
from typing import Any, Callable

from aiohttp import web

GLOBAL_PER_HOUR = 20
MAX_TRACKED = 10_000
BLOCK_AFTER = 10
BLOCK_HOURS = 24
STATS = "abuse_stats"
HONEYPOT = "website"
HONEYPOT_HTML = ('<div style="position:absolute;left:-10000px" aria-hidden="true"><label>Leave this empty '
                 f'<input name="{HONEYPOT}" tabindex="-1" autocomplete="off"></label></div>')
_LINK = re.compile(r"https?://|www\.|\b[\w-]+\.(?:com|net|org|io|ru|cn|xyz|top|info|biz)\b|[\w.+-]+@[\w-]+\.\w+", re.I)


class Guard:
    def __init__(self, state: Any = None, clock: Callable[[], float] = time.monotonic):
        self.state, self.clock = state, clock
        self._posts: dict[str, deque[float]] = defaultdict(deque)
        self._strikes: dict[str, deque[float]] = defaultdict(deque)
        self._blocked: dict[str, float] = {}
        self._lock = threading.Lock()

    def _window(self, q: deque[float], now: float) -> deque[float]:
        while q and now - q[0] > 3600:
            q.popleft()
        return q

    def _count(self, reason: str) -> None:
        if self.state is None:
            return
        try:
            stats = dict(self.state.get(STATS) or {})
            day = self.state.now()[:10]
            stats.setdefault(day, {})
            stats[day][reason] = int(stats[day].get(reason) or 0) + 1
            self.state.set(STATS, {d: stats[d] for d in sorted(stats)[-14:]})
        except Exception:  # noqa: BLE001 - counting must never break a form
            pass

    def strike(self, ip: str, reason: str) -> None:
        """Phase 348: record a turned-away post; enough of them block the network."""
        now = self.clock()
        with self._lock:
            q = self._window(self._strikes[ip], now)
            q.append(now)
            if len(q) >= BLOCK_AFTER:
                self._blocked[ip] = now + BLOCK_HOURS * 3600
            if len(self._strikes) > MAX_TRACKED:  # bound memory under a flood from many networks
                for key in [k for k, v in self._strikes.items() if not self._window(v, now)][: MAX_TRACKED // 2]:
                    del self._strikes[key]
                if len(self._strikes) > MAX_TRACKED:  # all recent: keep the most recent half
                    keep = sorted(self._strikes, key=lambda k: self._strikes[k][-1], reverse=True)[: MAX_TRACKED // 2]
                    self._strikes = defaultdict(deque, {k: self._strikes[k] for k in keep})
            if len(self._blocked) > MAX_TRACKED:
                for key in [k for k, until in self._blocked.items() if until <= now][: MAX_TRACKED // 2]:
                    del self._blocked[key]
                while len(self._blocked) > MAX_TRACKED:  # still full: the oldest blocks go first
                    self._blocked.pop(min(self._blocked, key=self._blocked.get))
        self._count(reason)

    def check(self, ip: str, data: dict[str, Any], text_fields: tuple[str, ...] = ()) -> str | None:
        """None when the post may go ahead; otherwise why not: "blocked", "busy", "honeypot" or "links"."""
        now = self.clock()
        with self._lock:
            until = self._blocked.get(ip)
            if until and now < until:
                blocked = True
            else:
                self._blocked.pop(ip, None)
                blocked = False
                q = self._window(self._posts[ip], now)
                busy = len(q) >= GLOBAL_PER_HOUR
                if not busy:
                    q.append(now)
            if len(self._posts) > MAX_TRACKED:
                for key in [k for k, v in self._posts.items() if not v][: MAX_TRACKED // 2]:
                    del self._posts[key]
        if blocked:
            self._count("blocked")
            return "blocked"
        if busy:  # Phase 347
            self.strike(ip, "busy")
            return "busy"
        if str(data.get(HONEYPOT) or "").strip():  # Phase 345
            self.strike(ip, "honeypot")
            return "honeypot"
        if any(_LINK.search(str(data.get(f) or "")) for f in text_fields):  # Phase 346
            self.strike(ip, "links")
            return "links"
        return None


# Made at import time: aiohttp names an AppKey after the module that creates it, by walking the
# stack for module-level code, which a server thread (the webhook listener) doesn't have.
GUARD_KEY = web.AppKey("abuse_guard", Guard)


def for_app(app: Any, state: Any) -> Guard:
    """One guard per web app, shared by its forms."""
    guard = app.get(GUARD_KEY)
    if guard is None:
        guard = Guard(state)
        app[GUARD_KEY] = guard
    return guard


def today(state: Any) -> dict[str, int]:
    return dict((state.get(STATS) or {}).get(state.now()[:10]) or {})
