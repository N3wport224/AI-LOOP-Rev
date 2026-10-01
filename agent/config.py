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
    data_dir: Path = Path("data")        # where the database, datasets, site and logs live
    db_filename: str = "agent_state.db"  # SQLite file inside data_dir

    # Loop
    interval_seconds: int = 3600
    pivot_after_iterations: int = 24
    stale_revenue_days: int = 14            # a niche with no sale for this long is re-evaluated
    # Wall-clock floor before a zero-traction pivot. Iteration counts alone gave a product only
    # 24 hours at hourly cycles: less than one pricing window and one syndication slot.
    min_hypothesis_days: float = 10
    max_hypothesis_generations: int = 3  # how many times a niche idea may be re-tried with new parameters

    # Circuit breakers
    max_actions_per_cycle: int = 100        # the engine raises it to len(PLAN) + 10 if set lower
    max_api_calls_per_cycle: int = 200      # a runaway guard; the site publishes several new product pages per cycle
    max_consecutive_errors: int = 5
    task_retry_attempts: int = 3         # tries per task per cycle before it counts as failed

    # HTTP
    http_rate_per_minute: float = 20.0   # requests per minute to any one host (politeness limit)
    http_timeout: float = 15.0           # seconds before an HTTP request gives up
    http_backoff_seconds: float = 1.0    # first wait between HTTP retries (doubles each time)
    respect_robots_txt: bool = True      # never fetch a page a site's robots.txt disallows

    # Shell sandbox
    shell_allowlist: list[str] = field(default_factory=lambda: ["git", "python3", "ls", "cat", "echo", "zip"]) # the only programs the agent may run
    shell_timeout: float = 30.0          # seconds before a shell command is stopped

    # Revenue
    daily_target_cents: int = 1000
    gumroad_access_token: str = ""       # secret: Gumroad API token (optional second storefront)
    platform_fee_pct: float = 10.0       # marketplace fee % used to estimate net revenue
    platform_fee_fixed_cents: int = 50   # marketplace fixed fee per sale (cents)

    # Strategies
    lead_sources: list[str] = field(default_factory=lambda: ["remoteok", "arbeitnow", "hn_hiring", "remotive", "jobicy",
                                                             "himalayas", "weworkremotely"])
    niches: list[dict[str, Any]] = field(default_factory=lambda: [dict(n) for n in DEFAULT_NICHES])
    min_leads_for_asset: int = 10
    asset_price_cents: int = 900  # legacy (Phase 1); starting prices now come from price_tiers. Kept so old configs still load.
    storefront_url: str = ""

    # Outreach (drafts only, never sent automatically)
    sender_name: str = ""
    sender_email: str = ""
    sender_skills: list[str] = field(default_factory=list) # your skills, quoted in reviewed outreach drafts
    outreach_offer: str = "short-term contract help" # what reviewed outreach drafts offer
    outreach_daily_cap: int = 20         # most outreach emails per day (each one needs your OK)
    outreach_min_score: float = 0.6      # minimum lead fit (0-1) before a draft is written
    outreach_cooldown_days: int = 30     # days before the same company can be contacted again

    # Hypothesis scoring
    signal_window_iterations: int = 12   # cycles with zero views AND zero sales before a pivot
    high_urgency_threshold: int = 60

    # Tech stack intel
    intel_max_url_checks: int = 15

    # Storefronts ("auto" = stripe if a key is set, else lemonsqueezy if configured, else gumroad staging)
    storefront_provider: str = "auto"
    price_tiers: list[list[int]] = field(default_factory=lambda: [[0, 900], [25, 1400], [75, 1900]])
    currency: str = "usd"                # currency of every price (ISO code)
    stripe_secret_key: str = ""          # secret: Stripe API key (sk_live_/rk_live_ for real sales)
    stripe_payment_links: dict[str, str] = field(default_factory=dict)  # niche -> pre-made Payment Link URL
    stripe_fee_pct: float = 2.9
    stripe_fee_fixed_cents: int = 30
    lemonsqueezy_api_key: str = ""       # secret: Lemon Squeezy API key (optional storefront)
    lemonsqueezy_store_id: str = ""
    lemonsqueezy_variant_id: str = ""                                  # shared "dataset" variant
    lemonsqueezy_variant_map: dict[str, str] = field(default_factory=dict)  # niche -> dedicated variant id
    lemonsqueezy_fee_pct: float = 5.0    # Lemon Squeezy fee % used to estimate net revenue
    lemonsqueezy_fee_fixed_cents: int = 50 # Lemon Squeezy fixed fee per sale (cents)
    allow_manual_fulfillment: bool = False  # publish checkouts even when the agent cannot email the file

    # GitHub (showcase directory, gists, Pages landers, traffic-based view tracking)
    github_token: str = ""
    github_showcase_repo: str = ""   # owner/repo; samples go to showcase/<niche>/
    github_showcase_mode: str = "repo"  # repo | gist
    github_pages_repo: str = ""      # owner/repo serving GitHub Pages; landers go to docs/<niche>/
    github_branch: str = "main"          # branch for GitHub commits (showcases; Pages unless github_pages_branch)
    pages_base_url: str = ""         # e.g. https://you.github.io/datasets

    # Email dispatch (cold outreach needs approval; everything is dry-run until dry_run = false)
    dry_run: bool = True
    email_backend: str = ""          # smtp | sendgrid | postmark
    outreach_email_backend: str = "smtp"
    smtp_host: str = ""
    smtp_port: int = 587                 # 587 (STARTTLS) or 465 (TLS)
    smtp_username: str = ""
    smtp_password: str = ""              # secret: SMTP password (Gmail: an app password)
    sendgrid_api_key: str = ""           # secret: SendGrid key (with email_backend = "sendgrid")
    postmark_server_token: str = ""      # secret: Postmark token (with email_backend = "postmark")
    sender_postal_address: str = ""  # required by CAN-SPAM for live commercial email
    unsubscribe_email: str = ""          # mailbox for unsubscribe replies (default: sender_email)
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
    imap_password: str = ""              # secret: IMAP password for reading replies (default: smtp_password)

    # Stripe webhooks (real-time fulfilment)
    stripe_webhook_secret: str = ""
    webhook_host: str = "127.0.0.1"
    webhook_port: int = 8443
    webhook_path: str = "/webhook"
    webhook_tolerance_seconds: int = 300 # oldest Stripe webhook signature accepted

    # Resilience
    network_check_hosts: list[str] = field(default_factory=lambda: ["api.stripe.com:443", "api.github.com:443"])
    supervisor_max_restarts: int = 5     # worker crashes allowed within the window before it's given up
    supervisor_restart_window_seconds: int = 600 # window for counting crashes
    shutdown_timeout_seconds: float = 60.0
    # Systemic breaker trips quarantine the engine instead of stopping it for good: an alert, a
    # cooldown (doubling on repeat trips within 24h, capped), then a self-diagnostic and resume.
    quarantine_hours: float = 2.0
    quarantine_max_hours: float = 24.0
    alert_notifications: bool = True         # macOS Notification Center banner for alerts

    # Unattended operation (macOS)
    public_webhook_url: str = ""             # https://<tunnel hostname>/webhook, set by setup_tunnel.sh
    power_assertions: bool = True            # hold off idle sleep while a cycle or webhook is in flight
    schedule_wake: bool = True               # ask pmset to wake the Mac for the next cycle (needs sudo -n)
    digest_push: bool = True                 # one-line daily summary to your phone (needs ntfy_topic)
    # Product factory (strategies/product_factory.py)
    product_factory: bool = True             # make new products from slices of the collected postings
    factory_interval_seconds: int = 600      # one new product this often (the factory worker under supervise)
    factory_min_rows: int = 20               # a product needs at least this many postings...
    factory_min_companies: int = 8           # ...from at least this many companies
    factory_max_age_days: int = 60           # only postings this recent go into a product
    factory_max_live: int = 500              # most factory products on sale at once
    factory_retire_days: int = 60            # a product with no sale after this long is retired
    factory_prices: list[int] = field(default_factory=lambda: [500, 900, 1400])  # cents: <60 rows, <200, larger
    # More ways to get paid (strategies/revenue_models.py)
    marketing_engine: bool = True            # plan traffic plays daily: automatic ones run, drafts wait for you
    marketing_drafts_per_day: int = 3        # most ready-to-post drafts queued per day
    affiliate_rate: float = 0.30             # affiliates earn this share of the sales they bring
    github_samples_repo: str = ""            # owner/repo for free 10-row teasers linking to the products
    related_offers: bool = True              # one "more on the same topic" email 5-12 days after an order
    offer_custom_request: bool = True        # $29 custom dataset built to order
    offer_pay_what_you_want: bool = True     # name-your-price supporter product (from $3)
    offer_sponsorship: bool = True           # $49/week sponsor line, shown only after you approve it
    offer_lifetime: bool = True              # $149 lifetime pass: every dataset now and future
    offer_gift: bool = True                  # $25 gift card codes
    battery_saver: bool = True               # on battery, heavy builds wait until the Mac is plugged in

    # Pricing engine
    price_matrix: list[int] = field(default_factory=lambda: [900, 1400, 1900])
    pricing_min_views: int = 20
    pricing_window_hours: int = 48
    demand_sales_threshold: int = 3          # sales in 24h that count as strong demand (>2)
    premium_price_cents: int = 1900
    max_scrape_depth: int = 3

    # Inbound site + syndication
    github_pages_branch: str = ""            # "" = github_branch; e.g. "gh-pages"
    github_pages_dir: str = "docs"           # "" to publish at the branch root
    site_title: str = "Tech Stack Intel"
    devto_api_key: str = ""              # secret: Dev.to key for articles
    hashnode_token: str = ""             # secret: Hashnode token for articles
    hashnode_publication_id: str = ""
    github_discussions_repo: str = ""        # owner/repo with Discussions enabled
    github_discussions_category: str = "Announcements"
    syndication_publish: bool = True         # False = create drafts only (Dev.to) / skip live posting
    syndication_interval_days: int = 5      # hard floor of 5 days per platform, whatever is configured
    syndication_min_companies: int = 10
    hn_tracker_enabled: bool = True
    hn_gist_refresh_hours: int = 24
    og_images: bool = True                   # PNG OpenGraph cards (needs Pillow); SVG badges always

    # Recurring subscriptions (Stripe)
    subscription_price_cents: int = 1000     # 0 disables the subscription tier
    subscription_interval: str = "month"     # month | week
    subscription_delivery_weekday: int = 0   # 0 = Monday
    subscription_delivery_hour: int = 8  # hour (subscription_timezone) the weekly updates go out
    subscription_timezone: str = "UTC"

    # Free lead magnet (10-record sample by email, then a Monday "Weekly Tech Pulse")
    lead_magnet_enabled: bool = True
    lead_magnet_double_opt_in: bool = True   # the weekly pulse only goes to addresses that confirmed
    lead_magnet_sample_size: int = 10
    lead_magnet_max_per_hour: int = 60       # global cap on captures (abuse brake)
    lead_magnet_max_per_ip_hour: int = 5
    lead_nurture_weekday: int = 0            # 0 = Monday
    lead_nurture_hour: int = 9               # in subscription_timezone
    lead_nurture_signals: int = 3

    # Programmatic SEO matrix pages (intel/companies-hiring-<tech>-engineers.html ...)
    seo_matrix_enabled: bool = True
    seo_min_companies: int = 5               # no thin pages: skip a technology below this
    seo_min_migrations: int = 3
    seo_max_pages: int = 60
    indexnow_enabled: bool = True            # submit changed URLs to IndexNow (Bing, Yandex, Seznam...)
    indexnow_key: str = ""                   # generated on first use when empty

    # Local control GUI
    gui_host: str = "127.0.0.1"
    gui_port: int = 8080

    # Developer API tier (metered REST API on the public listener, /v1/...)
    api_enabled: bool = True
    api_price_cents: int = 2900              # per month; 0 disables the tier
    api_daily_quota: int = 500
    api_degraded_quota: int = 50             # while an invoice is failing (Stripe is retrying)
    api_burst: int = 10                      # token bucket: burst size...
    api_rate_per_second: float = 2.0         # ...and sustained rate per key
    api_max_page_size: int = 100
    api_auth_failures_per_ip_hour: int = 30  # brakes on key guessing

    # Executive Migration Dossier (one-off, per company)
    dossier_price_cents: int = 4900          # 0 disables the tier
    dossier_min_score: int = 75              # sold only when max(urgency, intent) is above this
    dossier_pulse_min_intent: int = 80       # Tech Pulse shows a dossier button above this intent score

    # Retention / dunning
    dunning_grace_days: int = 7              # API keys stay degraded (not revoked) this long after a failed payment
    dunning_reminder_days: list[int] = field(default_factory=lambda: [0, 3, 6])
    recovery_per_ip_hour: int = 3            # /v1/orders/recover
    recovery_per_email_day: int = 3

    # Autonomous source discovery
    source_discovery_enabled: bool = True
    source_discovery_interval_hours: int = 24
    source_trial_days: int = 3               # healthy probes on this many separate days before activation
    source_min_valid_ratio: float = 0.6
    source_max_error_rate: float = 0.2
    source_max_active: int = 20
    source_probes_per_run: int = 5
    source_seed_feeds: list[str] = field(default_factory=list)  # extra RSS/JSON feed URLs to evaluate

    # Landing-page copy bandit
    copy_bandit_enabled: bool = True
    copy_bandit_algorithm: str = "epsilon_greedy"   # epsilon_greedy | thompson
    copy_bandit_epsilon: float = 0.2                # 80% to the winner, 20% exploring
    copy_bandit_min_views: int = 200                # per variant before it can be deprecated
    copy_bandit_deprecate_sd: float = 2.0

    # Satellite niches (run alongside the primary hypothesis)
    max_active_niches: int = 3               # primary + satellites
    satellite_window_days: int = 14          # revenue window for capacity allocation
    satellite_min_share: float = 0.15        # every niche keeps at least this share of capacity
    satellite_max_days_without_revenue: int = 21

    # Owner reports (strategies/owner_reports.py): sale emails, alerts, a daily digest
    owner_reports: bool = True
    owner_email: str = ""                    # "" = sender_email
    owner_digest_hour: int = 8               # local hour (subscription_timezone) after which the digest goes out

    # All-datasets bundle (strategies/bundle_engine.py)
    bundle_min_niches: int = 2
    bundle_discount: float = 0.4             # 40% off the sum of the parts

    # Growth & bookkeeping (strategies/buyer_followup.py, strategies/bookkeeping.py)
    buyer_followup: bool = True              # one "did it arrive?" email per order
    buyer_followup_days: int = 3             # days after delivery
    bookkeeping: bool = True                 # monthly revenue CSV emailed to owner_email

    # Storefront health, release emails, freshness (Phases 24-27)
    storefront_check_hours: float = 6.0      # how often checkout links, pages and downloads are checked
    release_announcements: bool = True       # email past buyers when a new niche goes on sale
    announce_min_gap_days: int = 14          # at most one announcement per buyer in this many days
    stale_after_days: int = 7                # a dataset with no new postings this long is not promoted

    # Self-update (agent/self_update.py)
    auto_update: bool = True                 # install verified updates of the branch this checkout tracks
    auto_update_hours: float = 6.0

    # Referral rewards and subscriber win-back (Phases 34-35)
    referrals: bool = True                   # personal referral links in follow-ups; referrers get the newest version free
    winback: bool = True                     # one discounted invitation back after a subscription is canceled
    winback_after_days: int = 7
    winback_discount_pct: int = 50           # off the first month back

    # Upgrade credit, sample offer, quarterly sale (Phases 36-39)
    bundle_upgrade: bool = True              # past buyers: the bundle with what they paid counted
    bundle_upgrade_after_days: int = 10
    sample_offer: bool = True                # free-sample signups: one single-use discount
    sample_offer_after_days: int = 14
    sample_offer_pct: int = 25
    seasonal_sale: bool = True               # a short store-wide sale every few months
    sale_pct: int = 25
    sale_days: int = 3
    sale_every_days: int = 90
    sale_min_store_age_days: int = 30

    # Contact policy, bounces, offer tuning, privacy (Phases 40-44)
    promo_daily_cap: int = 150               # marketing emails per day in total (tools/contact_policy.py)
    bounce_pause_rate: float = 0.05          # pause marketing email for a week above this hard-bounce rate
    offer_tuning: bool = True                # adjust offer discounts from their measured sales

    # Money admin (Phases 85-89)
    tax_set_aside_pct: int = 25              # books suggest setting this share of profit aside for taxes
    payout_watch_days: int = 30              # alert if money sits in Stripe this long without a payout

    # Site trust (Phases 80-84)
    refund_alert_rate: float = 0.10          # to-do when more than this share of 30 days' orders is refunded
    refund_policy_days: int = 14             # stated on the refunds page and in the FAQ
    google_site_verification: str = ""       # the code Google Search Console gives you
    bing_site_verification: str = ""         # the code Bing Webmaster Tools gives you

    # Remote control (Phases 60-64)
    ntfy_topic: str = ""                     # phone notifications via ntfy.sh (`automonetize phone`)
    ntfy_server: str = ""                    # "" = https://ntfy.sh
    sale_alerts: str = "each"                # each | daily (digest only) | off
    owner_digest: str = "daily"              # daily | weekly (Mondays) | off
    owner_commands: bool = True              # email commands from owner_email with the command code

    # Plans and conversion (Phases 55-59)
    team_license: bool = True
    team_license_multiplier: float = 3.0     # team price = dataset price x this, whole dollars
    team_license_seats: int = 10
    annual_plan: bool = True
    annual_months_paid: int = 10             # yearly = 10 x monthly ("2 months free")
    checkout_thank_you: bool = True          # after payment, Stripe sends buyers to the site's thank-you page
    checkout_thank_you_live: bool = False    # internal: set by the agent once the page is confirmed online
    blocked_signup_domains: list[str] = field(default_factory=list)  # extra disposable domains to refuse

    # Delivery & payments (Phases 50-54)
    download_link_days: int = 7              # large files: private link lifetime
    download_link_uses: int = 5
    webhook_check_hours: float = 6.0
    card_testing_threshold: int = 10         # failed charges in an hour that count as an attack
    stripe_automatic_tax: bool = False       # switched on by itself when Stripe Tax is active on the account

    # Housekeeping and disk (Phases 47-48)
    log_keep_days: int = 90                  # actions kept this long, errors twice as long
    lead_keep_days: int = 365                # job postings not seen for this long are deleted
    unconfirmed_signup_days: int = 30        # free sign-ups that never confirmed are deleted after this
    keep_versions: int = 3                   # dataset versions kept per niche (sold ones always kept)
    disk_warn_gb: float = 2.0
    disk_critical_gb: float = 0.5            # below this, housekeeping runs at once and builds pause

    # Heartbeat, release gate, refresh offers, launch codes (Phases 28-31)
    heartbeat_url: str = ""                  # e.g. https://hc-ping.com/<uuid>: emails you if the agent goes quiet
    release_gate_max_drop: float = 0.5       # hold a new version that lost more than this share of rows
    release_gate_hold_days: int = 3          # ...unless the drop persists this long (then it's real)
    refresh_offers: bool = True              # past buyers get the updated dataset at a discount
    refresh_after_days: int = 30             # earliest, after their purchase
    refresh_min_new_rows: int = 25           # only when the dataset grew by at least this many rows
    refresh_discount_pct: int = 50
    launch_promos: bool = True               # time-limited launch code for each new niche
    launch_discount_pct: int = 20
    launch_promo_days: int = 7

    # Backups (agent/backup.py)
    backups_enabled: bool = True
    backup_dir: str = ""                     # "" = ~/Library/Application Support/AutoMonetize/backups (macOS)
    backup_keep_days: int = 14

    # Autonomous code evolution (agent/evolution): off until explicitly switched on
    enable_autonomous_code_evolution: bool = False
    evolution_repo: str = ""                 # git checkout to evolve; "" = the one this code runs from
    evolution_interval_hours: float = 6.0    # at most one attempt per interval
    evolution_cooldown_hours: float = 24.0   # after a failed attempt or a rollback
    evolution_canary_minutes: int = 60       # post-merge watch window
    evolution_check_timeout_seconds: int = 1200
    evolution_run_simulator: bool = True     # also run `test-full-loop` in the worktree
    evolution_min_tag_postings: int = 5      # an unknown tag must appear this often to be learned
    evolution_plateau_cycles: int = 5        # cycles without a checkout before copy is mutated

    @property
    def lead_capture_base(self) -> str:
        """Public origin that serves /lead-magnet/* (the tunnel hostname), or "" when not set up."""
        from urllib.parse import urlsplit

        parts = urlsplit(self.public_webhook_url)
        return f"{parts.scheme}://{parts.netloc}" if parts.scheme == "https" and parts.netloc else ""

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
        prefixed: set[str] = set()
        for name, f in known.items():
            key = ENV_PREFIX + name.upper()
            if key in env:
                values[name] = _coerce(env[key], None if f.default is MISSING else f.default, name)
                prefixed.add(name)
        for env_key, name in _PLAIN_ENV.items():
            # Conventional names (.env, the GUI) override the TOML file, as the precedence order says;
            # an AUTOMONETIZE_* variable still wins, and a blank .env line never wipes a TOML value.
            if env_key in env and name not in prefixed and (env[env_key].strip() or name not in values):
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
    "PUBLIC_WEBHOOK_URL": "public_webhook_url",
    "ENABLE_AUTONOMOUS_CODE_EVOLUTION": "enable_autonomous_code_evolution",
    "OWNER_EMAIL": "owner_email",
    "HEALTHCHECK_URL": "heartbeat_url",
    "NTFY_TOPIC": "ntfy_topic",
    # Aliases accepted for hand-edited .env files (and the names the GUI shows).
    "STRIPE_API_KEY": "stripe_secret_key",
    "SMTP_HOST": "smtp_host",
    "SMTP_PORT": "smtp_port",
    "SMTP_USER": "smtp_username",
    "SMTP_USERNAME": "smtp_username",
    "CAN_SPAM_POSTAL_ADDRESS": "sender_postal_address",
    "CAN_SPAM_UNSUBSCRIBE_EMAIL": "unsubscribe_email",
}

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}

_LIST_FIELDS = {"shell_allowlist", "lead_sources", "sender_skills", "blocked_recipient_tlds", "network_check_hosts",
                "blocked_signup_domains"}
_JSON_FIELDS = {"niches", "price_tiers", "stripe_payment_links", "lemonsqueezy_variant_map", "price_matrix", "dunning_reminder_days",
                "factory_prices"}


def _coerce(raw: str, default: Any, name: str) -> Any:
    if name in _LIST_FIELDS:
        return [s.strip() for s in raw.split(",") if s.strip()]
    if name in _JSON_FIELDS:
        return json.loads(raw)
    if name == "data_dir":
        return Path(raw)
    if isinstance(default, bool):
        # Only an explicit token flips a switch. An empty or unrecognised value (a blanked
        # DRY_RUN= line, a typo) keeps the safe default instead of silently becoming False.
        value = raw.strip().lower()
        if value in _TRUE:
            return True
        if value in _FALSE:
            return False
        return default
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return raw
