# AutoMonetize

An autonomous, goal-directed agent loop that works toward **$10.00/day of verified net
revenue** with zero startup capital. Every cycle it:

1. collects hiring data from public job-board APIs;
2. turns it into **company-level tech-stack intelligence** (stack fingerprints, hiring-intent
   signals, urgency scores, verified careers URLs) packaged as a paid dataset;
3. **publishes** it: a live checkout (Stripe Payment Link or Lemon Squeezy), an SEO lander
   with `schema.org/Product` markup on GitHub Pages, and a sanitized free preview;
4. **distributes** it inbound, with no approval needed: a weekly data-driven "Tech Radar"
   article syndicated to Dev.to, Hashnode, GitHub Discussions and an RSS feed, linking back
   to the lander and checkout. (Cold outreach still exists, but only sends drafts a human approved.)
5. **sells in real time**: a signature-verified Stripe webhook records verified revenue and
   emails the buyer their zip plus a receipt seconds after payment; polling reconciles anything missed;
6. **optimizes price**: experiments across $5/$9/$14/$19 per dataset, steps down or bundles
   2-for-1 when traffic doesn't convert, steps up and converges when it does, and on strong
   demand scrapes deeper and ships a $19 premium deep-dive add-on;
7. **scores** each hypothesis on its funnel and pivots to an adjacent, higher-demand stack
   cluster when it isn't converting.

It runs as a supervised daemon (engine + webhook listener) under launchd or systemd,
survives crashes, reboots, sleep and network drops, and shuts down cleanly on SIGTERM.

> **Reality check.** None of this guarantees income. Once configured, the sales path
> (checkout → payment → delivery → revenue) and inbound publishing run without anyone
> watching. What stays human: one-time account setup, approving any cold email, and exposing
> the webhook endpoint publicly (a tunnel or host, see below). Only revenue confirmed by a
> payment provider counts toward the target.

## How it works

```
 guard → observe → reflect/pivot → plan → act (≤ max_actions_per_cycle, 3 attempts per task)
                                            │
  aggregate_leads → build_intel → package_asset → publish_listing → publish_showcase
  → syndicate → build_site → stage_outreach → dispatch_outreach → sync_revenue
  → deliver_orders → collect_metrics → optimize_pricing

 in parallel (supervised thread): Stripe webhook → verify → record revenue → email zip + receipt
```

| Task | Module | What it does |
|---|---|---|
| `aggregate_leads` | `strategies/b2b_lead_aggregator.py` | Remote OK, Arbeitnow and HN "Who is hiring" APIs → validated, enriched, deduplicated leads. Every valid lead also goes into a demand pool used for pivots. |
| `build_intel` | `strategies/tech_stack_intel.py` | Per company: stack fingerprint (languages, frameworks, databases, data platform, cloud, infra, observability, AI/ML), intent triggers (migration, legacy refactor, ERP integration, new team, greenfield, scaling, urgent hire, funding), urgency score 0-100, careers URL verified with a live HTTP 2xx. Writes `tech_radar.json/.csv` and `EXECUTIVE_TECH_RADAR.md`. |
| `package_asset` | `strategies/digital_asset_packager.py` | Versioned zip (radar + CSV/JSON + role directory + attribution), listing with a starting price by company count, 5-record sanitized sample. |
| `publish_listing` | `strategies/distribution_engine.py` + `tools/storefront/` | Opens or reuses a checkout on the active storefront and deploys the lander (local `data/site/`, optionally GitHub Pages). Won't sell what it can't deliver. |
| `publish_showcase` | `strategies/distribution_engine.py` | Sanitized preview with the checkout link → `data/showcase/<niche>/`, plus GitHub repo `showcase/<niche>/README.md` or a public Gist. |
| `syndicate` | `strategies/inbound_syndicator.py`, `tools/syndicator.py` | This week's radar article → RSS always; Dev.to / Hashnode / GitHub Discussions when configured (max one post per platform per week); Substack paste-ready file. |
| `build_site` | `strategies/inbound_syndicator.py`, `tools/page_builder.py` | Product page per dataset, bundle and add-on (JSON-LD, OG tags, canonical, KPIs, sample, CTA) plus index, `sitemap.xml`, `robots.txt`, `feeds/radar.xml`; committed to GitHub Pages. |
| `stage_outreach` | `strategies/outreach_stager.py` | Personalized drafts → `pending_review`. |
| `dispatch_outreach` | `tools/dispatcher.py`, `tools/inbox.py` | Honours unsubscribe replies (IMAP), then sends **approved** drafts. Dry run by default. |
| `sync_revenue` | `tools/revenue_tracker.py` | Polls Stripe sessions, Lemon Squeezy orders and Gumroad sales; records verified net revenue and attributes it to a hypothesis. |
| `deliver_orders` | `strategies/distribution_engine.py` | Emails each paid order its zip. |
| `collect_metrics` | `strategies/distribution_engine.py` | Impressions (sent outreach, live surfaces, syndicated posts), views (GitHub traffic on the showcase path), purchases. |
| `optimize_pricing` | `agent/pricing_engine.py` | Price experiments, bundles, demand-driven depth and premium add-ons (see below). |

### Hypotheses, scoring and pivots (`agent/hypotheses.py`)

Each hypothesis is a niche plus its keyword cluster. After every cycle the engine stores a
funnel score (`impressions`, `views`, `purchases`, `conversion` = purchases/views,
`velocity` = purchases per cycle). It deprecates a hypothesis when:

* **no views and no sales after `signal_window_iterations` (12) cycles**, once view tracking
  works (needs a GitHub showcase repo and token; without it this rule stays off rather than
  firing on missing data);
* **no verified revenue after `pivot_after_iterations` (24) cycles**;
* a task proves the niche can't work (for example, too few listings exist).

The next hypothesis comes from, in order:
1. **clusters adjacent** to the one just dropped (e.g. Python → AI Infrastructure, Data
   Engineering, Cloud Security), ranked by how many pooled roles matched them in the last 7 days;
2. configured niches;
3. new variants mined from the most frequent hiring tags;
4. broadened revisits of dropped niches.

## Installation

Requires Python 3.11+.

```bash
git clone <this repo> AutoMonetize && cd AutoMonetize
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
automonetize init          # writes automonetize.toml (commented) and data/agent_state.db
cp .env.example .env       # secrets go here, never in the TOML
pytest                     # 222 tests, ~10 s, no network
```

Existing databases from earlier versions are migrated automatically on open.

## Storefront setup

The agent publishes to the first storefront that has credentials (`storefront_provider = "auto"`),
polls orders from **all** configured ones, and falls back to Gumroad staging when none are set.

### Stripe (recommended: fully closed loop)

1. Create a Stripe account and complete activation (needed for live payments).
2. Dashboard → Developers → API keys → create a **restricted key** with write access to
   *Products*, *Prices* and *Payment Links* and read access to *Checkout Sessions*.
   (A standard secret key also works.)
3. `STRIPE_SECRET_KEY=rk_live_…` in `.env`. Use an `rk_test_…`/`sk_test_…` key first; test
   links accept Stripe's test cards.

Per dataset the agent creates a Product, a Price and a Payment Link (idempotency keys make
retries safe). New versions at the same price reuse the link; a price change creates a new
link and deactivates the old one. Orders are the link's completed Checkout Sessions, which
include the buyer's email, so the agent delivers the zip itself. Fees are estimated at
2.9% + 30¢ (`stripe_fee_pct`, `stripe_fee_fixed_cents`).

No API key? Create Payment Links by hand and map them in `[automonetize.stripe_payment_links]`.
Landers will use them, but orders can't be polled, so record sales with `revenue add --verified`.

### Lemon Squeezy

Lemon Squeezy's API can **read** products, variants and files but can't create them, and
can't upload files. It **can** create checkouts with a custom price, name and description.
So:

1. In the dashboard, create one product (e.g. "Tech Stack Intel") with a variant. Either use
   a single shared variant, or create one variant per niche and attach that niche's zip to it.
2. Settings → API → create a key. Set `LEMONSQUEEZY_API_KEY`, `lemonsqueezy_store_id` and
   `lemonsqueezy_variant_id` (or `[automonetize.lemonsqueezy_variant_map]` per niche).

The agent verifies the variant (`GET /v1/variants/:id`), checks for an attached file
(`GET /v1/files?filter[variant_id]=`), creates a checkout per dataset (`POST /v1/checkouts`
with `custom_price` in the $5-$19 band) and polls `GET /v1/orders`.

**Attribution caveat:** checkout custom data is only returned in webhooks, not by the orders
API. Orders are matched by variant (exact with dedicated variants) or by product name (best
effort). Unmatched orders still count as verified revenue but are flagged
`needs_manual_delivery`. Deliver them with `automonetize orders deliver <order> <asset>`.
Dedicated variants with the file attached let Lemon Squeezy deliver the file itself.

### Gumroad (fallback)

With no Stripe or Lemon Squeezy credentials, listings are staged for manual upload as
before. Set `storefront_url` once the product exists, and `GUMROAD_ACCESS_TOKEN` to sync sales.

### GitHub showcase, landers and view tracking

Create a fine-grained token with **Contents: read/write** on your showcase/pages repos
(and **Gists: write** for gist mode). For view tracking the token needs push access to the
showcase repo, because GitHub's traffic API requires it. Then set:

```toml
github_showcase_repo = "you/datasets"      # previews at showcase/<niche>/README.md
github_pages_repo    = "you/you.github.io" # landers at docs/<niche>/index.html
pages_base_url       = "https://you.github.io"
```

Views come from `GET /repos/{repo}/traffic/popular/paths` (top 10 paths, rolling 14 days).
GitHub exposes no view counts for Gists or Pages sites, so gist mode publishes but can't
feed the views rule. `automonetize serve` hosts `data/site/` locally for previewing landers.

## Real-time fulfilment: Stripe webhooks

`tools/storefront/webhook_listener.py` runs an aiohttp server (default
`127.0.0.1:8443/webhook`, plus `GET /healthz`) inside the supervisor. For every request it:

1. verifies the `Stripe-Signature` header with `stripe.Webhook.construct_event` and
   `STRIPE_WEBHOOK_SECRET`, rejecting anything unsigned, tampered or older than 5 minutes
   (replay protection);
2. deduplicates on the event id, since Stripe delivers at least once;
3. matches the Checkout Session to its dataset by `payment_link` (or `metadata.asset_id`) and
   takes the buyer from `customer_details.email`;
4. records verified net revenue (the daily goal updates at once);
5. answers 200, then emails the zip and a receipt in the background. An atomic claim
   (`paid → delivering`) means the webhook and the engine's `deliver_orders` sweep can never
   send twice; failed sends stay `paid` and are retried.

Handled events: `checkout.session.completed` (paid now, or pending for delayed payment
methods), `checkout.session.async_payment_succeeded`, `payment_intent.succeeded` (completes a
pending session; a bare PaymentIntent carries no dataset or email, so it never creates an
order on its own), and `checkout.session.expired` (feeds the cart drop-off metric).
Polling (`sync_revenue`) stays on as reconciliation: it uses the same order id, so a sale is
never counted twice, and it catches anything sent while the machine was asleep.

### Local development with the Stripe CLI

```bash
brew install stripe/stripe-cli/stripe && stripe login
stripe listen --forward-to localhost:8443/webhook \
  --events checkout.session.completed,checkout.session.async_payment_succeeded,checkout.session.expired,payment_intent.succeeded
# copy the printed "whsec_..." into .env as STRIPE_WEBHOOK_SECRET, then in another terminal:
automonetize webhook --selftest          # checks the secret round-trips
automonetize supervise                   # engine + listener (or: automonetize webhook for the listener only)
stripe trigger checkout.session.completed
```

### Production

Stripe must reach the endpoint over public HTTPS. The listener binds to localhost on
purpose; expose it with a tunnel (Cloudflare Tunnel's free tier, or ngrok) or run on a
small VPS behind a reverse proxy. Then Dashboard → Developers → Webhooks → *Add endpoint*
`https://<your-host>/webhook` with the four events above, and put that endpoint's signing
secret in `STRIPE_WEBHOOK_SECRET`. If the endpoint is unreachable for a while, Stripe retries
for up to 3 days and polling covers the gap.

## Inbound distribution

* **Landers** (`tools/page_builder.py`): one page per dataset, bundle and premium add-on,
  with `<title>`, meta description, canonical, Open Graph and product price tags, a
  `schema.org/Product` + `Offer` JSON-LD block (escaped so it can't break out of its
  `<script>`), urgency KPIs, the 5-record sample and the Payment Link. Plus `index.html`,
  `sitemap.xml`, `robots.txt` and an RSS 2.0 feed at `feeds/radar.xml`. Written to
  `data/site/` and committed to `github_pages_repo`, on `github_pages_branch` (e.g.
  `gh-pages`) under `github_pages_dir` (`docs` by default, `""` for the root). Unchanged
  files make no commit. Rebuild by hand with `automonetize site`.
* **Syndication** (`tools/syndicator.py`): a weekly article from the active niche's radar,
  with a headline that only claims what the data shows (e.g. *"Weekly Tech Radar: Top 10
  Python Companies Planning Migrations (PostgreSQL Leads Their Stacks)"*), a top-10 table,
  stack adoption, signals, method, and a footer linking the free preview, the checkout and
  the canonical lander.

| Channel | Setup | Notes |
|---|---|---|
| RSS | none | Always on; Substack, Medium and newsletter tools can import from it |
| Dev.to | `DEVTO_API_KEY` (Settings → Extensions) | Sets `canonical_url`; `syndication_publish = false` makes drafts |
| Hashnode | `HASHNODE_TOKEN` + `hashnode_publication_id` | GraphQL `publishPost` with `originalArticleURL` |
| GitHub Discussions | `github_discussions_repo` (Discussions enabled) + token with Discussions write | Category from `github_discussions_category` |
| Substack | none | No public posting API: paste-ready file in `data/syndication/substack/` |

At most one post per platform every `syndication_interval_days` (7), each article once, and
only when at least `syndication_min_companies` (10) back it. Contact details are never included.
`automonetize syndicate [--drafts]` runs it on demand.

## Pricing engine

`agent/pricing_engine.py` runs one price experiment per live dataset, each with its own
Stripe Price and Payment Link, so every order maps to the price that produced it. Links from
earlier experiments stay attributable. It tracks showcase views gained since the experiment
started, checkout initiations (all Checkout Sessions: open, complete or expired), completed
orders and cart drop-off.

| Condition | Action |
|---|---|
| > 20 views, 0 orders, experiment ≥ 48h old | new Price one tier down ($19 → $14 → $9 → $5) |
| same at $5 | 2-for-1 bundle with another niche's dataset (prefers adjacent clusters), at the higher price |
| ≥ 2 orders at the current price | test one tier up, unless it already earned less per view |
| 1 order in > 20 views after 48h | explore the cheaper tier once, then settle on the best |
| neighbouring tiers tested and worse | **converge**: hold the best revenue-per-view tier |
| > 2 sales in 24h for the niche | scraping depth +1 (more Arbeitnow pages, more HN comments, broader keywords) and a $19 premium deep-dive (per-company profiles), at most weekly |

Revenue per view uses a smoothed conversion estimate, so a single lucky sale can't lock a
price in. The tests drive the decision rule against three demand curves and check it
converges on the best tier each time. Needs a Stripe secret key (or Lemon Squeezy); Gumroad
prices are manual. `automonetize pricing status` shows every experiment.

## Email: outreach and delivery

```toml
dry_run = true                    # nothing is sent until false (or DRY_RUN=false)
outreach_email_backend = "smtp"   # cold outreach: your own mailbox
email_backend = "postmark"        # order delivery (transactional); defaults to the outreach backend
smtp_host = "smtp.fastmail.com"
smtp_username = "you@yourdomain.com"   # SMTP_PASSWORD in .env
sender_name = "Sam Dev"
sender_email = "you@yourdomain.com"
sender_postal_address = "1 Main St, Springfield, IL 62701, USA"
unsubscribe_url = ""              # https URL adds RFC 8058 one-click unsubscribe
imap_host = "imap.fastmail.com"   # IMAP_PASSWORD in .env; unsubscribe replies are honoured automatically
```

* **Dry run.** Every would-be email (headers, body, attachment sizes) is appended to
  `data/dispatched_audit.log` as JSON lines. Live sends and failures are logged there too.
* **Approval gate.** Only drafts you `approve` are ever sent:
  `automonetize outreach list --full` → `outreach approve 3 5` → the next cycle sends them.
* **Warm-up.** 5 cold emails/day, +5 per full week since the first live send, capped at
  `dispatch_max_per_day`. Three failures in a run stop sending, to protect sender reputation.
* **CAN-SPAM.** Live outreach refuses to start without sender name/email, a physical postal
  address and an unsubscribe address. Messages carry `List-Unsubscribe` (plus one-click
  when configured), a footer with your address and opt-out instructions, and the subject
  you approved. Unsubscribe replies ("unsubscribe", "no thanks", "remove me", …) are
  suppressed automatically, or add them with `automonetize suppress add`.
* **EU/UK recipients are blocked by default** (`blocked_recipient_tlds`). Unsolicited B2B
  email there generally needs prior consent. Only remove entries after checking the law yourself.
* **Provider policies.** SendGrid and Postmark prohibit sending to people who didn't opt in,
  so cold outreach defaults to your own SMTP mailbox. Use them for order delivery
  (transactional), which is what they're for.
* **Delivery** emails attach the purchased zip. The agent won't open a Stripe checkout until
  delivery works (`dry_run = false` plus an email backend), unless you set
  `allow_manual_fulfillment = true`.

Check before going live: `automonetize dispatch --check` lists any missing requirement.

## Headless production launch

```bash
# 1. configure, then verify each piece
automonetize dispatch --check          # email compliance + mode + today's limit
automonetize run --once                # one supervised cycle; read the dashboard
automonetize orders list; tail data/dispatched_audit.log

# 2. go live
sed -i 's/^DRY_RUN=.*/DRY_RUN=false/' .env

# 3. run unattended: the supervisor runs the engine + webhook listener (pick one)
deploy/install_launchd.sh              # macOS (below)
cp deploy/automonetize.service ~/.config/systemd/user/ && \
  systemctl --user daemon-reload && systemctl --user enable --now automonetize   # Linux
# or in a terminal: automonetize supervise
# (engine only, no webhooks: deploy/crontab.example or `automonetize run --headless`)

# 4. watch it
automonetize dashboard --watch         # or: automonetize status --json
tail -f ~/Library/Logs/automonetize.stdout.log   # macOS; Linux: journalctl --user -u automonetize -f
```

### macOS: launchd

```bash
deploy/install_launchd.sh              # renders deploy/com.automonetize.agent.plist into
                                       # ~/Library/LaunchAgents/, lints it, bootstraps and starts it
launchctl print gui/$(id -u)/com.automonetize.agent | head -30   # state, PID, last exit
launchctl kickstart -k gui/$(id -u)/com.automonetize.agent        # restart
deploy/install_launchd.sh uninstall    # stop and remove
```

The agent starts at login (`RunAtLoad`), restarts whenever it exits (`KeepAlive = true`,
at most every 30 s via `ThrottleInterval`), and logs to
`~/Library/Logs/automonetize.stdout.log` / `.stderr.log`. Secrets are not copied into the
plist: `deploy/run_agent.sh` sources `.env` at every start, then `exec`s
`automonetize supervise`, so launchd's SIGTERM reaches the supervisor directly.
`ExitTimeOut` gives it 90 s to finish up.

### Supervisor and resilience (`agent/supervisor.py`)

* The **engine** and **webhook** workers run as threads sharing one lock-guarded SQLite
  connection. A crashed worker restarts with exponential backoff. More than 5 crashes in 10
  minutes shuts everything down if it's the engine, or disables just the webhook (polling
  then covers orders).
* **SIGTERM / SIGINT / SIGHUP** → stop taking work, let the current cycle and in-flight
  fulfilment finish (≤ `shutdown_timeout_seconds`), `PRAGMA wal_checkpoint(TRUNCATE)`,
  write an operational checkpoint (`last_shutdown`: reason, iteration, per-worker
  starts/crashes), release `data/agent.lock`.
* **Network drops and sleep**: each cycle first probes `network_check_hosts` (or the
  `HTTPS_PROXY`). If nothing answers, the cycle is skipped as *offline*, with no retries burned
  and no failures counted toward the emergency stop, and the loop resumes by itself.
* **Reboots**: launchd/systemd restart the supervisor; state, backlog, experiments and
  undelivered orders all live in SQLite.

Your recurring job: `automonetize outreach list --full` → approve/reject every day or two,
and `automonetize orders list --status needs_manual_delivery` if you use Lemon Squeezy with
a shared variant.

Safety rails stay on in production: a process lock (`data/agent.lock`) stops two loops
sharing a database; the circuit breaker caps tasks and API calls per cycle; 5 consecutive
operational failures trigger an emergency stop that survives restarts (`touch data/EMERGENCY_STOP`
or `automonetize stop` to halt by hand, `automonetize resume` to clear).

## Configuration reference

Resolution order: defaults → `automonetize.toml` → `AUTOMONETIZE_<KEY>` environment variables
(lists comma-separated; dict/list-of-list keys as JSON). Secrets and `DRY_RUN` also accept
their conventional unprefixed names. Unknown keys are rejected.

| Key | Default | Meaning |
|---|---|---|
| `interval_seconds` | `3600` | Time between cycles |
| `signal_window_iterations` / `pivot_after_iterations` | `12` / `24` | Pivot windows (views+sales / revenue) |
| `daily_target_cents` | `1000` | The $10.00/day goal |
| `max_actions_per_cycle` / `max_api_calls_per_cycle` / `max_consecutive_errors` | `16` / `60` / `5` | Circuit breakers |
| `storefront_provider` | `auto` | `auto`, `stripe`, `lemonsqueezy` or `gumroad` |
| `price_tiers` | `[[0,500],[25,900],[75,1500]]` | Starting price by company count, clamped to $5-$19 |
| `price_matrix` / `pricing_min_views` / `pricing_window_hours` | `[500,900,1400,1900]` / `20` / `48` | Price experiments |
| `demand_sales_threshold` / `premium_price_cents` / `max_scrape_depth` | `3` / `1900` / `3` | Demand expansion |
| `stripe_webhook_secret` / `webhook_host` / `webhook_port` / `webhook_path` | env / `127.0.0.1` / `8443` / `/webhook` | Webhook listener |
| `network_check_hosts` | Stripe + GitHub API | Offline probe (`[]` disables) |
| `github_pages_branch` / `github_pages_dir` / `site_title` | `""` / `docs` / `Tech Stack Intel` | Site publishing |
| `syndication_publish` / `syndication_interval_days` / `syndication_min_companies` | `true` / `7` / `10` | Syndication |
| `allow_manual_fulfillment` | `false` | Sell even when the agent can't email the file |
| `intel_max_url_checks` / `high_urgency_threshold` | `15` / `60` | Careers URL checks per cycle; "hot" cutoff |
| `github_showcase_repo` / `github_showcase_mode` / `github_pages_repo` / `pages_base_url` | empty / `repo` | Publishing targets |
| `dry_run` | `true` | Master switch for all email |
| `warmup_start_per_day` / `warmup_step_per_week` / `dispatch_max_per_day` | `5` / `5` / `30` | Cold email warm-up |
| `blocked_recipient_tlds` | EU/EEA/UK/CH | Recipients never emailed |
| `lead_sources`, `niches`, `min_leads_for_asset` | see `automonetize init` | Data collection |
| `sender_*`, `outreach_*` | see `automonetize init` | Draft personalization and guardrails |

## CLI

```
automonetize supervise [--no-webhook] [--headless]   # daemon: engine + webhook listener
automonetize webhook [--port P] [--selftest]         # listener only
automonetize run [--once|--cycles N] [--interval S] [--headless]
automonetize site | syndicate [--drafts] | pricing status|run
automonetize dashboard [--watch] | status [--json] | hypotheses
automonetize outreach list [--full] | approve ID… | reject ID… | export | mark-sent ID…
automonetize dispatch [--check]           # send approved outreach now
automonetize orders list [--status S] | deliver ORDER ASSET [--email E]
automonetize revenue sync | report [--ledger] | add AMOUNT [--verified]
automonetize suppress add EMAIL… | list
automonetize assets list | link ASSET PRODUCT
automonetize serve [--port 8000]          # local lander hosting
automonetize stop [--reason R] | resume
```

## Verifying revenue tracking

```bash
automonetize revenue sync                  # Stripe + Lemon Squeezy + Gumroad, idempotent per order id
automonetize revenue report --days 7 --ledger
automonetize orders list
```

Net = gross − fees (estimated per provider) − tax (Lemon Squeezy reports tax separately).
Refunds are skipped. To test the whole path, use a Stripe test key, buy from the test
Payment Link with card `4242 4242 4242 4242`, and within one cycle you should see the
order, the verified revenue and a delivery entry in `data/dispatched_audit.log` (or a real
email once `dry_run = false`).

## Data sources & compliance

* Official public JSON APIs only (Remote OK, Arbeitnow, Algolia HN). The agent doesn't
  scrape HTML or log in anywhere; robots.txt is honoured for everything else (careers-page checks).
* Records keep their source and URL, and bundles include `ATTRIBUTION.md`. **Read each
  source's terms before selling derived data.** Remote OK, for instance, requires
  attribution and a link back.
* Public previews are sanitized: no contact emails, no descriptions.
* Tokens are redacted from logged URLs. Secrets live in the environment.

## Testing

```bash
pytest     # 222 tests, ~10 s, no network
```

Phase 3 adds: webhook signature verification with both the Stripe SDK and the built-in
verifier (tampered, wrong-secret, stale/replayed and malformed signatures), event-id dedupe,
webhook/polling idempotency, async payments via `payment_intent.succeeded`, expired sessions,
metadata attribution, 500-and-retry on processing errors, concurrent fulfilment sending
exactly one email with receipt, the aiohttp app and a real-socket server; JSON-LD validity
and `<script>` breakout resistance, SEO tags, sitemap and RSS 2.0 XML compliance, Pages
branch publishing, article and platform payloads, syndication cadence and dedupe; pricing
decision rules, convergence against three simulated demand curves, repricing through fake
Stripe (new link, idempotency key, old link deactivated, lander updated, old link still
attributable), bundles, demand expansion, premium add-ons, drop-off, Arbeitnow paging;
supervisor lifecycle, SIGTERM/SIGINT shutdown with handler restore, WAL checkpoint, lock
release, crash restart with backoff, crash-loop handling, the supervised webhook serving
requests, offline cycles, and launchd plist validity.

Coverage includes the engine loop and circuit breakers; tool sandbox boundaries; Stripe
(form encoding, idempotency, link reuse and deactivation, session pagination); Lemon Squeezy
(JSON:API payloads, price band, variant and file checks, order parsing, attribution);
tech-stack parsing, intent triggers, urgency scoring and packaging; showcase sanitization,
repo and gist publishing; dispatcher dry-run, audit log, CAN-SPAM gating, headers, warm-up
ramp, suppression, TLD blocking, failure handling, SendGrid/Postmark payloads; IMAP
unsubscribe handling; order delivery; funnel scoring, adjacency pivots and DB migrations;
and an end-to-end closed-loop test (publish → sale → verified revenue → emailed file →
target met).

## Extending

Subclass `strategies.base.Strategy`, list its task names in `tasks`, add them to `PLAN` in
`agent/engine.py`, and pass the strategy to `Engine(strategies=[...])` (tasks without a
handler are skipped). A new storefront implements `configured()`, `publish()` and
`fetch_orders()` from `tools/storefront/__init__.py` and registers in `build_storefronts`.
