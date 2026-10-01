"""Referral rewards: buyers who bring a buyer get the newest version of the dataset, free.

Word of mouth is the cheapest channel, and a free update costs nothing to give. Each buyer gets a
personal link (in their follow-up email, ``strategies/buyer_followup.py``): the normal checkout,
with ``client_reference_id`` ``am--ref--<token>``, so the sale is attributed to them.

``reward_referrals`` then, once the referred order is ``REWARD_AFTER_DAYS`` (7) old and still
delivered (not refunded or disputed), emails the referrer the newest version of that dataset as an
attachment, with a thank-you. A referrer can't reward themselves (same address) and gets at most
one reward per ``REWARD_GAP_DAYS`` (30); extra referrals wait for the next window.

State: kv ``referral_tokens`` {token: email}, ``referral_rewarded`` [order ids], ``referral_last``
{email: when}.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Any

from strategies.base import Strategy, TaskContext, TaskResult
from tools.dispatcher import Attachment, Email

TOKENS = "referral_tokens"
DONE = "referral_rewarded"
LAST = "referral_last"
REWARD_AFTER_DAYS = 7
REWARD_GAP_DAYS = 30


def token_for(state: Any, email: str) -> str:
    tokens: dict[str, str] = dict(state.get(TOKENS) or {})
    email = email.strip().lower()
    for tok, addr in tokens.items():
        if addr == email:
            return tok
    tok = secrets.token_hex(5)
    tokens[tok] = email
    state.set(TOKENS, tokens)
    return tok


def referral_link(state: Any, email: str, checkout_url: str) -> str:
    from tools.attribution import checkout_link

    return checkout_link(checkout_url, "ref", token_for(state, email))


def newest_for(state: Any, asset: dict[str, Any]) -> dict[str, Any] | None:
    """The newest published version of the dataset ``asset`` belongs to."""
    from strategies.freshness_guard import niche_of

    niche = niche_of(state, asset)
    for a in state.list_assets():  # newest first
        if a["kind"] == asset["kind"] and a.get("status") == "published" and niche_of(state, a) == niche:
            return a
    return None


class Referrals(Strategy):
    name = "referrals"
    tasks = ("reward_referrals",)

    def run(self, task: str, ctx: TaskContext) -> TaskResult:
        tools, cfg, state = ctx.tools, ctx.tools.config, ctx.tools.state
        tokens: dict[str, str] = dict(state.get(TOKENS) or {})
        if not tokens:
            return TaskResult(True, "no referral links handed out yet", {"rewarded": 0})
        done = list(state.get(DONE) or [])
        seen = set(done)
        last: dict[str, str] = dict(state.get(LAST) or {})
        now = state.clock()
        cutoff = (now - timedelta(days=REWARD_AFTER_DAYS)).isoformat(timespec="seconds")
        rows = state._all("SELECT * FROM orders WHERE channel = 'ref' AND status = 'delivered' AND occurred_at <= ? ORDER BY id",
                          (cutoff,))
        rewarded = 0
        for o in rows:
            referrer = tokens.get(str(o.get("campaign") or ""))
            if o["id"] in seen:
                continue
            if not referrer or referrer == (o.get("email") or "").lower():
                done.append(o["id"])  # unknown token or a self-referral: nothing to reward
                seen.add(o["id"])
                continue
            if referrer in last and now - datetime.fromisoformat(last[referrer]) < timedelta(days=REWARD_GAP_DAYS):
                continue  # waits for the next window
            bought = state.get_asset(o["asset_id"]) if o.get("asset_id") else None
            gift = newest_for(state, bought) if bought else None
            if not gift or not tools.files.exists(gift["path"]):
                continue
            body = "\n".join([
                "Hi,", "", "Someone bought through your link, thank you! As promised, here's the newest version of "
                f"{gift['title']}, free (attached).", "", "Your link keeps working; every referral earns the next update.",
                "", "Thanks,", cfg.sender_name or "AutoMonetize"])
            try:
                tools.dispatcher.send_transactional(
                    Email(to=referrer, subject=f"Thanks for the referral: {gift['title']} (free update)", body=body, kind="delivery",
                          attachments=[Attachment(gift["path"].rsplit("/", 1)[-1], tools.files.read_bytes(gift["path"]))]),
                    audit_key=f"referral:{o['id']}")
            except Exception as exc:  # noqa: BLE001 - retried next cycle
                state.log_error("referrals", f"referral reward for order {o['id']} failed: {exc!r}")
                continue
            done.append(o["id"])
            seen.add(o["id"])
            last[referrer] = state.now()
            rewarded += 1
            state.log_action(int(state.get("iteration", 0)), None, "referral", "ok", f"{referrer} rewarded for order {o['id']}")
        state.set(DONE, done[-5000:])
        state.set(LAST, last)
        return TaskResult(True, f"referrals: {rewarded} rewarded", {"rewarded": rewarded})
