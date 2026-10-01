"""New-release emails: tell past buyers when a new dataset goes on sale.

People who already bought a dataset are the likeliest buyers of the next one. When the agent
starts selling a dataset for a new niche, ``announce_releases`` emails each past buyer who doesn't
own that niche yet, once:

* Only past buyers (delivered one-off orders, not refunded or disputed), never anyone else, and
  never a suppressed address. Every email has the postal address, a reply-"unsubscribe" opt-out
  (the support desk honours it) and a ``List-Unsubscribe`` header.
* At most one announcement per buyer every ``announce_min_gap_days`` (14), at most 50 per cycle,
  and only during the first 14 days after the release. New versions of a niche you already sell
  are not "new". Stale datasets (``strategies/freshness_guard.py``) are not announced.
* The first run only records what's already on sale, so turning this on doesn't email anyone
  about old products. Live sending waits until ``sender_postal_address`` is set. Off with
  ``release_announcements = false``.
* If a launch code is active (``strategies/launch_promos.py``), the email offers it with a link
  that applies it at checkout.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from strategies.base import Strategy, TaskContext, TaskResult
from tools import contact_policy as contact
from tools.dispatcher import Email

RELEASES = "release_families"       # {niche: first seen on sale}
LAST_SENT = "release_last_sent"     # {email: when}
SENT_PREFIX = "release_sent:"       # + niche -> [emails]
WINDOW_DAYS = 14
PER_CYCLE = 50


def families(state: Any) -> dict[str, dict[str, Any]]:
    """niche -> newest promotable dataset of that niche."""
    from strategies.freshness_guard import niche_of, promotable

    out: dict[str, dict[str, Any]] = {}
    for p in promotable(state):
        if p["kind"] != "lead_directory":
            continue
        niche = niche_of(state, state.get_asset(p["id"]) or {})
        if niche and niche not in out:
            out[niche] = p
    return out


def new_releases(state: Any) -> dict[str, dict[str, Any]]:
    """Record niches newly on sale; return those first seen in the last ``WINDOW_DAYS`` (niche -> product
    with ``released_at``). The first call counts everything already on sale as old news."""
    now = state.clock()
    current = families(state)
    known: dict[str, str] = dict(state.get(RELEASES) or {})
    first_run = state.get(RELEASES) is None
    for niche in current:
        if niche not in known:
            known[niche] = (now - timedelta(days=WINDOW_DAYS + 1)).isoformat(timespec="seconds") if first_run else state.now()
    state.set(RELEASES, known)
    return {n: {**p, "released_at": known[n]} for n, p in current.items()
            if now - datetime.fromisoformat(known[n]) <= timedelta(days=WINDOW_DAYS)}


def buyers(state: Any) -> dict[str, set[str]]:
    """email -> niches they own."""
    from strategies.freshness_guard import niche_of

    owned: dict[str, set[str]] = {}
    rows = state._all("SELECT email, asset_id FROM orders WHERE status = 'delivered' AND kind = 'one_off' AND email IS NOT NULL")
    for r in rows:
        asset = state.get_asset(r["asset_id"]) if r.get("asset_id") else None
        owned.setdefault(r["email"].lower(), set()).add(niche_of(state, asset) if asset else "")
    return owned


def announcement(cfg: Any, product: dict[str, Any], to: str, promo: dict[str, Any] | None = None) -> Email:
    from strategies.launch_promos import offer_line
    from strategies.share_kit import money
    from tools.attribution import add_utm, checkout_link
    from tools.promo import promo_link

    if promo:  # straight to checkout with the code applied
        link = promo_link(checkout_link(product["url"], "announce", "release"), promo["code"])
    else:
        link = add_utm(product["lander_url"], "announce", "email", "release") if product.get("lander_url") \
            else checkout_link(product["url"], "announce", "release")
    mailbox = cfg.unsubscribe_email or cfg.sender_email
    lines = ["Hi,", "", "You bought one of my hiring datasets before, so a quick heads-up: a new one is out.", "",
             f"{product['title']}: {money(product['price_cents'])}", *([offer_line(promo)] if promo else []), link, "",
             "Same format as the one you have: companies hiring now, their stack and how urgently they're hiring.", "",
             "Thanks,", cfg.sender_name or "AutoMonetize", "", "--",
             "You're getting this because you bought a dataset. Reply \"unsubscribe\" and you won't hear about new ones again."]
    if cfg.sender_postal_address:
        lines.append(cfg.sender_postal_address)
    headers = {"List-Unsubscribe": f"<mailto:{mailbox}?subject=unsubscribe>"} if mailbox else {}
    return Email(to=to, subject=f"New dataset: {product['title']}", body="\n".join(lines), kind="delivery", headers=headers)


class ReleaseAnnouncer(Strategy):
    name = "release_announcer"
    tasks = ("announce_releases",)

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        tools, cfg, state = ctx.tools, ctx.tools.config, ctx.tools.state
        if not cfg.release_announcements:
            return TaskResult(True, "release announcements off", {"sent": 0})
        fresh = new_releases(state)
        if not fresh:
            return TaskResult(True, "no new releases to announce", {"sent": 0})
        if tools.dispatcher.live and not cfg.sender_postal_address.strip():
            return TaskResult(True, "release emails wait for sender_postal_address (CAN-SPAM)", {"sent": 0})
        from strategies.launch_promos import active_code

        sent = 0
        owners = buyers(state)
        for niche, product in sorted(fresh.items()):
            done = set(state.get(SENT_PREFIX + niche) or [])
            for email, owned in sorted(owners.items()):
                if sent >= PER_CYCLE:
                    break
                if niche in owned or email in done or state.is_suppressed(email):
                    continue
                if contact.blocked(state, cfg, email, "release"):
                    continue
                try:
                    tools.dispatcher.send_transactional(announcement(cfg, product, email, active_code(state, niche)),
                                                        audit_key=f"release:{niche}:{email}")
                except Exception as exc:  # noqa: BLE001 - retried next cycle
                    state.log_error("release_announcer", f"announcement to {email} failed: {exc!r}")
                    continue
                done.add(email)
                contact.record(state, email, "release", niche)
                sent += 1
            state.set(SENT_PREFIX + niche, sorted(done))
        return TaskResult(True, f"release announcements: {sent} sent", {"sent": sent})
