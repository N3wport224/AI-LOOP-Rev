"""Companies can ask to be left out (Phases 285-289).

The datasets are built from public job postings, but a company may still prefer not to appear. This
gives it a simple, respectful way to say so.

* **Phase 285, the request form:** ``remove/`` on the site posts to ``/v1/optout`` (rate-limited per
  network, ``REQUESTS_PER_IP_HOUR``): company name, a work email, and an optional note. Without a public
  address the page gives your email instead.
* **Phase 286, you decide:** each request waits for you (``automonetize optout`` lists them;
  ``approve ID`` / ``reject ID``) and raises one alert. The list shows whether the email's domain looks
  like the company's, so you can spot a request made on someone else's behalf.
* **Phase 287, left out everywhere:** an approved company's postings are dropped from every new
  product and every weekly refresh (``excluded``), so it disappears from the catalog within a week.
* **Phase 288, confirmation:** on approval the requester gets one email saying it's done (and when).
* **Phase 289, said in the privacy policy:** the privacy page explains that companies can ask, and
  links the form.
"""

from __future__ import annotations

import html
import re
import secrets
from typing import Any

REQUESTS = "optout_requests"
EXCLUDED = "optout_companies"
REQUESTS_PER_IP_HOUR = 3
PAGE = "remove/index.html"


def _key(name: str) -> str:
    from strategies.posting_quality import company_key

    return company_key(name)


# ------------------------------------------------------------------ Phase 285
def remove_page(cfg: Any, shell: Any) -> str:
    base = str(cfg.lead_capture_base or "")
    intro = ("<h1>Leave your company out</h1><p>Our datasets are built from public job postings. If your company would "
             "rather not appear, tell us and, once we've checked the request, its postings are left out of every dataset "
             "from the next weekly update.</p>")
    if base:
        form = (f'<form method="post" action="{html.escape(base)}/v1/optout"><p><label for="oo-company">Company name</label><br>'
                '<input id="oo-company" name="company" required maxlength="120" style="width:100%"></p>'
                '<p><label for="oo-email">Your work email (so we can confirm)</label><br>'
                '<input id="oo-email" type="email" name="email" required maxlength="254" autocomplete="email" style="width:100%"></p>'
                '<p><label for="oo-note">Anything we should know (optional)</label><br>'
                '<textarea id="oo-note" name="note" maxlength="500" rows="3" style="width:100%"></textarea></p>'
                '<button type="submit">Send request</button></form>')
    else:
        contact = html.escape(cfg.sender_email or cfg.owner_email or "")
        form = f'<p>Email <a href="mailto:{contact}">{contact}</a> from your work address with the company name.</p>' if contact else ""
    return shell("Leave your company out", intro + form, "Ask for your company's job postings to be left out of our datasets.")


def record(state: Any, company: str, email: str, note: str = "") -> dict[str, Any]:
    from strategies.lead_magnet import normalize_email

    company = " ".join(str(company or "").split())[:120]
    addr = normalize_email(email or "")
    if len(company) < 2 or not _key(company):
        raise ValueError("Please give the company's name.")
    if not addr:
        raise ValueError("Please give a valid work email, so we can confirm.")
    items = list(state.get(REQUESTS) or [])
    if any(r["company_key"] == _key(company) and r["status"] == "pending" for r in items):
        return next(r for r in items if r["company_key"] == _key(company) and r["status"] == "pending")
    entry = {"id": secrets.token_hex(3), "company": company, "company_key": _key(company), "email": addr,
             "note": " ".join(str(note or "").split())[:500], "status": "pending", "at": state.now(),
             "domain_matches": domain_matches(company, addr)}
    state.set(REQUESTS, (items + [entry])[-500:])
    state.log_error("opt_out", f"{company} asked to be left out of the datasets ({addr}). Review: automonetize optout",
                    kind="alert")
    return entry


# ------------------------------------------------------------------ Phase 286
def domain_matches(company: str, email: str) -> bool:
    domain = email.rsplit("@", 1)[-1].lower()
    name = re.sub(r"[^a-z0-9]", "", _key(company))
    first = re.sub(r"[^a-z0-9]", "", domain.split(".")[0])
    return bool(name) and (name in first or first in name) and len(first) >= 3


def pending(state: Any) -> list[dict[str, Any]]:
    return [r for r in state.get(REQUESTS) or [] if r["status"] == "pending"]


def decide(tools: Any, request_id: str, approve: bool) -> dict[str, Any]:
    state = tools.state
    items = list(state.get(REQUESTS) or [])
    entry = next((r for r in items if r["id"] == request_id), None)
    if entry is None or entry["status"] != "pending":
        raise ValueError(f"no pending request {request_id!r}")
    entry["status"], entry["decided_at"] = ("approved" if approve else "rejected"), state.now()
    state.set(REQUESTS, items)
    if approve:
        excluded_now = set(state.get(EXCLUDED) or [])
        excluded_now.add(entry["company_key"])
        state.set(EXCLUDED, sorted(excluded_now))
        _confirm(tools, entry)  # Phase 288
    return entry


# ------------------------------------------------------------------ Phase 287
def excluded(state: Any) -> set[str]:
    return set(state.get(EXCLUDED) or [])


def drop_excluded(state: Any, leads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    gone = excluded(state)
    if not gone:
        return leads
    return [lead for lead in leads if _key(lead.get("company") or "") not in gone]


# ------------------------------------------------------------------ Phase 288
def _confirm(tools: Any, entry: dict[str, Any]) -> None:
    from tools.dispatcher import Email

    tools.dispatcher.send_transactional(Email(
        to=entry["email"], subject=f"{entry['company']} is being left out of our datasets", kind="delivery",
        body=(f"Hello,\n\nAs you asked, {entry['company']}'s job postings are left out of our datasets from now on. Products "
              "already on sale are updated at their next weekly refresh, so within 7 days nothing of yours remains.\n\n"
              "Thank you for letting us know.\n")), audit_key=f"optout:{entry['id']}")


# ------------------------------------------------------------------ the endpoint
def mount(app: Any, tools: Any, run: Any, client_ip: Any, limiter: Any) -> None:
    from aiohttp import web

    from strategies.buyer_experience import recovery_page

    async def handler(request: web.Request) -> web.Response:
        if request.content_length and request.content_length > 4096:
            return web.Response(status=413, text="too large")
        if not limiter.allow(client_ip(request)):
            return web.Response(status=429, text=recovery_page(429, "Too many requests from this network. Try again in an hour."),
                                content_type="text/html")
        try:
            data = dict(await request.post()) if "json" not in request.headers.get("Content-Type", "") else await request.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            return web.Response(status=400, text=recovery_page(400, "Send the form."), content_type="text/html")
        try:
            await run(record, tools.state, str(data.get("company") or ""), str(data.get("email") or ""), str(data.get("note") or ""))
        except ValueError as exc:
            return web.Response(status=400, text=recovery_page(400, str(exc)), content_type="text/html")
        return web.Response(status=202, text=recovery_page(202, "Thanks. We'll check the request and email you once it's done.",
                                               "Request received"), content_type="text/html", headers={"Cache-Control": "no-store"})

    app.router.add_post("/v1/optout", handler)
