"""Weekly share kit: ready-to-paste posts for the places only you can post.

Posting to LinkedIn, X, Reddit or a friend's inbox needs *your* account, and the agent never
signs up or posts as you. What it can do is make sharing a 30-second job: ``refresh_share_kit``
writes, for each product on sale, a LinkedIn post, an X post (under 280 characters), a Reddit post
and a direct message, built only from real numbers in the current dataset.

* Every link is attributed (``utm_source`` on the product page, or ``client_reference_id`` on the
  checkout link), so the dashboard shows which channel sold.
* Datasets the freshness guard marked stale are left out.
* Rebuilt when the products or prices change, and at least once a week.
* Where to find it: the Monday daily report email, the control panel's **Share** tab (copy
  buttons) and ``automonetize share``.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

from strategies.base import Strategy, TaskContext, TaskResult

KEY = "share_kit"
CAMPAIGN = "share_kit"
MAX_PRODUCTS = 5
REFRESH_DAYS = 7
X_LIMIT = 280
CHANNELS = (("linkedin", "LinkedIn"), ("x", "X / Twitter"), ("reddit", "Reddit"), ("dm", "Direct message"))


def money(cents: int) -> str:
    return f"${cents / 100:,.2f}".replace(".00", "")


def link_for(product: dict[str, Any], channel: str) -> str:
    from tools.attribution import add_utm, checkout_link

    if product.get("lander_url"):
        return add_utm(product["lander_url"], channel, "social", CAMPAIGN)
    return checkout_link(product["url"], channel, CAMPAIGN)


def niche_of(state: Any, asset_id: int) -> str:
    from strategies.freshness_guard import niche_of as asset_niche

    return asset_niche(state, state.get_asset(asset_id) or {})


def facts(files: Any, niche: str) -> list[str]:
    """Short, true statements about the dataset. Missing numbers are left out, never guessed."""
    from strategies.inbound_syndicator import intel_metrics

    if not niche:
        return []
    _, m = intel_metrics(files, niche)
    out = []
    if m.get("companies"):
        out.append(f"{m['companies']} companies hiring right now")
    if m.get("roles"):
        out.append(f"{m['roles']} open roles")
    if m.get("high_urgency"):
        out.append(f"{m['high_urgency']} hiring urgently")
    if m.get("signals"):
        out.append("top signal: " + m["signals"][0][0])
    return out


def posts_for(product: dict[str, Any], niche_facts: list[str]) -> list[dict[str, str]]:
    title, price = product["title"], money(product["price_cents"])
    lead = ", ".join(niche_facts[:3])  # the short form, for X and DMs
    full = ", ".join(niche_facts)
    links = {ch: link_for(product, ch) for ch, _ in CHANNELS}
    linkedin = "\n".join(filter(None, [
        f"I put together {title}: a spreadsheet of companies actively hiring, refreshed by an automated pipeline.",
        f"This week: {full}." if full else "",
        "Useful if you sell to tech teams, recruit, or are job hunting and want to see who is hiring before everyone else.",
        f"{price}, instant download: {links['linkedin']}",
    ]))
    x_text = f"{title}: {lead}. {price}, instant download: {links['x']}" if lead else \
        f"{title}, fresh hiring data, instant download for {price}: {links['x']}"
    if len(x_text) > X_LIMIT:
        x_text = f"{title}, {price}: {links['x']}"
    reddit = "\n".join(filter(None, [
        f"Title: [Dataset] {title}" + (f" ({niche_facts[0]})" if niche_facts else ""),
        "",
        "I built a pipeline that collects public job postings and turns them into a list of companies that are hiring, "
        "with the stack they use and how urgently they're hiring.",
        f"Current numbers: {full}." if full else "",
        f"It's {price} here: {links['reddit']}",
        "Happy to answer questions about how it's built.",
        "",
        "(Check the subreddit's self-promotion rules before posting.)",
    ]))
    dm = (f"Hi! Thought this might help you: {title}, a list of companies hiring right now"
          + (f" ({lead})" if lead else "") + f". {price}: {links['dm']}")
    texts = {"linkedin": linkedin, "x": x_text, "reddit": reddit, "dm": dm}
    return [{"product": title, "price": price, "channel": ch, "label": label, "text": texts[ch], "link": links[ch]}
            for ch, label in CHANNELS]


def fingerprint(products: list[dict[str, Any]]) -> str:
    raw = "|".join(f"{p['url']}:{p['price_cents']}:{p.get('lander_url', '')}" for p in products)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def build_kit(state: Any, files: Any) -> dict[str, Any]:
    from strategies.freshness_guard import promotable

    products = promotable(state)[:MAX_PRODUCTS]  # stale datasets are not advertised
    posts: list[dict[str, str]] = []
    for p in products:
        posts += posts_for(p, facts(files, niche_of(state, p["id"])))
    return {"generated_at": state.now(), "fingerprint": fingerprint(products), "posts": posts}


def as_text(kit: dict[str, Any] | None) -> str:
    if not kit or not kit.get("posts"):
        return "Nothing to share yet: the kit appears once a product is on sale."
    out = []
    for post in kit["posts"]:
        out += [f"--- {post['label']} · {post['product']} ---", post["text"], ""]
    return "\n".join(out).rstrip()


class ShareKit(Strategy):
    name = "share_kit"
    tasks = ("refresh_share_kit",)

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        from strategies.freshness_guard import promotable

        state = ctx.tools.state
        current = state.get(KEY) or {}
        fp = fingerprint(promotable(state)[:MAX_PRODUCTS])
        age_ok = current.get("generated_at") and \
            state.clock() - datetime.fromisoformat(current["generated_at"]) < timedelta(days=REFRESH_DAYS)
        if current.get("fingerprint") == fp and age_ok:
            return TaskResult(True, "share kit up to date", {"posts": len(current.get("posts", []))})
        kit = build_kit(state, ctx.tools.files)
        state.set(KEY, kit)
        return TaskResult(True, f"share kit: {len(kit['posts'])} post(s) ready", {"posts": len(kit["posts"])})
