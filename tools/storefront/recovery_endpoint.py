"""Self-service order recovery: ``POST /v1/orders/recover`` (public, no key).

A buyer who lost their download (or their API key) enters their email; everything they bought
is re-sent **to that address only**:

* datasets: the zip they bought, re-attached;
* dossiers: the PDF that was delivered (regenerated if the file is gone);
* dataset subscriptions: the current full dataset;
* API subscriptions: keys can't be re-sent (only their hash is stored), and rotating a key
  because someone typed an email into a form would let anyone break a customer's integration.
  So the email carries a one-time **confirm link** (24 h). Only a click from the mailbox owner
  issues a new key, emailed as usual, and retires the old one.

Harvesting and abuse:

* The response is identical whether or not the address has purchases (``202`` + the same text),
  and the work happens after responding, so neither the body nor the timing says who's a
  customer. The only thing it can do is email an address its own purchases.
* At most ``recovery_per_ip_hour`` (3) requests per client IP per hour → ``429`` with
  ``Retry-After``; and at most ``recovery_per_email_day`` (3) recovery emails per address per day
  (further requests still get the same 202, but nothing is sent), so nobody can mail-bomb a buyer.
* Addresses are validated strictly; malformed input is a ``400 invalid_parameter``.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta
from typing import Any, Callable

from strategies.lead_magnet import CaptureLimiter, normalize_email
from tools.dispatcher import MAX_ATTACHMENT_BYTES, Attachment, Email

GENERIC = "If that address has purchases with us, they're on their way to it now. Check your inbox (and spam) in a minute."
TOKEN_TTL_HOURS = 24


def _h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class RecoveryService:
    def __init__(self, tools: Any, limiter: CaptureLimiter | None = None):
        self.tools = tools
        self.limiter = limiter or CaptureLimiter(tools.config.recovery_per_ip_hour)

    # -- the public request ---------------------------------------------------------------
    def request(self, payload: dict[str, Any], ip: str) -> tuple[int, dict[str, Any], dict[str, str], Callable[[], Any] | None]:
        """Returns (status, body, headers, after)."""
        if not self.limiter.allow(ip):
            return 429, {"error": {"code": "rate_limited", "status": 429, "param": None,
                                   "message": "Too many recovery requests from this network. Try again in an hour."}}, \
                {"Retry-After": "3600"}, None
        raw = payload.get("email") if isinstance(payload, dict) else None
        email = normalize_email(raw if isinstance(raw, str) else None)
        if email is None:
            return 400, {"error": {"code": "invalid_parameter", "status": 400, "param": "email",
                                   "message": "email must be a valid address"}}, {}, None
        return 202, {"object": "recovery_request", "status": "accepted", "message": GENERIC}, {}, (lambda: self.dispatch(email))

    def _allowed_today(self, email: str) -> bool:
        state = self.tools.state
        key = f"recovery_sent:{_h(email)[:24]}"
        now = state.clock()
        recent = [t for t in (state.get(key) or []) if now - datetime.fromisoformat(t) < timedelta(days=1)]
        if len(recent) >= self.tools.config.recovery_per_email_day:
            return False
        state.set(key, recent + [now.isoformat(timespec="seconds")])
        return True

    # -- the work (after responding) ---------------------------------------------------------
    def purchases(self, email: str) -> dict[str, list[dict[str, Any]]]:
        state = self.tools.state
        orders = state._all("SELECT * FROM orders WHERE email = ? AND kind = 'one_off' AND status IN "
                            "('delivered', 'paid', 'delivering') ORDER BY id", (email,))
        subs = [s for s in state.list_subscribers(("active", "trialing", "past_due")) if (s.get("email") or "") == email]
        from api.auth import is_api_subscriber

        return {"orders": orders, "dataset_subs": [s for s in subs if not is_api_subscriber(state, s)],
                "api_subs": [s for s in subs if is_api_subscriber(state, s)]}

    def attachments_for(self, found: dict[str, list[dict[str, Any]]]) -> tuple[list[Attachment], list[str]]:
        from strategies.digital_asset_packager import ASSET_KIND
        from strategies.dossier_engine import delivered_file, produce

        tools = self.tools
        files: list[Attachment] = []
        notes: list[str] = []
        seen: set[str] = set()
        for order in found["orders"]:
            asset = tools.state.get_asset(order["asset_id"]) if order["asset_id"] else None
            if not asset:
                continue
            if asset["kind"] == "dossier":
                path = delivered_file(tools, order)
                meta = json.loads(order.get("meta") or "{}")
                if path is not None:
                    content, name = path.read_bytes(), path.name
                else:
                    made = produce(tools, meta.get("company_id", ""))
                    if made is None:
                        continue
                    content, name = made[1], f"{meta.get('company_id')}-dossier.pdf"
                if name not in seen:
                    files.append(Attachment(name, content, "application/pdf"))
                    notes.append(f"Executive dossier: {meta.get('company_id', 'company')} (order {order['order_id'][-8:]})")
                    seen.add(name)
            elif tools.files.exists(asset["path"]):
                path = tools.files.resolve(asset["path"])
                if path.name not in seen:
                    note = f"{asset['title']} (order {order['order_id'][-8:]})"
                    link = self._link_if_large(path, asset["path"], order.get("email") or "")
                    if link:
                        notes.append(f"{note}: too big to attach, download it here (7 days): {link}")
                    else:
                        files.append(Attachment(path.name, path.read_bytes()))
                        notes.append(note)
                    seen.add(path.name)
        for sub in found["dataset_subs"]:
            dataset = next((a for a in tools.state.list_assets() if a["kind"] == ASSET_KIND and a.get("niche") == sub.get("niche")), None)
            if dataset and tools.files.exists(dataset["path"]):
                path = tools.files.resolve(dataset["path"])
                if path.name not in seen:
                    files.append(Attachment(path.name, path.read_bytes()))
                    notes.append(f"Current dataset for your subscription ({sub.get('niche')})")
                    seen.add(path.name)
        return files, notes

    def _link_if_large(self, path: Any, rel: str, email: str) -> str:
        from tools import download_links
        from tools.dispatcher import MAX_ATTACHMENT_BYTES

        if path.stat().st_size <= MAX_ATTACHMENT_BYTES or not download_links.available(self.tools.config):
            return ""
        return download_links.issue(self.tools.state, self.tools.config, rel, email)

    def issue_token(self, email: str) -> str:
        token = secrets.token_urlsafe(24)
        now = self.tools.state.clock()
        self.tools.state._exec("INSERT INTO recovery_tokens (token_hash, email, purpose, created_at, expires_at) VALUES (?,?,?,?,?)",
                               (_h(token), email, "api_key", now.isoformat(timespec="seconds"),
                                (now + timedelta(hours=TOKEN_TTL_HOURS)).isoformat(timespec="seconds")))
        return token

    def dispatch(self, email: str) -> dict[str, Any]:
        tools = self.tools
        found = self.purchases(email)
        if not any(found.values()):
            return {"sent": False, "reason": "no purchases"}  # nothing is emailed: no unsolicited mail
        if not self._allowed_today(email):
            return {"sent": False, "reason": "daily limit"}
        files, notes = self.attachments_for(found)
        lines = ["Hi,", "", "Here's everything you bought with us, as requested:", ""] + [f"- {n}" for n in notes]
        if found["api_subs"]:
            link = f"{tools.config.lead_capture_base}/v1/orders/recover/confirm?t={self.issue_token(email)}"
            lines += ["", "Developer API: we never store keys in readable form, so we can't re-send yours. To get a new key "
                      "(the old one stops working), open this link within 24 hours and confirm:", link,
                      "", "If you still have your key, ignore this; nothing changes unless you confirm."]
        lines += ["", "Didn't ask for this? Someone typed your address into our order-recovery form. Nothing was changed.",
                  "", tools.config.sender_name or "AutoMonetize"]
        body = "\n".join(lines)
        batches, current, size = [], [], 0
        for f in files:  # keep each email under the attachment limit
            if current and size + len(f.content) > MAX_ATTACHMENT_BYTES:
                batches.append(current)
                current, size = [], 0
            current.append(f)
            size += len(f.content)
        batches.append(current)
        outcomes = []
        for i, batch in enumerate(batches, 1):
            subject = "Your purchases" + (f" ({i}/{len(batches)})" if len(batches) > 1 else "")
            outcomes.append(tools.dispatcher.send_transactional(
                Email(to=email, subject=subject, body=body, kind="delivery", attachments=batch),
                audit_key=f"recovery:{_h(email)[:16]}:{tools.state.now()}:{i}"))
        tools.state.log_action(int(tools.state.get("iteration", 0)), None, "orders:recover", "ok",
                               f"{len(files)} file(s), {len(found['api_subs'])} API subscription(s) for {_h(email)[:8]}")
        return {"sent": True, "files": len(files), "api_links": len(found["api_subs"]), "outcomes": outcomes}

    # -- API key reissue (confirm link) ---------------------------------------------------------
    def token_row(self, token: str) -> dict[str, Any] | None:
        row = self.tools.state._one("SELECT * FROM recovery_tokens WHERE token_hash = ?", (_h(token or ""),))
        if not row or row["used_at"] or self.tools.state.clock() > datetime.fromisoformat(row["expires_at"]):
            return None
        return row

    def confirm(self, token: str) -> int:
        """Issue new keys for the token's address (retiring the old). Returns how many."""
        from api.auth import ApiKeys, send_welcome

        row = self.token_row(token)
        if row is None:
            return -1
        state = self.tools.state
        state._exec("UPDATE recovery_tokens SET used_at = ? WHERE token_hash = ? AND used_at IS NULL", (state.now(), row["token_hash"]))
        keys = ApiKeys(state, self.tools.config)
        issued = 0
        for sub in self.purchases(row["email"])["api_subs"]:
            old = keys.for_subscriber(sub["id"])
            status = old[0]["status"] if old else "active"
            for k in old:
                keys.set_status(k["id"], "rotated", "reissued via order recovery")
            key, new = keys.issue(sub["id"], sub.get("email"), status)
            send_welcome(self.tools, sub, key, new, rotated=True)
            issued += 1
        return issued


def mount(app: Any, tools: Any, run: Callable[..., Any], client_ip: Callable[[Any], str], executor: Any, held: Callable[..., Any],
          pending: set) -> RecoveryService:
    import asyncio
    import html

    from aiohttp import web

    svc = RecoveryService(tools)

    def respond(status: int, body: dict[str, Any], headers: dict[str, str]) -> web.Response:
        return web.Response(status=status, text=json.dumps(body, sort_keys=True), content_type="application/json",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", **headers})

    async def recover(request: web.Request) -> web.Response:
        if request.content_length and request.content_length > 2048:
            return respond(413, {"error": {"code": "invalid_parameter", "status": 413, "param": None, "message": "payload too large"}}, {})
        try:
            payload = await request.json() if "json" in request.headers.get("Content-Type", "") else dict(await request.post())
        except (ValueError, UnicodeDecodeError):
            return respond(400, {"error": {"code": "invalid_parameter", "status": 400, "param": None,
                                           "message": "send JSON {\"email\": ...} or a form field email"}}, {})
        status, body, headers, after = svc.request(payload, client_ip(request))
        if after is not None:
            fut = asyncio.get_running_loop().run_in_executor(executor, held, after)
            pending.add(fut)
            fut.add_done_callback(pending.discard)
        return respond(status, body, headers)

    def page(title: str, text: str, form: str = "", status: int = 200) -> web.Response:
        return web.Response(status=status, content_type="text/html", headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"},
                            text=(f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                                  f"<title>{html.escape(title)}</title><body style='font-family:system-ui;max-width:560px;margin:3rem auto;"
                                  f"padding:0 1rem'><h1>{html.escape(title)}</h1><p>{html.escape(text)}</p>{form}</body>"))

    async def confirm(request: web.Request) -> web.Response:
        if request.method == "GET":  # a mail scanner's prefetch must not reissue anything
            token = request.query.get("t", "")[:100]
            if svc.token_row(token) is None:
                return page("Link expired", "This link was already used or has expired. Request a new one.", status=404)
            form = (f'<form method="post"><input type="hidden" name="t" value="{html.escape(token)}">'
                    '<button type="submit" style="padding:.6rem 1rem;font-size:1rem">Issue a new API key</button></form>')
            return page("Replace your API key", "This emails you a new key. Your current key stops working.", form)
        data = dict(await request.post())
        issued = await run(svc.confirm, str(data.get("t", ""))[:100])
        if issued < 0:
            return page("Link expired", "This link was already used or has expired.", status=404)
        return page("Done", f"{issued} new key(s) are on their way to your inbox." if issued else "No active API subscription found.")

    app.router.add_post("/v1/orders/recover", recover)
    app.router.add_get("/v1/orders/recover/confirm", confirm)
    app.router.add_post("/v1/orders/recover/confirm", confirm)
    return svc
