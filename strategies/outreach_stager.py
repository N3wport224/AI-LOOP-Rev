"""Draft personalized, value-first outreach and stage it for human review. Nothing is ever sent.

Two draft types:

* ``email`` pitches to employers who published a contact address in their listing, offering
  ``config.outreach_offer`` matched to the stack they are hiring for.
* ``submission`` packages announcing a new asset version to a community (Show HN, a subreddit,
  an awesome-list PR). The reviewer picks where it goes.

Guardrails: per-recipient cooldown, daily cap, minimum quality score, stale-listing filter,
an explicit opt-out line, and a spam-phrase blocklist. Everything lands in ``outreach_queue``
with status ``pending_review``; ``automonetize outreach approve/export`` is the only way out.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from strategies.base import Strategy, TaskContext, TaskResult
from strategies.digital_asset_packager import ASSET_KIND, listing_title

SPAM_PHRASES = (
    "guarantee", "act now", "limited time", "risk-free", "risk free", "100%", "click here",
    "make money", "no obligation", "urgent", "winner", "!!!",
)
OPT_OUT = 'Reply "no thanks" and I won\'t contact you again.'
MAX_LISTING_AGE_DAYS = 45


def _words(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text))


def score_message(body: str, lead: dict[str, Any]) -> float:
    lowered = body.lower()
    if any(p in lowered for p in SPAM_PHRASES):
        return 0.0
    score = 0.0
    if lead.get("company") and lead["company"].lower() in lowered:
        score += 0.2
    if lead.get("title") and lead["title"].lower() in lowered:
        score += 0.2
    if any(s in lowered for s in (lead.get("stack") or [])):
        score += 0.2
    if 50 <= _words(body) <= 170:
        score += 0.2
    if OPT_OUT.lower() in lowered:
        score += 0.2
    return round(score, 2)


def _age_days(posted_at: str, now: datetime) -> float | None:
    if not posted_at:
        return None
    try:
        dt = datetime.fromisoformat(posted_at)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt).total_seconds() / 86400


def draft_pitch(lead: dict[str, Any], *, sender_name: str, sender_email: str, skills: list[str], offer: str, now: datetime) -> tuple[str, str]:
    company, title = lead["company"], lead["title"]
    stack = lead.get("stack") or []
    overlap = [s for s in stack if s in {k.lower() for k in skills}] if skills else stack[:3]
    age = _age_days(lead.get("posted_at", ""), now)
    when = f" (posted {int(age)} day{'s' if int(age) != 1 else ''} ago)" if age is not None and age >= 1 else ""
    stack_line = f"The listing mentions {', '.join(stack[:4])}, which is the work I do day to day." if overlap else ""
    focus = f" in {', '.join(overlap[:3])}" if overlap else ""
    subject = f"{title} at {company}: help while you hire"
    body = "\n".join(
        line
        for line in [
            f"Hi {company} team,",
            "",
            f"I saw you're hiring a {title}{when}. {stack_line}".rstrip(),
            "",
            f"While you look for the right long-term fit, I can offer {offer}{focus}: taking one scoped "
            "piece of the backlog off the team's plate for a few weeks, with a written handover at the end.",
            "",
            "If that's useful, reply with the task you'd most like handled and I'll send a concrete plan "
            "and a fixed quote. If not, no follow-up from me.",
            "",
            sender_name or "[Your name]",
            sender_email or "[your@email]",
            "",
            OPT_OUT,
        ]
    )
    return subject, body


def draft_submission(asset: dict[str, Any], niche: str, lead_count: int, storefront_url: str) -> tuple[str, str]:
    title = listing_title(niche)
    subject = f"Show: {title}, {lead_count} deduplicated roles"
    link = storefront_url or "[storefront link]"
    body = (
        f"I built a small pipeline that pulls public job-board APIs, validates and deduplicates the "
        f"listings, and tags each role with stack and seniority. Version {asset['version']} of the "
        f"{title} covers {lead_count} roles.\n\n"
        f"There's a free sample on the showcase page and the full CSV/JSON bundle here: {link}\n\n"
        "Feedback on which fields would make this more useful is very welcome.\n\n"
        "Suggested venues (pick one, follow its self-promotion rules): Show HN, a relevant subreddit, "
        "or a PR to a matching awesome-list."
    )
    return subject, body


class OutreachStager(Strategy):
    name = "outreach_stager"
    tasks = ("stage_outreach",)

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        tools = ctx.tools
        cfg = tools.config
        state = tools.state
        now = state.clock()
        day_start = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        budget = max(0, cfg.outreach_daily_cap - state.outreach_staged_since(day_start))
        if ctx.degraded:
            budget = min(budget, 5)
        staged = skipped = rejected_quality = 0

        asset = state.latest_asset(ctx.hypothesis["id"], ASSET_KIND)
        if asset and budget > 0:
            subject, body = draft_submission(asset, ctx.niche, asset["lead_count"], cfg.storefront_url)
            if state.stage_outreach(ctx.hypothesis["id"], f"asset-v{asset['version']}", "submission", "community", subject, body, 1.0):
                staged += 1
                budget -= 1

        cooldown_start = now - timedelta(days=cfg.outreach_cooldown_days)
        for lead in state.leads_for_niche(ctx.niche):
            if budget <= 0:
                break
            email = (lead.get("contact_email") or "").lower()
            if not email:
                continue
            age = _age_days(lead.get("posted_at", ""), now)
            if age is not None and age > MAX_LISTING_AGE_DAYS:
                skipped += 1
                continue
            if state.recipient_contacted_since(email, cooldown_start):
                skipped += 1
                continue
            subject, body = draft_pitch(
                lead, sender_name=cfg.sender_name, sender_email=cfg.sender_email,
                skills=cfg.sender_skills, offer=cfg.outreach_offer, now=now,
            )
            score = score_message(body, lead)
            if score < cfg.outreach_min_score:
                rejected_quality += 1
                continue
            if state.stage_outreach(ctx.hypothesis["id"], lead["dedupe_key"], "email", email, subject, body, score):
                staged += 1
                budget -= 1
            else:
                skipped += 1

        return TaskResult(
            ok=True,
            summary=f"staged {staged} drafts for review ({skipped} skipped, {rejected_quality} below quality bar)",
            metrics={"staged": staged, "skipped": skipped, "low_quality": rejected_quality, "budget_left": budget},
        )
