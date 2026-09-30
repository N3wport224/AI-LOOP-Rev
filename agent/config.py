"""Configuration: defaults < TOML file < environment variables."""

from __future__ import annotations

import os
import tomllib
import json
from dataclasses import MISSING, dataclass, field, fields
from pathlib import Path
from typing import Any, Mapping

DEFAULT_NICHES: list[dict[str, Any]] = [
    {"name": "python-remote", "keywords": ["python", "django", "fastapi", "flask"]},
    {"name": "devops-sre", "keywords": ["devops", "sre", "kubernetes", "terraform", "platform engineer"]},
    {"name": "ml-ai", "keywords": ["machine learning", "ml engineer", "llm", "data scientist", "ai engineer"]},
    {"name": "frontend-react", "keywords": ["react", "frontend", "front-end", "typescript", "next.js"]},
    {"name": "rust-go-systems", "keywords": ["rust", "golang", "go developer", "systems engineer"]},
]

ENV_PREFIX = "AUTOMONETIZE_"


@dataclass
class Config:
    data_dir: Path = Path("data")
    db_filename: str = "agent_state.db"

    # Loop
    interval_seconds: int = 3600
    pivot_after_iterations: int = 24
    max_hypothesis_generations: int = 3

    # Circuit breakers
    max_actions_per_cycle: int = 16
    max_api_calls_per_cycle: int = 60
    max_consecutive_errors: int = 5
    task_retry_attempts: int = 3

    # HTTP
    http_rate_per_minute: float = 20.0
    http_timeout: float = 15.0
    http_backoff_seconds: float = 1.0
    respect_robots_txt: bool = True

    # Shell sandbox
    shell_allowlist: list[str] = field(default_factory=lambda: ["git", "python3", "ls", "cat", "echo", "zip"])
    shell_timeout: float = 30.0

    # Revenue
    daily_target_cents: int = 1000
    gumroad_access_token: str = ""
    platform_fee_pct: float = 10.0
    platform_fee_fixed_cents: int = 50

    # Strategies
    lead_sources: list[str] = field(default_factory=lambda: ["remoteok", "arbeitnow", "hn_hiring"])
    niches: list[dict[str, Any]] = field(default_factory=lambda: [dict(n) for n in DEFAULT_NICHES])
    min_leads_for_asset: int = 10
    asset_price_cents: int = 900
    storefront_url: str = ""

    # Outreach (drafts only, never sent automatically)
    sender_name: str = ""
    sender_email: str = ""
    sender_skills: list[str] = field(default_factory=list)
    outreach_offer: str = "short-term contract help"
    outreach_daily_cap: int = 20
    outreach_min_score: float = 0.6
    outreach_cooldown_days: int = 30

    # Hypothesis scoring
    signal_window_iterations: int = 12   # cycles with zero views AND zero sales before a pivot
    high_urgency_threshold: int = 60

    # Tech stack intel
    intel_max_url_checks: int = 15

    # Storefronts ("auto" = stripe if a key is set, else lemonsqueezy if configured, else gumroad staging)
    storefront_provider: str = "auto"
    price_tiers: list[list[int]] = field(default_factory=lambda: [[0, 500], [25, 900], [75, 1500]])
    currency: str = "usd"
    stripe_secret_key: str = ""
    stripe_payment_links: dict[str, str] = field(default_factory=dict)  # niche -> pre-made Payment Link URL
    stripe_fee_pct: float = 2.9
    stripe_fee_fixed_cents: int = 30
    lemonsqueezy_api_key: str = ""
    lemonsqueezy_store_id: str = ""
    lemonsqueezy_variant_id: str = ""                                  # shared "dataset" variant
    lemonsqueezy_variant_map: dict[str, str] = field(default_factory=dict)  # niche -> dedicated variant id
    lemonsqueezy_fee_pct: float = 5.0
    lemonsqueezy_fee_fixed_cents: int = 50
    allow_manual_fulfillment: bool = False  # publish checkouts even when the agent cannot email the file

    # GitHub (showcase directory, gists, Pages landers, traffic-based view tracking)
    github_token: str = ""
    github_showcase_repo: str = ""   # owner/repo; samples go to showcase/<niche>/
    github_showcase_mode: str = "repo"  # repo | gist
    github_pages_repo: str = ""      # owner/repo serving GitHub Pages; landers go to docs/<niche>/
    github_branch: str = "main"
    pages_base_url: str = ""         # e.g. https://you.github.io/datasets

    # Email dispatch (cold outreach needs approval; everything is dry-run until dry_run = false)
    dry_run: bool = True
    email_backend: str = ""          # smtp | sendgrid | postmark
    outreach_email_backend: str = "smtp"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    sendgrid_api_key: str = ""
    postmark_server_token: str = ""
    sender_postal_address: str = ""  # required by CAN-SPAM for live commercial email
    unsubscribe_email: str = ""
    unsubscribe_url: str = ""
    warmup_start_per_day: int = 5
    warmup_step_per_week: int = 5
    dispatch_max_per_day: int = 30
    blocked_recipient_tlds: list[str] = field(
        default_factory=lambda: [
            "de", "at", "fr", "it", "es", "nl", "be", "pl", "se", "dk", "fi", "ie", "pt", "cz", "gr", "hu",
            "ro", "sk", "si", "hr", "bg", "lt", "lv", "ee", "lu", "mt", "cy", "eu", "uk", "ch", "no",
        ]
    )
    imap_host: str = ""
    imap_username: str = ""
    imap_password: str = ""

    # Stripe webhooks (real-time fulfilment)
    stripe_webhook_secret: str = ""
    webhook_host: str = "127.0.0.1"
    webhook_port: int = 8443
    webhook_path: str = "/webhook"
    webhook_tolerance_seconds: int = 300

    # Resilience
    network_check_hosts: list[str] = field(default_factory=lambda: ["api.stripe.com:443", "api.github.com:443"])
    supervisor_max_restarts: int = 5
    supervisor_restart_window_seconds: int = 600
    shutdown_timeout_seconds: float = 60.0

    # Pricing engine
    price_matrix: list[int] = field(default_factory=lambda: [500, 900, 1400, 1900])
    pricing_min_views: int = 20
    pricing_window_hours: int = 48
    demand_sales_threshold: int = 3          # sales in 24h that count as strong demand (>2)
    premium_price_cents: int = 1900
    max_scrape_depth: int = 3

    # Inbound site + syndication
    github_pages_branch: str = ""            # "" = github_branch; e.g. "gh-pages"
    github_pages_dir: str = "docs"           # "" to publish at the branch root
    site_title: str = "Tech Stack Intel"
    devto_api_key: str = ""
    hashnode_token: str = ""
    hashnode_publication_id: str = ""
    github_discussions_repo: str = ""        # owner/repo with Discussions enabled
    github_discussions_category: str = "Announcements"
    syndication_publish: bool = True         # False = create drafts only (Dev.to) / skip live posting
    syndication_interval_days: int = 7
    syndication_min_companies: int = 10

    @property
    def db_path(self) -> Path:
        return self.data_dir / self.db_filename

    @property
    def stop_file(self) -> Path:
        return self.data_dir / "EMERGENCY_STOP"

    @property
    def workspace_dir(self) -> Path:
        return self.data_dir / "workspace"

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None, env: Mapping[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env
        values: dict[str, Any] = {}
        path = path or env.get(ENV_PREFIX + "CONFIG")
        if path is None and Path("automonetize.toml").exists():
            path = "automonetize.toml"
        if path:
            with open(path, "rb") as fh:
                raw = tomllib.load(fh)
            values.update(raw.get("automonetize", raw))

        known = {f.name: f for f in fields(cls)}
        for name, f in known.items():
            key = ENV_PREFIX + name.upper()
            if key in env:
                values[name] = _coerce(env[key], None if f.default is MISSING else f.default, name)
        for env_key, name in _PLAIN_ENV.items():
            if env_key in env and name not in values:
                values[name] = _coerce(env[env_key], known[name].default, name)

        unknown = set(values) - set(known)
        if unknown:
            raise ValueError(f"unknown config keys: {sorted(unknown)}")
        if "data_dir" in values:
            values["data_dir"] = Path(values["data_dir"])
        return cls(**values)

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.workspace_dir):
            d.mkdir(parents=True, exist_ok=True)


# Conventional, unprefixed variable names accepted for secrets and the dry-run switch.
_PLAIN_ENV = {
    "GUMROAD_ACCESS_TOKEN": "gumroad_access_token",
    "STRIPE_SECRET_KEY": "stripe_secret_key",
    "LEMONSQUEEZY_API_KEY": "lemonsqueezy_api_key",
    "GITHUB_TOKEN": "github_token",
    "SENDGRID_API_KEY": "sendgrid_api_key",
    "POSTMARK_SERVER_TOKEN": "postmark_server_token",
    "SMTP_PASSWORD": "smtp_password",
    "IMAP_PASSWORD": "imap_password",
    "DRY_RUN": "dry_run",
    "STRIPE_WEBHOOK_SECRET": "stripe_webhook_secret",
    "DEVTO_API_KEY": "devto_api_key",
    "HASHNODE_TOKEN": "hashnode_token",
}

_LIST_FIELDS = {"shell_allowlist", "lead_sources", "sender_skills", "blocked_recipient_tlds", "network_check_hosts"}
_JSON_FIELDS = {"niches", "price_tiers", "stripe_payment_links", "lemonsqueezy_variant_map", "price_matrix"}


def _coerce(raw: str, default: Any, name: str) -> Any:
    if name in _LIST_FIELDS:
        return [s.strip() for s in raw.split(",") if s.strip()]
    if name in _JSON_FIELDS:
        return json.loads(raw)
    if name == "data_dir":
        return Path(raw)
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return raw
