"""Free lead magnet: a 10-record sample for an email address, then a weekly nurture email.

Flow:

1. **Capture**: a visitor submits the form on a lander or matrix page. It posts through the
   Cloudflare tunnel to ``/lead-magnet/capture`` on the local listener (the site itself is
   static). The address is stored in ``subscribers`` with ``tier="free"``, and never counts as
   revenue, MRR or traction.
2. **Sample**: straight away, the address gets a 10-record sample (CSV, the highest-intent
   companies) and a Markdown hiring-intent cheatsheet, through the dispatcher (dry-run aware).
3. **Confirm** (``lead_magnet_double_opt_in``, on by default): the sample email carries a
   confirm link. Only confirmed addresses get the weekly email. A public form lets anyone
   type anyone's address; this keeps a typo or a prank from turning into weekly unsolicited
   mail, which is what gets a sending domain blocklisted.
4. **Nurture** (``nurture_leads``): every Monday from 09:00 (``lead_nurture_weekday``,
   ``lead_nurture_hour``, ``subscription_timezone``), each confirmed lead gets a "Weekly Tech
   Pulse": 3 fresh buying signals from their niche and 1-click upgrade buttons (the $10/month
   subscription and the full dataset, with the email prefilled and the sale attributed to
   ``leadmagnet``). Once per ISO week, catching up later in the week if the Mac slept.
5. **Leave**: every email has a one-click unsubscribe (RFC 8058 ``List-Unsubscribe-Post``) and
   the CAN-SPAM postal address. Unsubscribing also adds the address to the suppression list.
   The weekly email is not sent at all until a postal address is configured.

Abuse brakes on the public endpoint: a honeypot field, per-IP and global hourly caps, strict
address validation, and silent acceptance for suppressed addresses (the form never reveals who
is on the list).
"""

from __future__ import annotations

import csv
import html
import io
import re
import secrets
import threading
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from strategies.base import Strategy, TaskContext, TaskResult
from strategies.digital_asset_packager import ASSET_KIND, niche_title
from strategies.subscription_engine import delivery_moment, subscription_asset
from tools.attribution import checkout_link, decode_ref
from tools.dispatcher import Attachment, Email

FREE = "free"
SOURCE = "leadmagnet"
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,24}$")
SAMPLE_FIELDS = ["company", "domain", "intent_tag", "intent_score", "migration_path", "urgency_score", "openings",
                 "stack", "careers_url", "latest_posted_at"]
MAX_PER_RUN = 200
SAMPLE_RETRIES_PER_RUN = 20


def normalize_email(raw: str | None) -> str | None:
    email = (raw or "").strip().lower()
    if not email or len(email) > 254 or any(c in email for c in "\r\n\0 ,;<>"):
        return None
    return email if EMAIL_RE.match(email) else None


def _with_params(url: str, params: dict[str, str]) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({k: v for k, v in params.items() if v})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def link(config: Any, action: str, token: str) -> str:
    base = config.lead_capture_base
    return f"{base}/lead-magnet/{action}?t={token}" if base and token else ""


# ----------------------------------------------------------------------------- capture
class CaptureLimiter:
    """Sliding-window counter per client IP (in memory: a restart just resets it)."""

    def __init__(self, per_ip_hour: int, clock: Callable[[], float] = time.monotonic):
        self.per_ip_hour = per_ip_hour
        self.clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, ip: str) -> bool:
        now = self.clock()
        with self._lock:
            q = self._hits[ip or "unknown"]
            while q and now - q[0] > 3600:
                q.popleft()
            if len(q) >= self.per_ip_hour:
                return False
            q.append(now)
            if len(self._hits) > 10_000:  # bound memory under a distributed flood
                for key in [k for k, v in self._hits.items() if not v][:5_000]:
                    del self._hits[key]
            return True


@dataclass
class CaptureResult:
    status: str                    # accepted | invalid | spam | rate_limited | disabled | suppressed
    subscriber_id: int | None = None
    created: bool = False
    send_sample: bool = False

    @property
    def public_ok(self) -> bool:
        """What the visitor is told. Spam and suppressed look like success on purpose."""
        return self.status in ("accepted", "spam", "suppressed")


def known_niches(tools) -> set[str]:
    niches = {a.get("niche") for a in tools.state.list_assets() if a.get("niche")}
    return {n for n in niches if n} | {n["name"] for n in tools.config.niches}


def capture(tools, email: str | None, niche: str | None = None, source: str = "", ref: str = "", honeypot: str = "",
            ip: str = "", limiter: CaptureLimiter | None = None, copy_tag: str = "") -> CaptureResult:
    cfg, state = tools.config, tools.state
    if not cfg.lead_magnet_enabled:
        return CaptureResult("disabled")
    if honeypot:
        return CaptureResult("spam")
    if limiter is not None and not limiter.allow(ip):  # every attempt counts, valid or not
        return CaptureResult("rate_limited")
    addr = normalize_email(email)
    if addr is None:
        return CaptureResult("invalid")
    if state.free_captures_since(state.clock() - timedelta(hours=1)) >= cfg.lead_magnet_max_per_hour:
        state.log_error("lead_magnet", "hourly capture cap reached; rejecting signups", kind="security")
        return CaptureResult("rate_limited")
    if state.is_suppressed(addr):
        return CaptureResult("suppressed")
    niches = known_niches(tools)
    if niche not in niches:
        active = state.active_hypothesis()
        niche = active["params"].get("niche") if active else None
    channel, campaign = decode_ref(ref)
    if channel == "direct" and source:
        channel, campaign = "lander", re.sub(r"[^a-z0-9_/.-]", "_", source.lower())[:60]
    status = "pending" if cfg.lead_magnet_double_opt_in else "active"
    sid, created = state.capture_free_subscriber(addr, niche, secrets.token_urlsafe(24), channel, campaign, status)
    if created and copy_tag:
        from tools.copy_bandit import record_signup

        record_signup(state, copy_tag, state.clock())  # credits the copy variant the visitor saw
    sub = state.subscriber(sid) or {}
    if sub.get("subscription_status") == "unsubscribed":
        return CaptureResult("suppressed", sid)
    done = state.subscription_delivery(sid, "sample")
    resend = done is None or done["status"] == "failed"
    return CaptureResult("accepted", sid, created, resend)


# ----------------------------------------------------------------------------- content
def radar(tools, niche: str) -> list[dict[str, Any]]:
    path = f"exports/intel/{niche}/tech_radar.json"
    return tools.files.read_json(path) if niche and tools.files.exists(path) else []


def sample_records(records: list[dict[str, Any]], size: int) -> list[dict[str, Any]]:
    ranked = sorted(records, key=lambda r: (-int(r.get("intent_score") or 0), -int(r.get("urgency_score") or 0),
                                            r.get("company", "").lower()))
    out = []
    for r in ranked[:size]:
        row = {f: r.get(f) for f in SAMPLE_FIELDS}
        row["stack"] = (r.get("stack") or [])[:6]
        out.append(row)
    return out


def sample_csv(rows: list[dict[str, Any]]) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=SAMPLE_FIELDS, extrasaction="ignore")
    w.writeheader()
    for row in rows:
        w.writerow({k: "; ".join(map(str, v)) if isinstance(v, list) else ("" if v is None else v) for k, v in row.items()})
    return buf.getvalue().encode()


ANGLES = {
    "migration": "Migrations need tooling, contractors and data-quality work. Lead with the migration path you've done before.",
    "compliance": "Audits come with deadlines and budget: evidence collection, hardening, policy-as-code, pen tests.",
    "leadership": "A founding or first infra/data hire picks the stack. Reach them in their first 90 days.",
}


def cheatsheet(niche: str, records: list[dict[str, Any]], offer_url: str = "") -> str:
    label = niche_title(niche)
    stack = Counter(t for r in records for t in r.get("stack") or [])
    signals = Counter(s for r in records for s in (r.get("commercial_signals") or []))
    levels = Counter(r.get("intent_level") for r in records if r.get("intent_score"))
    lines = [
        f"# {label} Hiring-Intent Cheatsheet", "",
        f"Built from {len(records)} companies currently hiring. Every number below is computed from live postings.", "",
        "## Reading an intent tag", "",
        "`Urgency: High (Cloud Migration)` = intent score 60-100 with the strongest signal in brackets.",
        "Medium is 35-59, Low 1-34. The score adds the strongest buying signal, half of any others, freshness "
        "(posted in the last 7 days) and the number of open roles.", "",
        "| Signal family | What it looks like in a posting | Why it means budget |", "|---|---|---|",
        "| Migration & modernization | \"moving from Snowflake to BigQuery\", \"legacy Oracle to Postgres\", \"Kubernetes migration\" | vendor churn, a project with a deadline |",
        "| Compliance & security | SOC 2, HIPAA, FedRAMP, PCI DSS, ISO 27001, zero trust | audits are funded and dated |",
        "| Leadership & scaling | founding engineer, first DevOps hire, head of infrastructure | a new owner choosing tools |",
        "",
        f"## This week in {label}", "",
        f"- Buying intent: {levels.get('High', 0)} high, {levels.get('Medium', 0)} medium, {levels.get('Low', 0)} low.",
        "- Top technologies: " + (", ".join(f"{t} ({n})" for t, n in stack.most_common(6)) or "n/a") + ".",
        "- Top buying signals: " + (", ".join(f"{t} ({n})" for t, n in signals.most_common(6)) or "n/a") + ".",
        "", "## How to use it", "",
    ]
    lines += [f"- **{k.title()}**: {v}" for k, v in ANGLES.items()]
    lines += ["", "Work the High tags first, then Medium tags with fresh postings. Always personalise from the "
              "company's own careers page (linked in the sample)."]
    if offer_url:
        lines += ["", f"Full dataset, every company with its intent tag, stack and verified careers page: {offer_url}"]
    return "\n".join(lines) + "\n"


def upgrade_links(tools, niche: str | None, campaign: str, email: str) -> dict[str, str]:
    """Checkout links attributed to the lead magnet, with the buyer's email prefilled."""
    state = tools.state
    out: dict[str, str] = {}
    if not niche:
        return out
    dataset = next((a for a in state.list_assets() if a["kind"] == ASSET_KIND and a.get("niche") == niche
                    and a.get("checkout_url") and a.get("status") == "published"), None)
    sub = subscription_asset(state, niche)
    if sub:
        out["subscription"] = _with_params(checkout_link(sub["checkout_url"], SOURCE, campaign), {"prefilled_email": email})
        out["subscription_price"] = f"${sub['price_cents'] / 100:.2f}"
    if dataset:
        out["dataset"] = _with_params(checkout_link(dataset["checkout_url"], SOURCE, campaign), {"prefilled_email": email})
        out["dataset_price"] = f"${dataset['price_cents'] / 100:.2f}"
    return out


def _footer_text(config: Any, unsubscribe: str) -> str:
    parts = ["--", f"You asked for this at {config.pages_base_url or 'our site'}."]
    if unsubscribe:
        parts.append(f"Unsubscribe with one click: {unsubscribe}")
    if config.sender_postal_address:
        parts.append(config.sender_postal_address)
    return "\n".join(parts)


def _list_unsubscribe(config: Any, unsubscribe: str) -> dict[str, str]:
    if not unsubscribe:
        return {}
    targets = [f"<{unsubscribe}>"]
    mailbox = config.unsubscribe_email or config.sender_email
    if mailbox:
        targets.append(f"<mailto:{mailbox}?subject=unsubscribe>")
    return {"List-Unsubscribe": ", ".join(targets), "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}


def send_sample(tools, subscriber_id: int) -> str:
    """Email the free sample (once). Returns delivered | dry_run | failed | skipped."""
    cfg, state = tools.config, tools.state
    sub = state.subscriber(subscriber_id)
    if not sub or sub.get("tier") != FREE or sub.get("subscription_status") == "unsubscribed" or not sub.get("email"):
        return "skipped"
    done = state.subscription_delivery(subscriber_id, "sample")
    if done and (done["status"] == "delivered" or (done["status"] == "dry_run" and not tools.dispatcher.live)):
        return done["status"]
    niche = sub.get("niche") or ""
    records = radar(tools, niche)
    if not records:
        return "skipped"  # no data yet: the nurture task retries once the niche has a radar
    rows = sample_records(records, cfg.lead_magnet_sample_size)
    links = upgrade_links(tools, niche, "sample", sub["email"])
    confirm = link(cfg, "confirm", sub["token"]) if sub["subscription_status"] == "pending" else ""
    unsubscribe = link(cfg, "unsubscribe", sub["token"])
    label = niche_title(niche)
    body = [f"Hi,\n\nHere is your free sample of {label} Tech Stack Intel: the {len(rows)} companies with the strongest "
            "buying signals right now (CSV attached), plus a one-page cheatsheet on reading the intent tags.\n"]
    for r in rows[:3]:
        body.append(f"- {r['company']}: {r.get('intent_tag') or 'hiring'}"
                    + (f" ({r['migration_path']})" if r.get("migration_path") else ""))
    if confirm:
        body.append(f"\nWant 3 fresh buying signals every Monday? Confirm here: {confirm}\n(No confirmation, no more email.)")
    if links.get("dataset"):
        body.append(f"\nThe full dataset ({links['dataset_price']}): {links['dataset']}")
    body.append(f"\n{cfg.sender_name or 'AutoMonetize'}\n\n{_footer_text(cfg, unsubscribe)}")
    email = Email(
        to=sub["email"], subject=f"Your free {label} sample: {len(rows)} companies with buying intent", kind="delivery",
        body="\n".join(body), headers=_list_unsubscribe(cfg, unsubscribe),
        attachments=[Attachment(f"{niche}-free-sample.csv", sample_csv(rows), "text/csv"),
                     Attachment(f"{niche}-intent-cheatsheet.md", cheatsheet(niche, records, links.get("dataset", "")).encode(),
                                "text/markdown")],
    )
    try:
        outcome = tools.dispatcher.send_transactional(email, audit_key=f"lead:{subscriber_id}:sample")
    except Exception as exc:  # noqa: BLE001 - retried by nurture_leads
        state.record_subscription_delivery(subscriber_id, "sample", "failed", detail=repr(exc))
        state.log_error("lead_magnet", f"sample to subscriber {subscriber_id} failed: {exc!r}")
        return "failed"
    state.record_subscription_delivery(subscriber_id, "sample", outcome, len(rows))
    return outcome


def confirm(tools, token: str) -> dict[str, Any] | None:
    sub = tools.state.subscriber_by_token(token)
    if sub is None or sub["subscription_status"] == "unsubscribed":
        return None
    if sub["subscription_status"] != "active":
        tools.state.set_free_status(sub["id"], "active")
    return tools.state.subscriber(sub["id"])


def unsubscribe(tools, token: str) -> dict[str, Any] | None:
    sub = tools.state.subscriber_by_token(token)
    if sub is None:
        return None
    tools.state.set_free_status(sub["id"], "unsubscribed")
    tools.state.suppress(sub["email"], "lead magnet unsubscribe")
    return tools.state.subscriber(sub["id"])


# ----------------------------------------------------------------------------- weekly pulse
def pulse_signals(records: list[dict[str, Any]], since: datetime, n: int = 3) -> list[dict[str, Any]]:
    """The strongest buying signals first seen/posted since ``since``; topped up with the strongest
    overall (marked ``fresh: False``) when a quiet week has fewer than ``n``."""
    cutoff = since.isoformat(timespec="seconds")

    def key(r: dict[str, Any]) -> tuple:
        return (-int(r.get("intent_score") or 0), -int(r.get("urgency_score") or 0), r.get("company", "").lower())

    fresh = sorted([r for r in records if r.get("intent_score") and (r.get("latest_posted_at") or "") >= cutoff], key=key)
    picked = [dict(r, fresh=True) for r in fresh[:n]]
    if len(picked) < n:
        names = {r["company"] for r in picked}
        rest = sorted([r for r in records if r.get("intent_score") and r["company"] not in names], key=key)
        picked += [dict(r, fresh=False) for r in rest[: n - len(picked)]]
    from api.data import company_id

    return [{"company": r["company"], "intent_tag": r.get("intent_tag", ""), "migration_path": r.get("migration_path", ""),
             "stack": (r.get("stack") or [])[:4], "openings": r.get("openings", 0), "fresh": r["fresh"],
             "careers_url": r.get("careers_url", ""), "intent_score": int(r.get("intent_score") or 0),
             "urgency_score": int(r.get("urgency_score") or 0), "company_id": company_id(r["company"])} for r in picked]


def dossier_links(config: Any, signals: list[dict[str, Any]], email: str, live: bool) -> dict[str, str]:
    """1-click dossier checkout for featured companies above ``dossier_pulse_min_intent``."""
    from strategies.dossier_engine import dossier_link
    from tools.dossier_builder import eligible

    if not live:
        return {}
    out = {}
    for s in signals:
        if s.get("intent_score", 0) > config.dossier_pulse_min_intent and eligible(s, config.dossier_min_score):
            url = dossier_link(config, s["company_id"], "pulse")
            if url:
                out[s["company_id"]] = _with_params(url, {"email": email})
    return out


def render_pulse(config: Any, niche: str, period: str, signals: list[dict[str, Any]], links: dict[str, str],
                 unsubscribe: str, dossiers: dict[str, str] | None = None) -> tuple[str, str, str]:
    dossiers = dossiers or {}
    price = f"${config.dossier_price_cents / 100:.0f}"
    label = niche_title(niche)
    subject = f"{label} Tech Pulse {period}: " + (signals[0]["intent_tag"].replace("Urgency: ", "") + f" at {signals[0]['company']}"
                                                   if signals else "this week's buying signals")
    text = [f"Weekly Tech Pulse: {label} ({period})", "", "Three buying signals from this week's hiring data:", ""]
    for i, s in enumerate(signals, 1):
        text.append(f"{i}. {s['company']}: {s['intent_tag']}" + (f", {s['migration_path']}" if s["migration_path"] else "")
                    + f". Stack: {', '.join(s['stack']) or 'n/a'}. {s['openings']} open role(s)."
                    + ("" if s["fresh"] else " (still active)"))
        if s.get("company_id") in dossiers:
            text.append(f"   Executive dossier on {s['company']} ({price}, instant PDF): {dossiers[s['company_id']]}")
    if links.get("subscription"):
        text += ["", f"Every company, every week ({links['subscription_price']}/month): {links['subscription']}"]
    if links.get("dataset"):
        text += [f"The full dataset now ({links['dataset_price']}): {links['dataset']}"]
    text += ["", _footer_text(config, unsubscribe)]

    def button(url: str, label_: str, color: str) -> str:
        return (f'<a href="{html.escape(url)}" style="display:inline-block;background:{color};color:#fff;padding:12px 18px;'
                f'border-radius:6px;text-decoration:none;font-weight:600;margin:6px 8px 6px 0">{html.escape(label_)}</a>')

    items = "".join(
        f"<li style=\"margin-bottom:10px\"><strong>{html.escape(s['company'])}</strong>: "
        f"<span style=\"background:#fff3e0;color:#8a4b00;padding:1px 6px;border-radius:4px\">{html.escape(s['intent_tag'])}</span>"
        + (f" &middot; {html.escape(s['migration_path'])}" if s["migration_path"] else "")
        + f"<br><span style=\"color:#555\">Stack: {html.escape(', '.join(s['stack']) or 'n/a')} &middot; {int(s['openings'] or 0)} open role(s)"
        + ("" if s["fresh"] else " &middot; still active") + "</span>"
        + (f"<br><a href=\"{html.escape(dossiers[s['company_id']])}\" style=\"display:inline-block;margin-top:6px;background:#1f6feb;"
           f"color:#fff;padding:6px 12px;border-radius:5px;text-decoration:none;font-size:13px;font-weight:600\">"
           f"Get the executive dossier on {html.escape(s['company'])}: {price}</a>" if s.get("company_id") in dossiers else "")
        + "</li>"
        for s in signals
    )
    buttons = ""
    if links.get("subscription"):
        buttons += button(links["subscription"], f"Get every signal weekly: {links['subscription_price']}/mo", "#2e7d32")
    if links.get("dataset"):
        buttons += button(links["dataset"], f"Full dataset: {links['dataset_price']}", "#111111")
    footer = [f"You confirmed a subscription at {html.escape(config.pages_base_url or 'our site')}."]
    if unsubscribe:
        footer.append(f'<a href="{html.escape(unsubscribe)}" style="color:#777">Unsubscribe with one click</a>')
    if config.sender_postal_address:
        footer.append(html.escape(config.sender_postal_address))
    body_html = (f'<div style="font-family:-apple-system,Segoe UI,sans-serif;max-width:560px;margin:0 auto;color:#111">'
                 f"<h2 style=\"margin-bottom:4px\">Weekly Tech Pulse: {html.escape(label)}</h2>"
                 f"<p style=\"color:#666;margin-top:0\">{html.escape(period)} &middot; three buying signals from this week's hiring data</p>"
                 f"<ol style=\"padding-left:20px\">{items}</ol><p>{buttons}</p>"
                 f"<p style=\"color:#777;font-size:12px;border-top:1px solid #eee;padding-top:10px\">{'<br>'.join(footer)}</p></div>")
    return subject, "\n".join(text), body_html


def nurture_problems(tools) -> list[str]:
    cfg = tools.config
    problems = []
    if not cfg.sender_postal_address.strip():
        problems.append("sender_postal_address (CAN-SPAM) is not set")
    if not cfg.lead_capture_base:
        problems.append("no public URL for unsubscribe links (run deploy/tunnel/setup_tunnel.sh)")
    return problems


class LeadMagnet(Strategy):
    name = "lead_magnet"
    tasks = ("nurture_leads",)

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        tools, cfg, state = ctx.tools, ctx.tools.config, ctx.tools.state
        if not cfg.lead_magnet_enabled:
            return TaskResult(True, "lead magnet disabled", {})
        retried = self.retry_samples(tools)
        now = state.clock()
        moment, period = delivery_moment(now, cfg.lead_nurture_weekday, cfg.lead_nurture_hour, cfg.subscription_timezone)
        counts = state.free_subscriber_counts()
        base = {"samples": retried, "leads": counts}
        if now < moment:
            return TaskResult(True, f"{retried} samples sent; next Tech Pulse {moment.isoformat(timespec='minutes')}",
                              {**base, "due": False})
        problems = nurture_problems(tools)
        if problems:
            return TaskResult(True, "Tech Pulse not sent: " + "; ".join(problems), {**base, "due": True, "blocked": problems})
        report = self.send_pulses(tools, moment - timedelta(days=7), f"{period}")
        return TaskResult(True, f"{retried} samples; Tech Pulse {period}: {report['sent']} sent, {report['dry_run']} dry-run, "
                                f"{report['failed']} failed, {report['skipped']} skipped", {**base, "due": True, **report})

    @staticmethod
    def retry_samples(tools) -> int:
        sent = 0
        subs = [s for s in tools.state.list_subscribers(("pending", "active"), tier=FREE)]
        for sub in subs[:500]:
            done = tools.state.subscription_delivery(sub["id"], "sample")
            if done is not None and done["status"] != "failed":
                continue
            if sent >= SAMPLE_RETRIES_PER_RUN:
                break
            sent += send_sample(tools, sub["id"]) == "delivered"
        return sent

    @staticmethod
    def send_pulses(tools, since: datetime, period: str) -> dict[str, Any]:
        state, cfg = tools.state, tools.config
        paying = {s["email"] for s in state.list_subscribers(("active", "trialing")) if s.get("email")}
        report = {"period": period, "sent": 0, "dry_run": 0, "failed": 0, "skipped": 0}
        by_niche: dict[str, list[dict[str, Any]]] = {}
        attempts = 0
        for sub in state.list_subscribers("active", tier=FREE):
            key = f"pulse:{period}"
            done = state.subscription_delivery(sub["id"], key)
            if done and (done["status"] == "delivered" or (done["status"] == "dry_run" and not tools.dispatcher.live)):
                continue
            if not sub.get("email") or sub["email"] in paying or state.is_suppressed(sub["email"]) or not sub.get("niche"):
                report["skipped"] += 1
                continue
            if attempts >= MAX_PER_RUN:
                break  # the rest go out next cycle, same week
            attempts += 1
            niche = sub["niche"]
            if niche not in by_niche:
                by_niche[niche] = pulse_signals(radar(tools, niche), since, cfg.lead_nurture_signals)
            signals = by_niche[niche]
            if not signals:
                report["skipped"] += 1
                continue
            links = upgrade_links(tools, niche, f"pulse_{period}", sub["email"])
            unsub = link(cfg, "unsubscribe", sub["token"])
            from strategies.dossier_engine import dossier_asset

            dossiers = dossier_links(cfg, signals, sub["email"], dossier_asset(state) is not None)
            subject, text, body_html = render_pulse(cfg, niche, period, signals, links, unsub, dossiers)
            email = Email(to=sub["email"], subject=subject, body=text, html=body_html, kind="nurture",
                          headers=_list_unsubscribe(cfg, unsub))
            try:
                outcome = tools.dispatcher.send_transactional(email, audit_key=f"lead:{sub['id']}:{period}")
            except Exception as exc:  # noqa: BLE001 - retried next cycle this week
                state.record_subscription_delivery(sub["id"], key, "failed", len(signals), repr(exc))
                report["failed"] += 1
                continue
            state.record_subscription_delivery(sub["id"], key, outcome, len(signals))
            report["sent" if outcome == "delivered" else "dry_run"] += 1
        return report


# ----------------------------------------------------------------------------- HTTP (framework-free)
@dataclass
class PageResponse:
    status: int
    body: str
    content_type: str = "text/html"
    headers: dict[str, str] = field(default_factory=dict)
    after: Callable[[], Any] | None = None  # work to run after responding (sending the sample)


def _page(config: Any, title: str, message: str, status: int = 200) -> PageResponse:
    back = config.pages_base_url or ""
    link_html = f'<p><a href="{html.escape(back)}">Back to the site</a></p>' if back else ""
    body = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<meta name="robots" content="noindex"><title>{html.escape(title)}</title>'
            '<style>body{font-family:system-ui,sans-serif;max-width:560px;margin:3rem auto;padding:0 1rem;line-height:1.5}</style>'
            f"</head><body><h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>{link_html}</body></html>")
    return PageResponse(status, body, headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"})


class LeadEndpoints:
    """``/lead-magnet/capture``, ``/confirm`` and ``/unsubscribe`` for the public listener."""

    def __init__(self, tools, limiter: CaptureLimiter | None = None):
        self.tools = tools
        self.limiter = limiter or CaptureLimiter(tools.config.lead_magnet_max_per_ip_hour)

    def capture(self, form: dict[str, str], ip: str, wants_json: bool = False) -> PageResponse:
        cfg = self.tools.config
        res = capture(self.tools, form.get("email"), form.get("niche"), form.get("source", ""), form.get("ref", ""),
                      form.get("website", ""), ip, self.limiter, form.get("copy", "")[:10])
        after = (lambda sid=res.subscriber_id: send_sample(self.tools, sid)) if res.send_sample and res.subscriber_id else None
        status = {"invalid": 400, "rate_limited": 429, "disabled": 404}.get(res.status, 200)
        if wants_json:
            import json

            return PageResponse(status, json.dumps({"ok": res.public_ok, "error": None if res.public_ok else res.status}),
                                "application/json", {"Cache-Control": "no-store"}, after)
        if res.status == "invalid":
            page = _page(cfg, "Please check your email address", "That address doesn't look right. Go back and try again.", 400)
        elif res.status == "rate_limited":
            page = _page(cfg, "Too many requests", "Please try again in a little while.", 429)
        elif res.status == "disabled":
            page = _page(cfg, "Not available", "The free sample isn't available right now.", 404)
        else:
            extra = (" Confirm from that email to get the Monday Tech Pulse." if cfg.lead_magnet_double_opt_in else "")
            page = _page(cfg, "Check your inbox", "Your free sample is on its way." + extra)
        page.after = after
        return page

    def action_page(self, action: str, token: str) -> PageResponse:
        """GET shows a button; only the POST acts. Mail security scanners prefetch every link in
        an email, and a GET that acted would silently confirm or unsubscribe people."""
        cfg = self.tools.config
        sub = self.tools.state.subscriber_by_token(token)
        if sub is None:
            return _page(cfg, "Link not recognised", "This link isn't valid any more.", 404)
        title, button = (("Confirm the Weekly Tech Pulse", "Yes, send me the Monday email") if action == "confirm"
                         else ("Unsubscribe", "Unsubscribe me"))
        page = _page(cfg, title, f"For {sub['email']}.")
        form = (f'<form method="post" action="/lead-magnet/{action}"><input type="hidden" name="t" value="{html.escape(token)}">'
                f'<button type="submit" style="padding:.6rem 1rem;font-size:1rem">{html.escape(button)}</button></form>')
        page.body = page.body.replace("</body>", form + "</body>")
        return page

    def confirm(self, token: str) -> PageResponse:
        sub = confirm(self.tools, token)
        if sub is None:
            return _page(self.tools.config, "Link expired", "This confirmation link isn't valid any more.", 404)
        return _page(self.tools.config, "Confirmed", "You'll get the Weekly Tech Pulse every Monday. Unsubscribe any time.")

    def unsubscribe(self, token: str) -> PageResponse:
        sub = unsubscribe(self.tools, token)
        if sub is None:
            return _page(self.tools.config, "Link not recognised", "This unsubscribe link isn't valid.", 404)
        return _page(self.tools.config, "Unsubscribed", "You won't get any more emails from us.")
