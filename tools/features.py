"""Features overview (Phase 94): every major capability, whether it's on, and what it's waiting for.

With this many moving parts, "is X working?" deserves one answer. ``automonetize features`` lists
each feature by group with one of: **on**, **off** (switched off by a setting, which it names), or
**waiting** (on, but blocked by something only you can provide, with what). Computed from the
settings and the agent's own state; nothing is contacted.
"""

from __future__ import annotations

from typing import Any, Callable

Need = Callable[[Any, Any], str | None]


def _live(cfg: Any, state: Any) -> str | None:
    live = str(cfg.stripe_secret_key or "").startswith(("sk_live_", "rk_live_")) and not cfg.dry_run
    return None if live else "real payments (automonetize go-live)"


def _postal(cfg: Any, state: Any) -> str | None:
    return None if str(cfg.sender_postal_address or "").strip() else "your postal address (automonetize setup)"


def _site(cfg: Any, state: Any) -> str | None:
    return None if cfg.github_pages_repo and cfg.pages_base_url else "the public site (automonetize connect-marketing)"


def _tunnel(cfg: Any, state: Any) -> str | None:
    return None if cfg.lead_capture_base else "a public URL (deploy/tunnel/setup_tunnel.sh)"


def _imap(cfg: Any, state: Any) -> str | None:
    from tools.inbox import imap_settings

    return None if all(imap_settings(cfg)) else "IMAP access to your mailbox"


def _all(*needs: Need) -> Need:
    def check(cfg: Any, state: Any) -> str | None:
        missing = [m for m in (n(cfg, state) for n in needs) if m]
        return ", ".join(missing) or None
    return check


# (group, name, setting that switches it off or None, what it needs)
FEATURES: list[tuple[str, str, str | None, Need | None]] = [
    ("Selling", "Datasets built and sold", None, _live),
    ("Selling", "All-datasets bundle", None, _live),
    ("Selling", "Weekly-update subscription", None, _live),
    ("Selling", "Team license", "team_license", _live),
    ("Selling", "Yearly plan", "annual_plan", _live),
    ("Selling", "Instant delivery via webhook", None, _all(_live, _tunnel)),
    ("Selling", "Download links for big files", None, _tunnel),
    ("Selling", "Thank-you page after payment", "checkout_thank_you", _all(_live, _site)),
    ("Marketing", "Public site, SEO pages, RSS", None, _site),
    ("Marketing", "Articles on Dev.to", None, lambda c, s: None if c.devto_api_key else "a Dev.to key (connect-marketing)"),
    ("Marketing", "Free-sample lead magnet", "lead_magnet_enabled", _all(_tunnel, _postal)),
    ("Marketing", "Weekly share kit (posts for you)", None, None),
    ("Marketing", "Launch codes and quarterly sale", "seasonal_sale", _live),
    ("Customers", "Buyer follow-up", "buyer_followup", _postal),
    ("Customers", "New-release emails", "release_announcements", _postal),
    ("Customers", "Refresh offers", "refresh_offers", _all(_live, _postal)),
    ("Customers", "Win-back", "winback", _all(_live, _postal)),
    ("Customers", "Bundle upgrade credit", "bundle_upgrade", _all(_live, _postal)),
    ("Customers", "Free-sample offer", "sample_offer", _all(_live, _postal, _tunnel)),
    ("Customers", "Referral rewards", "referrals", _postal),
    ("Customers", "Support desk (resends, alerts)", None, _imap),
    ("Customers", "Testimonials", None, _imap),
    ("Customers", "1-click ratings", "buyer_followup", _all(_live, _postal, _tunnel)),
    ("Protection", "Refund and dispute tracking", None, _live),
    ("Protection", "Card-testing and duplicate guards", None, _live),
    ("Protection", "Bounce guard", None, _imap),
    ("Protection", "Storefront health", None, None),
    ("Protection", "Security self-audit", None, None),
    ("Protection", "Daily backups", "backups_enabled", None),
    ("Upkeep", "Self-update", "auto_update", None),
    ("Upkeep", "Self-evolution", "enable_autonomous_code_evolution", None),
    ("Upkeep", "Heartbeat", None, lambda c, s: None if c.heartbeat_url else "a ping URL (automonetize heartbeat)"),
    ("Upkeep", "Phone notifications", None, lambda c, s: None if c.ntfy_topic else "a topic (automonetize phone)"),
    ("Upkeep", "Email commands", "owner_commands", _imap),
    ("Upkeep", "Daily phone summary", "digest_push", lambda c, s: None if c.ntfy_topic else "a topic (automonetize phone)"),
    ("Upkeep", "Battery saver", "battery_saver", None),
    ("Upkeep", "Job-source and clock checks", None, None),
    ("Money", "Milestone emails", "owner_reports", None),
    ("Money", "Monthly and yearly books", "bookkeeping", None),
    ("Money", "Offer tuning", "offer_tuning", None),
]


def overview(cfg: Any, state: Any) -> list[dict[str, str]]:
    out = []
    for group, name, flag, need in FEATURES:
        if flag and not getattr(cfg, flag, True):
            status, detail = "off", f"{flag} = false"
        else:
            missing = need(cfg, state) if need else None
            status, detail = ("waiting", f"needs {missing}") if missing else ("on", "")
        out.append({"group": group, "name": name, "status": status, "detail": detail})
    return out
