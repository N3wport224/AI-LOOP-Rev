"""Buyer experience (Phases 205-209).

* **Phase 205, my library:** ``library/`` on the site: a buyer types their email and every dataset
  they bought is re-sent to that address (the existing order-recovery endpoint, with its limits:
  3 requests per network per hour, 3 emails per address per day, and the same answer whether or
  not the address bought anything). A form post now gets a plain page back instead of JSON.
* **Phase 206, free sample download:** every product page links ``sample.csv``, the same 5 public
  rows shown on the page, as a file to open in a spreadsheet.
* **Phase 207, real ratings on pages:** once a product has ``RATINGS_MIN`` (3) ratings from
  verified buyers (the 1-click ratings, Phase 101), its page shows the counts (😀 / 😐 / ☹️) and
  carries ``AggregateRating`` data (scale 1-3). Nothing is shown below that, and nothing is ever
  made up.
* **Phase 208, request a dataset:** ``request/`` on the site posts to ``/v1/requests`` (through the
  tunnel; ``REQUESTS_PER_IP_HOUR`` per network). The request feeds the factory's demand ranking (the
  same signal as customer emails). With "email me when it's ready" ticked, the address is kept only
  for that one notice, sent when a matching product goes on sale, then forgotten.
* **Phase 209, what's in the download:** each factory product page lists the files in the zip.
"""

from __future__ import annotations

import csv
import html
import io
from collections import Counter
from datetime import timedelta
from typing import Any

from strategies.base import Strategy, TaskContext, TaskResult

RATINGS_MIN = 3
REQUESTS_PER_IP_HOUR = 5
WATCH = "request_watchers"
WATCH_DAYS = 90
FILE_LIST = {"slice": ["leads.csv", "leads-excel.csv", "leads.json", "leads.jsonl", "schema.sql", "QUALITY.md", "FIELDS.md",
                       "TOP20.md", "README.md"],
             "salary": ["salaries.csv", "SALARIES.md", "README.md"],
             "top": ["companies.csv", "companies.json", "companies.jsonl", "schema.sql", "README.md"],
             "remote_first": ["companies.csv", "companies.json", "companies.jsonl", "schema.sql", "README.md"],
             "pack": ["one folder per dataset", "README.md"]}


# ------------------------------------------------------------------ Phase 205
def library_page(cfg: Any, shell: Any) -> str:
    base = str(cfg.lead_capture_base or "")
    if base:
        form = (f'<form method="post" action="{html.escape(base)}/v1/orders/recover"><label for="lib-email">Your email</label> '
                '<input id="lib-email" type="email" name="email" required maxlength="254" autocomplete="email"> '
                '<button type="submit">Send my purchases</button></form>')
    else:
        contact = html.escape(cfg.sender_email or "")
        form = f'<p>Email <a href="mailto:{contact}">{contact}</a> and they\'ll be re-sent.</p>' if contact else ""
    body = ("<h1>Your library</h1><p>Lost a download? Enter the email you bought with and everything you bought is sent to "
            "that address again, with fresh links. Nothing is shown here: it only ever goes to the buyer's inbox.</p>" + form)
    return shell("Your library", body, "Get every dataset you bought sent to your inbox again: enter the email you used at "
                                       "checkout.")


def recovery_page(status: int, message: str, title: str = "") -> str:
    title = title or {202: "Check your inbox", 429: "Please try again later"}.get(status, "Something's not right")
    return (f"<!doctype html><html lang=\"en\"><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            f"<title>{html.escape(title)}</title><body style='font-family:system-ui;max-width:560px;margin:3rem auto;padding:0 1rem'>"
            f"<h1>{html.escape(title)}</h1><p>{html.escape(message)}</p></body></html>")


# ------------------------------------------------------------------ Phase 206
def sample_csv(columns: list[str], rows: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({c: ", ".join(map(str, r[c])) if isinstance(r.get(c), list) else r.get(c) for c in columns})
    return buf.getvalue()


# ------------------------------------------------------------------ Phase 207
def ratings_for(state: Any, niche: str) -> Counter:
    from strategies.ratings import ensure

    ensure(state)
    rows = state._all("SELECT r.score AS score FROM ratings r JOIN orders o ON o.id = r.order_pk JOIN assets a ON a.id = o.asset_id "
                      "WHERE a.niche = ? AND r.score IS NOT NULL", (niche,))
    return Counter(int(r["score"]) for r in rows)


def ratings_html(counts: Counter) -> str:
    n = sum(counts.values())
    if n < RATINGS_MIN:
        return ""
    return (f'<p class="proof">Rated by {n} verified buyers: 😀 {counts.get(3, 0)} · 😐 {counts.get(2, 0)} · '
            f"☹️ {counts.get(1, 0)}</p>")


def aggregate_rating(counts: Counter) -> dict[str, Any] | None:
    n = sum(counts.values())
    if n < RATINGS_MIN:
        return None
    avg = sum(score * c for score, c in counts.items()) / n
    return {"@type": "AggregateRating", "ratingValue": f"{avg:.2f}", "bestRating": "3", "worstRating": "1", "ratingCount": n}


# ------------------------------------------------------------------ Phase 208
def request_page(cfg: Any, shell: Any) -> str:
    base = str(cfg.lead_capture_base or "")
    if not base:
        return ""
    body = ("<h1>Request a dataset</h1><p>Missing a technology, region or seniority? Tell us. Requests decide what gets built "
            "next.</p>"
            f'<form method="post" action="{html.escape(base)}/v1/requests"><p><label for="req-text">What would you like?</label><br>'
            '<input id="req-text" name="request" required maxlength="200" placeholder="e.g. senior Elixir roles in Europe" '
            'style="width:100%"></p><p><label for="req-email">Email (optional)</label> '
            '<input id="req-email" type="email" name="email" maxlength="254" autocomplete="email"></p>'
            '<p><label><input type="checkbox" name="notify" value="1"> Email me once when it\'s on sale (used for that '
            "one email only)</label></p><button type=\"submit\">Send request</button></form>")
    return shell("Request a dataset", body, "Ask for a hiring dataset by technology, region or seniority: requests decide what "
                                            "gets built next.")


def record_request(state: Any, cfg: Any, text: str, email: str = "", notify: bool = False) -> dict[str, Any]:
    from collections import Counter as C

    from strategies.customer_requests import KEY, TOPICS, topics
    from strategies.revenue_models import parse_request

    text = " ".join(str(text or "").split())[:200]
    if len(text) < 3:
        raise ValueError("tell us what you'd like")
    found = topics(cfg, text)
    wanted = parse_request(text)
    if wanted["tech"] and wanted["tech"] not in found:
        found.append(wanted["tech"])
    items = list(state.get(KEY) or [])
    items.append({"at": state.now(), "email": "", "text": text, "topics": found, "via": "site"})
    state.set(KEY, items[-200:])
    counts = C(state.get(TOPICS) or {})
    counts.update(found)
    state.set(TOPICS, dict(counts))
    watching = False
    if notify and email and wanted["tech"]:
        from strategies.lead_magnet import normalize_email

        addr = normalize_email(email)
        if addr:
            watchers = list(state.get(WATCH) or [])
            watchers.append({"email": addr, "wanted": wanted, "at": state.now()})
            state.set(WATCH, watchers[-500:])
            watching = True
    return {"topics": found, "watching": watching}


def notify_watchers(tools: Any) -> int:
    """One email per request when a matching product is on sale; requests older than 90 days lapse."""
    from strategies.revenue_models import live_products
    from strategies.upsells import Index
    from tools.dispatcher import Email

    state, cfg = tools.state, tools.config
    watchers = list(state.get(WATCH) or [])
    if not watchers:
        return 0
    index = Index(state, cfg)
    cutoff = (state.clock() - timedelta(days=WATCH_DAYS)).isoformat(timespec="seconds")
    keep, sent = [], 0
    live = [a for a in live_products(state) if a.get("checkout_url")]
    for w in watchers:
        if w["at"] < cutoff:
            continue  # lapsed: the address is forgotten
        match = next((a for a in live if index.tech.get(str(a.get("niche") or "")) == w["wanted"]["tech"]
                      and a["created_at"] >= w["at"]), None)
        if not match:
            keep.append(w)
            continue
        from strategies.marketing_engine import product_url

        tools.dispatcher.send_transactional(Email(
            to=w["email"], subject=f"It's ready: {match['title']}", kind="delivery",
            body=(f"You asked for this dataset, and it's on sale now:\n\n{match['title']}: {product_url(cfg, match)}\n\n"
                  "This is the only email we send for that request; your address isn't kept.")),
            audit_key=f"request-ready:{w['email']}:{match['niche']}")
        sent += 1
    state.set(WATCH, keep)
    return sent


class BuyerExperience(Strategy):
    name = "buyer_experience"
    tasks = ("notify_requests",)

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        sent = notify_watchers(ctx.tools)
        return TaskResult(True, f"requests: {sent} \"it's ready\" email(s)", {"sent": sent})


def mount(app: Any, tools: Any, run: Any, client_ip: Any, limiter: Any) -> None:
    from aiohttp import web

    async def request_handler(request: web.Request) -> web.Response:
        if request.content_length and request.content_length > 2048:
            return web.Response(status=413, text="too large")
        if not limiter.allow(client_ip(request)):
            return web.Response(status=429, text=recovery_page(429, "Too many requests from this network. Try again in an hour."),
                                content_type="text/html")
        data = dict(await request.post()) if "json" not in request.headers.get("Content-Type", "") else await request.json()
        try:
            out = await run(record_request, tools.state, tools.config, str(data.get("request") or ""), str(data.get("email") or ""),
                            str(data.get("notify") or "") in ("1", "on", "true"))
        except ValueError as exc:
            return web.Response(status=400, text=recovery_page(400, str(exc)), content_type="text/html")
        note = " We'll email you once when it's on sale." if out["watching"] else ""
        return web.Response(text=recovery_page(202, "Thanks! Your request counts towards what gets built next." + note,
                                               "Request received"), content_type="text/html", headers={"Cache-Control": "no-store"})

    app.router.add_post("/v1/requests", request_handler)


def file_list_html(kind: str) -> str:
    files = FILE_LIST.get(kind) or []
    if not files:
        return ""
    return "<p class=\"muted\"><b>In the download:</b> " + ", ".join(html.escape(f) for f in files) + "</p>"

