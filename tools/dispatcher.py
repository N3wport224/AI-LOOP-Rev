"""Email dispatch for approved outreach and for order delivery.

Safety model:

* **Dry run by default.** Until ``dry_run = false`` (or ``DRY_RUN=false``), nothing leaves the
  machine. Every payload that *would* be sent is appended to ``data/dispatched_audit.log`` (JSON lines).
* **Approval gate.** Cold outreach is only ever taken from ``outreach_queue`` rows a human marked
  ``approved``. Order delivery is transactional and is sent without review.
* **Domain warm-up.** Live cold email is capped at ``warmup_start_per_day`` (5) per day, rising by
  ``warmup_step_per_week`` each full week after the first send, up to ``dispatch_max_per_day``.
* **CAN-SPAM.** Live cold email refuses to run without a sender identity, a physical postal address
  and a working unsubscribe address. Every message carries ``List-Unsubscribe`` (plus RFC 8058
  one-click when an https unsubscribe URL is configured), a footer with the postal address and
  opt-out instructions, and the subject the reviewer approved. Suppressed recipients are never
  contacted again, and neither are recipients on blocked country TLDs (EU/UK by default, where
  unsolicited B2B email generally needs prior consent).
"""

from __future__ import annotations

import base64
import json
import re
import smtplib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Protocol

if TYPE_CHECKING:  # pragma: no cover
    from agent.config import Config
    from agent.state import StateStore
    from tools.http_client import HttpClient

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
AUDIT_FILE = "dispatched_audit.log"
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024


@dataclass
class Attachment:
    filename: str
    content: bytes
    mimetype: str = "application/zip"


@dataclass
class Email:
    to: str
    subject: str
    body: str
    kind: str  # outreach | delivery
    headers: dict[str, str] = field(default_factory=dict)
    attachments: list[Attachment] = field(default_factory=list)


@dataclass
class DispatchReport:
    sent: int = 0
    dry_run: int = 0
    blocked: int = 0
    failed: int = 0
    deferred: int = 0
    limit: int = 0
    reasons: list[str] = field(default_factory=list)


class EmailBackend(Protocol):
    name: str

    def configured(self) -> bool: ...

    def send(self, msg: Email, sender_email: str, sender_name: str) -> str: ...


class SMTPBackend:
    name = "smtp"

    def __init__(self, config: "Config", smtp_factory: Callable[..., Any] = smtplib.SMTP):
        self.config = config
        self.smtp_factory = smtp_factory

    def configured(self) -> bool:
        return bool(self.config.smtp_host)

    def send(self, msg: Email, sender_email: str, sender_name: str) -> str:
        em = EmailMessage()
        em["From"] = formataddr((sender_name, sender_email)) if sender_name else sender_email
        em["To"] = msg.to
        em["Subject"] = msg.subject
        message_id = make_msgid(domain=sender_email.split("@")[-1] if "@" in sender_email else None)
        em["Message-ID"] = message_id
        for k, v in msg.headers.items():
            em[k] = v
        em.set_content(msg.body)
        for a in msg.attachments:
            maintype, _, subtype = a.mimetype.partition("/")
            em.add_attachment(a.content, maintype=maintype, subtype=subtype or "octet-stream", filename=a.filename)
        cfg = self.config
        factory = self.smtp_factory
        if cfg.smtp_port == 465 and factory is smtplib.SMTP:
            factory = smtplib.SMTP_SSL
        with factory(cfg.smtp_host, cfg.smtp_port, timeout=30) as smtp:
            smtp.ehlo()
            if cfg.smtp_port != 465:
                smtp.starttls()
                smtp.ehlo()
            if cfg.smtp_username:
                smtp.login(cfg.smtp_username, cfg.smtp_password)
            smtp.send_message(em)
        return message_id


class SendGridBackend:
    name = "sendgrid"
    URL = "https://api.sendgrid.com/v3/mail/send"

    def __init__(self, http: "HttpClient", api_key: str):
        self.http, self.api_key = http, api_key

    def configured(self) -> bool:
        return bool(self.api_key)

    @staticmethod
    def payload(msg: Email, sender_email: str, sender_name: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "personalizations": [{"to": [{"email": msg.to}]}],
            "from": {"email": sender_email, **({"name": sender_name} if sender_name else {})},
            "subject": msg.subject,
            "content": [{"type": "text/plain", "value": msg.body}],
        }
        if msg.headers:
            body["headers"] = dict(msg.headers)
        if msg.attachments:
            body["attachments"] = [
                {"content": base64.b64encode(a.content).decode(), "filename": a.filename, "type": a.mimetype, "disposition": "attachment"}
                for a in msg.attachments
            ]
        return body

    def send(self, msg: Email, sender_email: str, sender_name: str) -> str:
        resp = self.http.post(
            self.URL, json_body=self.payload(msg, sender_email, sender_name),
            headers={"Authorization": f"Bearer {self.api_key}"}, check_robots=False,
        )
        return resp.headers.get("x-message-id", "sendgrid-accepted")


class PostmarkBackend:
    name = "postmark"
    URL = "https://api.postmarkapp.com/email"

    def __init__(self, http: "HttpClient", token: str):
        self.http, self.token = http, token

    def configured(self) -> bool:
        return bool(self.token)

    @staticmethod
    def payload(msg: Email, sender_email: str, sender_name: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "From": formataddr((sender_name, sender_email)) if sender_name else sender_email,
            "To": msg.to,
            "Subject": msg.subject,
            "TextBody": msg.body,
            # Postmark separates transactional and bulk traffic into message streams.
            "MessageStream": "outbound" if msg.kind == "delivery" else "broadcast",
        }
        if msg.headers:
            body["Headers"] = [{"Name": k, "Value": v} for k, v in msg.headers.items()]
        if msg.attachments:
            body["Attachments"] = [
                {"Name": a.filename, "Content": base64.b64encode(a.content).decode(), "ContentType": a.mimetype}
                for a in msg.attachments
            ]
        return body

    def send(self, msg: Email, sender_email: str, sender_name: str) -> str:
        resp = self.http.post(
            self.URL, json_body=self.payload(msg, sender_email, sender_name),
            headers={"X-Postmark-Server-Token": self.token, "Accept": "application/json"}, check_robots=False,
        )
        return str((resp.json() or {}).get("MessageID", "postmark-accepted"))


def build_backends(config: "Config", http: "HttpClient", smtp_factory: Callable[..., Any] = smtplib.SMTP) -> dict[str, EmailBackend]:
    return {
        "smtp": SMTPBackend(config, smtp_factory),
        "sendgrid": SendGridBackend(http, config.sendgrid_api_key),
        "postmark": PostmarkBackend(http, config.postmark_server_token),
    }


class Dispatcher:
    def __init__(self, config: "Config", state: "StateStore", backends: dict[str, EmailBackend]):
        self.config = config
        self.state = state
        self.backends = backends

    # -- configuration --------------------------------------------------------------
    @property
    def live(self) -> bool:
        return not self.config.dry_run

    @property
    def audit_path(self) -> Path:
        return self.config.data_dir / AUDIT_FILE

    def backend_for(self, kind: str) -> EmailBackend | None:
        name = self.config.outreach_email_backend if kind == "outreach" else (self.config.email_backend or self.config.outreach_email_backend)
        backend = self.backends.get(name or "")
        return backend if backend is not None and backend.configured() else None

    def unsubscribe_address(self) -> str:
        return self.config.unsubscribe_email or self.config.sender_email

    def compliance_problems(self, kind: str = "outreach") -> list[str]:
        cfg = self.config
        problems = []
        if not cfg.sender_email or not EMAIL_RE.match(cfg.sender_email):
            problems.append("sender_email is not set")
        if self.backend_for(kind) is None:
            problems.append(f"no configured email backend for {kind}")
        if kind == "outreach":
            if not cfg.sender_name:
                problems.append("sender_name is not set")
            if not cfg.sender_postal_address.strip():
                problems.append("sender_postal_address is required by CAN-SPAM")
            if not self.unsubscribe_address() and not cfg.unsubscribe_url:
                problems.append("an unsubscribe email or URL is required")
        return problems

    def can_deliver(self) -> bool:
        return self.live and not self.compliance_problems("delivery")

    # -- warm-up ------------------------------------------------------------------------
    def daily_limit(self, now: datetime | None = None) -> int:
        cfg = self.config
        now = now or self.state.clock()
        first = self.state.first_send_at()
        weeks = 0
        if first:
            weeks = max(0, (now - datetime.fromisoformat(first)).days // 7)
        return max(0, min(cfg.dispatch_max_per_day, cfg.warmup_start_per_day + cfg.warmup_step_per_week * weeks))

    def _day_start(self, now: datetime) -> datetime:
        return now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    def used_today(self, now: datetime) -> int:
        if self.live:
            return self.state.sent_since(self._day_start(now))
        return int(self.state.get(f"dry_run_count:{self._day_start(now).date()}", 0))

    # -- recipients ---------------------------------------------------------------------
    def recipient_block_reason(self, email: str) -> str | None:
        email = email.strip().lower()
        if not EMAIL_RE.match(email):
            return "invalid address"
        if self.state.is_suppressed(email):
            return "suppressed (unsubscribed)"
        tld = email.rsplit(".", 1)[-1]
        if tld in {t.lower().lstrip(".") for t in self.config.blocked_recipient_tlds}:
            return f"blocked TLD .{tld}"
        return None

    # -- composition --------------------------------------------------------------------
    def compose_outreach(self, row: dict[str, Any]) -> Email:
        cfg = self.config
        unsub = self.unsubscribe_address()
        targets = []
        if unsub:
            targets.append(f"<mailto:{unsub}?subject=unsubscribe>")
        if cfg.unsubscribe_url.startswith("https://"):
            targets.append(f"<{cfg.unsubscribe_url}>")
        headers = {"List-Unsubscribe": ", ".join(targets)}
        if cfg.unsubscribe_url.startswith("https://"):
            headers["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        footer = (
            "\n\n--\n"
            f"{cfg.sender_name}\n{cfg.sender_postal_address.strip()}\n"
            "You received this one-off note because this address was published in a public job listing. "
            f"Reply \"unsubscribe\"{f' or email {unsub}' if unsub else ''} and you won't hear from me again."
        )
        return Email(to=row["recipient"], subject=row["subject"], body=row["body"].rstrip() + footer, kind="outreach", headers=headers)

    def compose_delivery(self, to: str, title: str, zip_name: str, content: bytes, order: dict[str, Any] | None = None) -> Email:
        receipt = ""
        if order:
            receipt = (
                "\n\nReceipt\n-------\n"
                f"Order:   {order.get('provider', '')}:{order.get('order_id', '')}\n"
                f"Item:    {title}\n"
                f"Amount:  ${(order.get('gross_cents') or 0) / 100:.2f} {self.config.currency.upper()}\n"
                f"Date:    {order.get('occurred_at', '')}\n"
            )
        body = (
            f"Hi,\n\nThanks for buying {title}! Your dataset is attached ({zip_name}): CSV + JSON + the executive summary."
            f"{receipt}\n"
            "If anything is missing or wrong, just reply to this email.\n\n"
            f"{self.config.sender_name or 'AutoMonetize'}"
        )
        return Email(to=to, subject=f"Your download: {title}", body=body, kind="delivery",
                     attachments=[Attachment(zip_name, content)])

    # -- sending ------------------------------------------------------------------------
    def _audit(self, email: Email, mode: str, result: str, detail: str = "", backend: str = "") -> None:
        record = {
            "ts": self.state.now(),
            "mode": mode,
            "kind": email.kind,
            "backend": backend,
            "to": email.to,
            "subject": email.subject,
            "headers": email.headers,
            "body": email.body,
            "attachments": [{"filename": a.filename, "bytes": len(a.content)} for a in email.attachments],
            "result": result,
            "detail": detail,
        }
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.audit_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")

    def dispatch_approved(self, now: datetime | None = None, max_failures: int = 3) -> DispatchReport:
        now = now or self.state.clock()
        report = DispatchReport(limit=self.daily_limit(now))
        if self.live:
            problems = self.compliance_problems("outreach")
            if problems:
                report.reasons = problems
                return report
        backend = self.backend_for("outreach")
        budget = max(0, report.limit - self.used_today(now))
        for row in self.state.list_outreach("approved", limit=500):
            if row["channel"] != "email":
                continue
            if not self.live and row.get("dry_run_at"):
                continue  # already written to the audit log once
            reason = self.recipient_block_reason(row["recipient"])
            if reason:
                self.state.set_outreach_status(row["id"], "blocked")
                report.blocked += 1
                report.reasons.append(f"#{row['id']} {reason}")
                continue
            if budget <= 0:
                report.deferred += 1
                continue
            email = self.compose_outreach(row)
            if not self.live:
                self._audit(email, "dry_run", "not_sent", backend=backend.name if backend else "")
                self.state.mark_outreach_dry_run(row["id"])
                self.state.incr(f"dry_run_count:{self._day_start(now).date()}")
                report.dry_run += 1
                budget -= 1
                continue
            try:
                message_id = backend.send(email, self.config.sender_email, self.config.sender_name)  # type: ignore[union-attr]
            except Exception as exc:  # noqa: BLE001 - one bad message must not stop the audit trail
                self.state.record_send_failure(row["id"])
                self._audit(email, "live", "failed", repr(exc)[:500], backend.name)  # type: ignore[union-attr]
                self.state.log_error("dispatcher", f"send #{row['id']} failed: {exc!r}")
                report.failed += 1
                if report.failed >= max_failures:
                    report.reasons.append("stopped: repeated send failures (protecting sender reputation)")
                    break
                continue
            self.state.mark_outreach_sent(row["id"], message_id)
            self._audit(email, "live", "sent", message_id, backend.name)  # type: ignore[union-attr]
            report.sent += 1
            budget -= 1
        return report

    def deliver(self, order: dict[str, Any], title: str, zip_path: Path) -> str:
        """Email a purchased dataset. Returns ``delivered``, ``dry_run`` or ``failed``."""
        content = zip_path.read_bytes()
        if len(content) > MAX_ATTACHMENT_BYTES:
            raise ValueError(f"{zip_path.name} is too large to attach ({len(content)} bytes)")
        email = self.compose_delivery(order["email"], title, zip_path.name, content, order)
        backend = self.backend_for("delivery")
        if not self.live or backend is None:
            logged = set(self.state.get("delivery_dry_run_logged", []))
            if order["id"] not in logged:
                self._audit(email, "dry_run", "not_sent", "dry_run on or no email backend", backend.name if backend else "")
                self.state.set("delivery_dry_run_logged", sorted(logged | {order["id"]}))
            return "dry_run"
        try:
            message_id = backend.send(email, self.config.sender_email, self.config.sender_name)
        except Exception as exc:  # noqa: BLE001
            self._audit(email, "live", "failed", repr(exc)[:500], backend.name)
            raise
        self._audit(email, "live", "sent", message_id, backend.name)
        return "delivered"


def report_dict(report: DispatchReport) -> dict[str, Any]:
    return asdict(report)

