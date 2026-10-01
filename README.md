# AutoMonetize

An autonomous, goal-directed agent loop that works toward **$10.00/day of verified net
revenue** with zero startup capital. Every cycle it:

1. collects hiring data from public job-board APIs;
2. turns it into **company-level tech-stack intelligence** (stack fingerprints, urgency scores,
   verified careers URLs, and **commercial buying intent**: migrations, compliance deadlines,
   founding/first hires, each tagged like `Urgency: High (Cloud Migration)`) packaged as a paid dataset;
3. **publishes** it: a live checkout (Stripe Payment Link or Lemon Squeezy), an SEO lander
   with `schema.org/Product` markup on GitHub Pages, a sanitized free preview, and a cluster of
   **search-intent pages** ("companies hiring Kubernetes engineers", "PostgreSQL migrations");
4. **captures leads**: visitors who aren't ready to buy get a free 10-record sample by email,
   then (once they confirm) a Monday "Weekly Tech Pulse" with 3 fresh buying signals and
   1-click upgrade buttons;
5. **sells API access**: a metered REST API over the same signals ($29/month), with keys issued,
   degraded and revoked automatically from Stripe events;
6. **tunes itself**: discovers and trials new public job feeds, A/B-tests its landing-page copy
   with a multi-armed bandit, and works up to 3 niches at once, giving capacity to whichever earns;
7. **distributes** it inbound, with no approval needed: value-first "State of…" breakdowns
   syndicated to Dev.to, Hashnode, GitHub Discussions and RSS (at most one post per platform
   every 5 days), plus a free monthly Hacker News "Who is hiring?" stack gist. Every link carries
   UTM tags. (Cold outreach still exists, but only sends drafts a human approved.)
8. **sells in real time**: a signature-verified Stripe webhook records verified revenue and
   emails the buyer their zip plus a receipt seconds after payment; polling reconciles anything missed;
9. **sells recurring**: a $10/month subscription next to the one-off $9/$14/$19 price; Monday
   morning delta packages go to every active subscriber, and each paid invoice is verified revenue;
10. **optimizes price**: experiments across the one-off tiers per dataset, steps down or bundles
   2-for-1 when traffic doesn't convert, steps up and converges when it does, and on strong
   demand scrapes deeper and ships a $19 premium deep-dive add-on;
11. **measures** revenue by niche, tier and acquisition channel, checkout conversion per channel,
   MRR, churn and net revenue per day against the $10 goal (`automonetize analytics`);
12. **scores** each hypothesis on its funnel and pivots to an adjacent, higher-demand stack
   cluster when it isn't converting;
13. **keeps customers**: failed payments trigger dunning (billing-portal reminders, a 7-day grace
   period at 50 API requests/day), churn is logged by reason, and buyers can recover lost
   downloads and keys themselves at `POST /v1/orders/recover`;
14. **sells high-ticket**: a $49 Executive Migration Dossier (PDF) on any company with urgency or
   intent above 75, offered from the API and the Weekly Tech Pulse and delivered instantly;
15. **evolves its own heuristics** (opt-in, off by default): repairs parsers when a job feed
   renames fields, learns new technology tags, and tries new landing-page headlines when revenue
   stalls. Each change goes through the full test suite and the simulator in a sandboxed git
   worktree, is merged as a local commit, and is reverted automatically if the next cycle regresses.

It runs as a supervised daemon (engine + webhook listener) under launchd or systemd,
survives crashes, reboots, sleep and network drops, and shuts down cleanly on SIGTERM.
Everything day-to-day (keys, start/pause/kill, revenue, logs) is also available in a local
web control panel: `automonetize gui` (see [Control panel](#control-panel-automonetize-gui)).

> **Reality check.** None of this guarantees income. Once configured, the sales path
> (checkout → payment → delivery → revenue) and inbound publishing run without anyone
> watching. What stays human: one-time account setup, approving any cold email, and exposing
> the webhook endpoint publicly (a tunnel or host, see below). Only revenue confirmed by a
> payment provider counts toward the target.

## The easy path (copy and paste)

For a non-technical owner, these five commands are the whole setup. Each one checks before it
changes anything and says in plain words what to fix if something isn't ready.

```bash
automonetize gui                  # enter Stripe test key, Gmail (App password), postal address
automonetize go-live              # paste the live Stripe key: checks, switches to real payments, prints your link
automonetize connect-marketing    # paste a Dev.to key + GitHub token: public site and articles, automatically
automonetize autostart            # start by itself after a restart (macOS launchd)
automonetize doctor --fix         # anytime: health check in plain words, fixes what's safe to fix
```

After that the agent emails you every sale as it happens, anything that needs you, and a daily
report. The only recurring task is optional: approving the sales emails it drafts
(control panel → **Outreach**).

## Tonight's Launch Checklist

```bash
pip install -e '.[dev]'

# 1. Verify everything locally (about 2 s; sandboxed, no keys, no real email, no real money)
automonetize test-full-loop

# 2. Open the browser settings: paste your Stripe key, SMTP details and postal address, run preflight
automonetize gui

# 3. Launch background operation: engine + webhook listener, restarted on crash
automonetize supervise
```

1. **`automonetize test-full-loop`** rehearses the whole business in a throwaway sandbox. It
   seeds 5 cloud-migration postings, ingests them, extracts signals, builds the Executive Tech
   Radar and the matrix pages (checking JSON-LD validity), then buys every product through a
   simulated Stripe and real signed webhooks: the $14 dataset (zip delivered), the $10/month
   subscription (Monday digest staged), the $29/month API (key issued, then a live `curl`
   query) and the $49 dossier (PDF delivered). It also plays a failed payment, a recovery and a
   self-service order recovery, then checks that the dashboard shows ≥ $10/day. Each step is
   printed in colour and the command exits `0` only if every step passes. It never touches
   `data/`, sends no email and calls no external service.
2. **`automonetize gui`** opens the local control panel on `127.0.0.1` for your keys (see
   [Control panel](#control-panel-automonetize-gui)). Email stays in dry-run until you switch it off.
3. **`automonetize supervise`** runs the engine and webhook listener in the foreground; for
   start-at-login, use `automonetize setup-autonomous` ([Set-and-forget](#set-and-forget-on-macos)).
   One Stripe dashboard step is required: **Settings → Billing → Customer portal → Activate**. The
   dunning emails link to that portal. Until it is activated, they fall back to your lander URL.

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
* **traction faded**: it sold before, but has had no sale in `stale_revenue_days` (14) and
  no active subscribers;
* a task proves the niche can't work (for example, too few listings exist). This pivots at
  once.

The first three rules also wait until the niche is `min_hypothesis_days` (10) old. Cycle
counts alone would give a product only 24 hours at hourly cycles, less than one pricing
window or syndication slot. Cycles aborted because the network dropped don't count.

The next hypothesis is **ranked by live market demand**: every candidate (clusters adjacent
to the niche just dropped, with a small bonus; configured niches; all known clusters; tags
mined from recent postings) is scored by how many distinct roles first seen in the last 7
days match it, as whole words. Only candidates backed by at least `min_leads_for_asset`
roles qualify, so a hiring spike in, say, Elixir becomes a niche as soon as the data shows
it. With no evidence yet (cold start, sources down) it falls back to a fixed order: adjacent
clusters, configured niches, mined tags, then broadened revisits of dropped niches.

After a pivot, niches with paying subscribers keep getting refreshed data before each Monday
delivery, so subscribers never receive a stale "0 changes" update.

## Installation

Requires Python 3.11+.

```bash
git clone <this repo> AutoMonetize && cd AutoMonetize
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
automonetize init          # writes automonetize.toml (commented) and data/agent_state.db
cp .env.example .env       # secrets go here, never in the TOML
pip install -e '.[images]'    # optional: Pillow, for PNG OpenGraph cards (SVG badges work without it)
pytest                     # 865 tests, ~40 s, no network
automonetize gui           # optional: enter keys in the browser instead of editing .env
```

Existing databases from earlier versions are migrated automatically on open.

## Control panel (`automonetize gui`)

A local web interface for everything you'd otherwise do in a terminal: entering keys,
starting and stopping the agent, and watching revenue and logs.

```bash
automonetize gui                 # serves http://127.0.0.1:8080 and opens it in your browser, signed in
automonetize gui --port 8090     # another port (or gui_port in automonetize.toml)
automonetize gui --no-browser    # then sign in with the token in data/.gui_token
```

On macOS, run it from the checkout (`cd AutoMonetize && .venv/bin/automonetize gui`). It's
independent of the agent: it can start, pause and stop the launchd-managed agent, and closing
it (Ctrl-C) leaves the agent running. For a dock-launchable shortcut, save
`cd ~/AutoMonetize && .venv/bin/automonetize gui` as an Automator "Run Shell Script" app.

**First-time setup, visually:**

1. `automonetize gui`, then open **Settings**.
2. **Stripe**: pick *Test* or *Live* and paste the secret key. A live key in Test mode (or the
   reverse) is refused, so you can't go live by accident. Leave the webhook secret empty:
   `setup-autonomous` creates the endpoint and fills it in.
3. **Delivery mailer**: choose SMTP, SendGrid or Postmark and fill in its fields, plus the
   sender name and email.
4. **Syndication**: Dev.to, Hashnode, a GitHub token, the Pages repo and the site URL.
5. **Cloudflare Tunnel**: the hostname you'll run `deploy/tunnel/setup_tunnel.sh` with
   (e.g. `hooks.yourdomain.com`); this also sets `PUBLIC_WEBHOOK_URL`.
6. **Compliance**: your CAN-SPAM postal address and an unsubscribe mailbox.
7. **Save & verify**. Values are validated, `.env` is updated in place (comments, order and
   other keys kept, mode 600), and the preflight runs: Stripe API, a real mailer login,
   database, disk. Results show per check. If the agent is running, a banner offers
   **Restart** to load the new settings.
8. **Control → Start engine**. Once the tunnel exists, finish with
   `automonetize setup-autonomous` for the Stripe endpoint and the end-to-end handshake.

**Control tab**: live badges (supervisor, webhook health, engine state, dry run vs live email),
today's net revenue against the $10 target, MRR and paying subscribers, the active niche,
uptime, free leads (confirmed / pending), **API requests today** (per endpoint, 7-day total),
**API subscribers** (active and past-due keys), and the **copy bandit** table (views, clicks,
signups, purchases, conversion and traffic share per variant, winner starred, retired arms
struck through), plus niche capacity shares and discovered-source counts, refreshed every few
seconds. `automonetize dashboard` shows the same in a "Developer API & Growth Engine" panel.
Actions:

| Button | Effect |
|---|---|
| Start engine | Clears a pause, a kill-switch stop or a quarantine, then starts the supervisor (via launchd when installed, otherwise as a background process logging to `data/supervisor.log`) |
| Pause / sleep | The engine skips cycles and holds no power assertion, so the Mac can sleep. The webhook and lead capture keep running. Also `automonetize pause` / `resume` |
| Restart (apply settings) | Restarts the supervisor so it reloads `.env`; pause and stop flags stay as they are |
| Emergency kill switch | Engages the emergency stop and stops the supervisor. Under launchd the job is booted out, so KeepAlive doesn't restart it. Nothing runs until you press Start |

**Logs tab**: the SQLite action log and the tail of `~/Library/Logs/automonetize/agent.*.log`
(or `data/agent.log`), with API keys, webhook secrets, tokens and passwords redacted before
they reach the browser.

**Security.** The panel holds your secrets, so it's locked down even though it's local:

* **Loopback only.** It binds to `127.0.0.1` and refuses any other host (`--host 0.0.0.0`
  exits with an error). It's never reachable through the tunnel: the tunnel forwards only
  `/webhook`, `/healthz` and `/lead-magnet/*` to the listener on port 8443, and the panel is
  on 8080.
* **Signed in.** A 256-bit token in `data/.gui_token` (mode 600). `automonetize gui` signs your
  browser in with a one-time link valid for 2 minutes. Login attempts are rate-limited.
* **CSRF / DNS rebinding.** `SameSite=Strict` HttpOnly session cookie; every change needs an
  `X-CSRF-Token` header and a same-origin `Origin`; requests whose `Host` isn't
  `127.0.0.1:<port>`/`localhost:<port>` are refused; strict Content-Security-Policy with no
  inline script.
* **Secrets are write-only.** The page learns whether a key is set and its last 4 characters,
  never the value. An empty secret field keeps the stored one; *Clear* removes it. Line breaks
  and control characters are rejected, so a pasted value can't add lines to `.env`.
* The panel writes the names shown next to each field (`STRIPE_SECRET_KEY`, `SMTP_HOST`,
  `SMTP_USER`, `CAN_SPAM_POSTAL_ADDRESS`, ...). Hand-edited `.env` files may also use
  `STRIPE_API_KEY`, `SMTP_USERNAME` and `CAN_SPAM_UNSUBSCRIBE_EMAIL`. If `automonetize.toml`
  or an `AUTOMONETIZE_*` variable overrides a field, the save result says so.

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
  --events checkout.session.completed,checkout.session.async_payment_succeeded,checkout.session.expired,payment_intent.succeeded,invoice.paid,customer.subscription.updated,customer.subscription.deleted
# copy the printed "whsec_..." into .env as STRIPE_WEBHOOK_SECRET, then in another terminal:
automonetize webhook --selftest          # checks the secret round-trips
automonetize supervise                   # engine + listener (or: automonetize webhook for the listener only)
stripe trigger checkout.session.completed
```

### Production

Stripe must reach the endpoint over public HTTPS. The listener binds to localhost on
purpose. On a Mac, `deploy/tunnel/setup_tunnel.sh` sets up a permanent Cloudflare Tunnel and
`automonetize setup-autonomous` registers the endpoint with Stripe (see
[Set-and-forget on macOS](#set-and-forget-on-macos)). Elsewhere, use any tunnel or reverse
proxy, then Dashboard → Developers → Webhooks → *Add endpoint* `https://<your-host>/webhook`
with the events above, and put its signing secret in `STRIPE_WEBHOOK_SECRET`. If the endpoint
is unreachable for a while, Stripe retries for up to 3 days and polling covers the gap.

## Inbound distribution

* **Landers** (`tools/page_builder.py`): one page per dataset, bundle and premium add-on,
  with `<title>`, meta description, canonical, Open Graph and product price tags, a
  `schema.org/Product` + `Offer` JSON-LD block (escaped so it can't break out of its
  `<script>`), urgency KPIs, the 5-record sample and the Payment Link. Plus `index.html`,
  `sitemap.xml`, `robots.txt` and an RSS 2.0 feed at `feeds/radar.xml`. Written to
  `data/site/` and committed to `github_pages_repo`, on `github_pages_branch` (e.g.
  `gh-pages`) under `github_pages_dir` (`docs` by default, `""` for the root). Unchanged
  files make no commit. Rebuild by hand with `automonetize site`.
* **Search-intent matrix pages** (`intel/`): for every technology with at least
  `seo_min_companies` (5) companies behind it, `intel/companies-hiring-<tech>-engineers.html`.
  Where at least `seo_min_migrations` (3) companies are migrating to or from a technology, also
  `intel/<tech>-infrastructure-migrations.html`. Each page has hiring velocity (companies
  posting in the last 7 and 30 days), co-occurring stack, the intent mix (or migration paths
  such as `Oracle → PostgreSQL`), a sanitized 5-company preview ranked by intent,
  `schema.org/Dataset` JSON-LD with an `Offer`, the Payment Link of the niche with the most
  matching companies, the free-sample form, and internal links. An `intel/` hub links them all.
  Thin technologies get no page: templated pages with little behind them hurt a site in search.
* **Indexing**: every lander and matrix page is in `sitemap.xml`, which `robots.txt` points to
  (that's how Google finds sitemaps now; its ping endpoint was retired in 2023). Pages whose
  content changed are also submitted to **IndexNow** (Bing, Yandex, Seznam, Naver) once the
  site is live on Pages, with the key file published alongside. Publishing to Pages keeps a
  manifest of what's live, so only changed files cost API calls. If the per-cycle API budget
  runs out mid-publish, the rest goes out next cycle, and IndexNow waits until everything is up.
* **Meta assets** (`tools/seo_assets.py`): per dataset a `radar-badge.svg` (e.g. "python
  radar | 142 hiring signals", where hiring signals = open roles tracked) plus a site-wide
  badge, a 1200×630 OpenGraph card as `og.svg`, and `og.png` when Pillow is installed (most
  social networks ignore SVG `og:image`). Pages carry `og:image` and `twitter:card` tags; the
  JSON-LD lists both offers (one-off, and the subscription as a `UnitPriceSpecification` with
  `billingDuration`).
* **Social proof, real numbers only**: "Updated 2 hours ago · 14 company profiles added this
  week · 30 verified careers pages · 3 purchases in the last 7 days". The relative time is
  computed in the visitor's browser from the last data refresh, so a static page never shows a
  stale "2 hours ago"; zero values aren't shown at all.
* **Syndication** (`tools/syndication/`): a value-first breakdown of the active niche's radar,
  headline computed from the data (e.g. *"State of Python Migrations Q3 2026: 30 Companies
  Hiring for PostgreSQL & Snowflake"*). Sections: key findings, stack adoption, intent
  signals, the 5-record sanitized preview, top 10 by urgency, method. The footer has the free
  preview, one purchase link and the canonical link to the lander.

| Channel | Setup | Notes |
|---|---|---|
| RSS | none | Always on; Substack, Medium and newsletter tools can import from it |
| Dev.to | `DEVTO_API_KEY` | `POST /api/articles`; sets `canonical_url`; checks your existing titles first so it never double-posts; `syndication_publish = false` makes drafts |
| Hashnode | `HASHNODE_TOKEN` + `hashnode_publication_id` | Hashnode's API is GraphQL only: `publishPost` with `originalArticleURL` |
| GitHub Discussions | `github_discussions_repo` (Discussions enabled) + token with Discussions write | Category from `github_discussions_category` |
| Substack | none | No public posting API: paste-ready file in `data/syndication/substack/` |

At most one post per platform every `syndication_interval_days` (5, and never less, whatever
you configure), each article once, and only when at least `syndication_min_companies` (10)
back it. The cadence is checked against **the platform's own record** of your last post
(Dev.to articles, Hashnode publication, Discussions), so a wiped or fresh database can't
reset it. If that history can't be read, nothing is posted (fail closed). Contact details are never included. `automonetize syndicate [--drafts]` runs it on demand.

### Commercial buying intent (`strategies/tech_stack_intel.py`)

Beyond "who's hiring", each company record says **who's about to spend**:

| Family | Detected from postings like | Example tag |
|---|---|---|
| Migration & modernization | "moving from Snowflake to BigQuery", "legacy Oracle to Postgres", "Kubernetes migration", "migrating off our data center" | `Urgency: High (Database Migration)` with `migration_path` `Snowflake → BigQuery` |
| Compliance & security | SOC 2, HIPAA, FedRAMP, PCI DSS, ISO 27001, HITRUST, CMMC, zero trust, hardening; stronger when they're *pursuing* it | `Urgency: Medium (SOC 2 Compliance)` |
| Leadership & scaling | founding engineer, first DevOps/SRE/data/security hire, head of infrastructure/platform | `Urgency: Medium (First DevOps Hire)` |

A migration only counts when at least one end resolves to a real technology, so "moving from
junior to senior" or "switching to a new team" score 0. **Intent score (0-100)**: the strongest
signal (explicit from → to migrations score highest), plus half of any others, plus breadth,
open roles and freshness (posted in the last 7 days). High is 60+, Medium 35-59, Low 1-34.
Records carry `intent_score`, `intent_tag`, `intent_level`, `intent_category`,
`commercial_signals`, `migration_path` and short `intent_evidence` phrases (paid dataset only;
never on public pages). The tag leads the CSV columns and previews. The Executive Tech Radar
gets a *Commercial buying intent* section (top 15 with paths) and an Intent column.

## Free lead magnet (`strategies/lead_magnet.py`)

For visitors who aren't ready to pay: a form on every lander and matrix page, "Free: 10
records + a hiring-intent cheatsheet".

1. The form posts through the tunnel to `/lead-magnet/capture` on the local listener (the site
   itself is static). It works without JavaScript and carries the visitor's first-touch
   attribution. The address is stored in `subscribers` with `tier = "free"`. Free rows never
   count toward revenue, MRR, subscriber counts or traction.
2. Straight away they get an email with a CSV of the 10 highest-intent companies in that
   niche and a Markdown cheatsheet (how to read the tags, this week's top technologies and
   signals, how to use each signal family).
3. **Double opt-in** (`lead_magnet_double_opt_in = true`): that email has a confirm link. Only
   confirmed addresses get the weekly email. A public form lets anyone enter anyone's address;
   without this, typos and pranks turn into weekly unsolicited mail, which gets a sending
   domain blocklisted. Links in emails open a page with a button, and only the button acts,
   because mail security scanners prefetch every link.
4. **Weekly Tech Pulse**, every Monday from 09:00 (`lead_nurture_weekday`, `lead_nurture_hour`,
   `subscription_timezone`), once per ISO week: 3 buying signals first seen that week (topped up
   with the strongest active ones in a quiet week), then buttons for the $10/month subscription
   and the full dataset. Each button is a Payment Link with the email prefilled and the sale
   attributed to `leadmagnet` in analytics. Paying subscribers and suppressed addresses are
   skipped.
5. Every email has a one-click unsubscribe (`List-Unsubscribe` + `List-Unsubscribe-Post`, RFC
   8058) and your postal address. The weekly email isn't sent at all until
   `CAN_SPAM_POSTAL_ADDRESS` is set. Unsubscribing also adds the address to the suppression list.

Abuse brakes on the public endpoint: a hidden honeypot field, 5 attempts per visitor IP per
hour (`CF-Connecting-IP`, trusted only from cloudflared on loopback), 60 signups an hour
overall, strict address validation, and suppressed addresses silently "succeed" so the form
never reveals who's on the list. Emails go through the same dispatcher as deliveries, so
`DRY_RUN` applies. **If you set up the tunnel before this release, re-run
`deploy/tunnel/setup_tunnel.sh <hostname>`** to add the `/lead-magnet/*` paths to its ingress
rules.

### Setting up Dev.to and Hashnode

1. **Dev.to**: Settings → Extensions → *DEV Community API Keys* → generate a key and put it in
   `.env` as `DEVTO_API_KEY`.
2. **Hashnode**: Settings → Developer → generate a *Personal Access Token* → `HASHNODE_TOKEN`
   in `.env`. The publication id is in your blog dashboard URL
   (`hashnode.com/<publication-id>/dashboard`) → `hashnode_publication_id` in `automonetize.toml`.
3. Start with drafts: `automonetize syndicate --drafts` creates a Dev.to draft (Hashnode is
   skipped in draft mode). Check it on the site, then leave `syndication_publish = true` and
   the engine posts on its own.

### HN "Who is hiring?" tracker

`tools/syndication/hn_algolia_tracker.py` finds the current monthly thread through the Algolia
HN API. It parses each company's stack and intent signals and publishes a free Markdown
breakdown (technology demand, remote share, signals, a company → stack table) as a **public
Gist**, refreshed at most every `hn_gist_refresh_hours` (24) while the thread grows, with one
RSS item per thread. It holds only derived facts: no contact details and no text copied
from comments. Dataset links at the bottom carry `utm_source=github&utm_medium=gist`. Needs
`GITHUB_TOKEN` with gist scope; without it the summary is written to `data/syndication/hn/`.

## Subscriptions (recurring revenue)

`strategies/subscription_engine.py` adds a **$10/month** tier (`subscription_price_cents`,
`subscription_interval = "month"` or `"week"`) next to each niche's one-off dataset:

1. Once the one-off dataset is live on Stripe, it creates a recurring Price
   (`recurring[interval]=month`) and Payment Link, with `subscription_data.metadata` carrying the
   niche. The lander offers both buttons.
2. A completed subscription checkout (webhook or polling) creates the subscriber with
   `subscription_status = "active"` and emails the full current dataset as a welcome.
3. Every `invoice.paid` is one verified revenue event (idempotent by invoice id). The checkout
   session isn't counted as well, because its amount *is* the first invoice.
4. `customer.subscription.updated/deleted` keep the status current (active, past_due,
   canceled...). Only active and trialing subscribers get deliveries.
5. **Every Monday from 08:00** (`subscription_delivery_weekday/hour/timezone`), each active
   subscriber gets that week's package once: `WEEKLY_UPDATE.md` plus `delta.csv/json` (companies
   new, or with new roles, in the last 7 days) plus the full refreshed `tech_radar.csv`. If the
   machine was asleep on Monday it catches up later the same week. In dry run the email goes
   to the audit log once and is sent for real when you go live that week.

The engine won't sell subscriptions until email delivery works (`dry_run = false` plus a
backend), unless `allow_manual_fulfillment = true`. Lemon Squeezy/Gumroad subscriptions aren't
automated.

### Testing subscription fulfilment

```bash
# test-mode keys in .env: STRIPE_SECRET_KEY=sk_test_..., plus the whsec_ from `stripe listen`
stripe listen --forward-to localhost:8443/webhook --events checkout.session.completed,invoice.paid,customer.subscription.updated,customer.subscription.deleted
automonetize supervise                          # or: automonetize run --once, to create the offer
automonetize assets list                        # the "subscription" asset has the Payment Link URL
# open that URL, pay with 4242 4242 4242 4242 (any future date / CVC)
automonetize subscriptions list                 # → active, channel attributed, welcome delivered
automonetize analytics --period=30d             # → MRR $10.00, subscription tier revenue
tail -3 data/dispatched_audit.log               # welcome email (or the real email when dry_run=false)
# weekly delivery: set subscription_delivery_weekday to today and the hour to now, then
automonetize subscriptions deliver              # → "2026-W41: 1 sent ..."; running it again sends nothing
# cancellation: cancel in Dashboard → Customers → subscription → `subscriptions list` shows canceled
```

Renewals: Stripe sends `invoice.paid` each period (Stripe *test clocks* can fast-forward
billing for subscriptions you create in the Dashboard). The polling sweep (`sync_subscriptions`)
also pulls `/v1/invoices?subscription=…&status=paid`, so a missed webhook never loses a renewal.

## Developer API (`api/`, $29/month)

The same company-level signals as the datasets, as a metered REST API. Served by the public
listener under `/v1/` through the Cloudflare Tunnel; documentation at
**`https://<your tunnel host>/docs/api`** (reference plus a "Try it" console), machine-readable
**OpenAPI 3.1** at `https://<your tunnel host>/openapi.json` (import into Postman, Insomnia or a
client generator).

```bash
export AM_KEY=am_live_...      # from the welcome email
export API=https://hooks.yourdomain.com

# Companies running Kubernetes that are mid cloud migration, urgent hires, newest first by intent
curl -H "Authorization: Bearer $AM_KEY" \
  "$API/v1/signals?tech=kubernetes&intent_tag=Cloud%20Migration&min_urgency=60&since=2026-09-01&limit=25"

# Next page: pass pagination.next_cursor back
curl -H "Authorization: Bearer $AM_KEY" "$API/v1/signals?tech=kubernetes&intent_tag=Cloud%20Migration&min_urgency=60&since=2026-09-01&limit=25&cursor=eyJv..."

# One company: technology footprint, migration history, postings still live in the feeds
curl -H "Authorization: Bearer $AM_KEY" "$API/v1/companies/acme.com"

# Plan, status and today's usage
curl -H "Authorization: Bearer $AM_KEY" "$API/v1/me"

# Rotate the key: the response holds the new one; the old one stops working immediately
curl -X POST -H "Authorization: Bearer $AM_KEY" "$API/v1/auth/rotate"
```

| Endpoint | Returns |
|---|---|
| `GET /v1/signals` | Filters `tech`, `intent_tag` (substring of tag/level/signal, e.g. `SOC 2`, `High`), `min_urgency`, `min_intent`, `since`; `limit` ≤ 100; stable order (intent score, urgency, company id); `pagination.next_cursor`. A cursor is bound to its filters and to the dataset version (`cursor_expired` after a data refresh) |
| `GET /v1/companies/{domain}` | Domain or `company_id`. Stack, niches, intent tag and score, migration path, `history` (each change of intent tag/path/stack by date), `active_postings` (title, location, dates, public listing URL; no descriptions or contact details) |
| `GET /v1/me` | Key prefix, status, plan, daily quota, used today |
| `POST /v1/auth/rotate` | New key (shown once), old one revoked |

Every response is deterministic JSON (sorted keys) with `X-RateLimit-Limit`,
`X-RateLimit-Remaining`, `X-RateLimit-Reset` and `X-Request-Id`. Errors are always
`{"error": {"code", "message", "status", "param", "doc_url", "request_id"}}`; switch on `code`:
`missing_api_key`, `invalid_api_key`, `key_revoked`, `insufficient_permission`, `rate_limited`,
`quota_exceeded`, `too_many_auth_failures`, `invalid_parameter`, `invalid_cursor`,
`cursor_expired`, `not_found`, `method_not_allowed`, `internal_error`. Every 429 has
`Retry-After`.

**Keys and limits.** Keys are `am_live_` + 48 hex characters. Only a SHA-256 is stored, so the
database can't be used to call the API. Send them as `Authorization: Bearer` or `X-API-Key`,
never in a URL (a key in a query string is ignored and counts as a failed attempt). Each key
has a token bucket (`api_burst` 10, refilled at `api_rate_per_second` 2) and a daily quota
(`api_daily_quota` 500, reset 00:00 UTC, persisted so restarts don't reset it). A client with
30 failed authentications in an hour is blocked for the rest of the hour.

**Stripe lifecycle** (webhooks, and the polling sweep as a safety net):

| Event | Key |
|---|---|
| `checkout.session.completed` for the API product | Issued (once per subscriber) and emailed with curl quickstart and doc links |
| `invoice.payment_failed`, or status `past_due` | Degraded at once to `api_degraded_quota` (50/day) while Stripe retries the card; responses carry `X-API-Key-Status: past_due` |
| Still unpaid after `dunning_grace_days` (7) | Suspended: `402 payment_required` (kept, not deleted) |
| `invoice.paid` / status back to active | Restored to full quota |
| `customer.subscription.deleted`, `unpaid`, `incomplete_expired` | Revoked: `401 key_revoked` from the next request |

The product itself ("Developer API: Hiring & Buying-Intent Signals", $29/month recurring Payment
Link with `tier=api` metadata) is created by the `publish_api_tier` task once Stripe, the tunnel
and email delivery all work. API subscribers count toward MRR and revenue like any subscriber;
they don't get dataset emails. With `DRY_RUN` on, a key issued at checkout can't be emailed:
the agent raises an alert, and `automonetize api reissue <subscriber_id>` sends a fresh key once
you're live. `automonetize api keys | usage | revoke <id>` covers the rest.

**If you set up the tunnel before this release**, re-run `deploy/tunnel/setup_tunnel.sh
<hostname>`: the ingress rules now also forward `/v1/*`, `/openapi.json`, `/docs/api` and the
copy-telemetry beacon `/t/e` (still nothing else).

## Customer lifecycle (`strategies/retention_engine.py`)

**Dunning.** A failed payment (`invoice.payment_failed`, or the subscription reported as
`past_due`) opens one *dunning case* per subscriber:

| When | What happens |
|---|---|
| Day 0 | API keys drop to 50 requests/day. A polite "your payment didn't go through" email goes out with a one-time **Stripe billing portal** link (`billing_portal.Session`) where the customer can update their card |
| Days 3 and 6 (`dunning_reminder_days`) | Follow-up reminders from the `run_dunning` task |
| Day 7 (`dunning_grace_days`) | Keys are suspended (`402 payment_required`). Nothing is deleted, and a later `past_due` status sync can't lift the suspension |
| Payment succeeds, at any point | The case closes as *recovered* and full access returns |
| `customer.subscription.deleted` | Subscriber marked `canceled`, keys revoked, case closed; a **churn event** is logged with product, reason (Stripe's `cancellation_details.reason`, or `payment_failed` after dunning), tenure and MRR lost |

Stripe has no `customer.subscription.past_due` event. It reports past-due as
`customer.subscription.updated` with `status: past_due`, and that is handled here. The webhook
endpoint registration lists only real event names, because Stripe rejects unknown ones.

**Self-service order recovery.** `POST /v1/orders/recover` takes `{"email": "..."}` (JSON or a
form field) with no key. Everything bought with that address is re-sent **to that address
only**: dataset zips, dossier PDFs, and the current dataset for dataset subscribers.
Anti-abuse measures:

* The response is always the same `202`, and the work happens after responding, so neither the
  body nor the timing reveals who is a customer.
* Limits are 3 requests per IP per hour (`429` with `Retry-After`) and 3 recovery emails per
  address per day.
* Bad input returns `400 invalid_parameter`.

API keys are stored only as hashes, so they can't be re-sent. Rotating a key because someone
typed an email into a form would let anyone break a customer's integration. Instead, API
subscribers get a one-time confirm link (24 h). Opening it (GET) only shows a button, so mail
scanners that prefetch links do nothing. Clicking it (POST) emails a new key and retires the
old one.

## Executive Migration Dossier ($49, `tools/dossier_builder.py`)

A one-company briefing for anyone selling into a migration. Only companies with urgency or
intent above `dossier_min_score` (75) qualify. It covers:

* **Why now**: the intent tag, path, open roles and leadership hires in one line.
* **Technology footprint** by category, and **legacy systems** in play.
* **Verified migration path** with detected signals, evidence phrases and signal history.
* **Hiring activity**: open roles with links, a per-department table with new roles in the last
  7 and 30 days, and leadership roles being hired. It lists roles only, never people or
  personal data.
* **Recommended pitch angles**, derived from the signals (migration delivery, compliance
  deadline, new technical leadership, platform engineering, capacity).

The PDF comes from a small standard-library writer (`tools/pdf_writer.py`): PDF 1.4, the
built-in Helvetica fonts, compressed streams and page numbers, with no external binaries. A
Markdown copy is attached as well.

**Selling it.** `publish_dossier_tier` creates one Stripe Product and a one-off $49 Price once
Stripe, the tunnel and email work. The flow:

1. `GET /v1/dossiers/{company_id}/buy` creates a Checkout Session for that company and
   redirects (303) to Stripe.
2. The `checkout.session.completed` webhook records the order.
3. The dossier is built from the latest data and emailed within seconds; the files are kept in
   `data/dossiers/` so order recovery can re-send them.

Ineligible companies get a 404, so a thin dossier is never sold.

**Upsells.** `GET /v1/companies/{domain}` returns `dossier_available`, `dossier_url` and
`dossier_price_cents`. In the Monday Tech Pulse, every featured company with intent above 80
(`dossier_pulse_min_intent`) gets a 1-click "Get the executive dossier" button, with the
reader's email pre-filled at checkout.

## Self-evolving growth engine

### Source discovery (`agent/source_discovery.py`)

The lead aggregator no longer depends only on the three built-in job APIs. Once a day, the
`discover_sources` task collects candidate feeds:

* **Company ATS boards found in the agent's own data.** Every careers URL it verifies and every
  apply link it collects is checked for Greenhouse, Lever and Ashby. All three publish a public
  JSON API of the company's open roles. A company that already showed buying intent is exactly
  the one worth watching.
* **RSS/Atom autodiscovery** (`<link rel="alternate">`) on job-site pages the leads point to.
* **A seed catalogue** of public job feeds, plus your own `source_seed_feeds`.

(GitHub trending isn't used: it lists repositories, not jobs.) Every candidate goes through a
sandbox before it can feed the pipeline:

* **SSRF guard.** Public HTTPS hosts only. IP literals, internal names, and hosts resolving to
  private, loopback, link-local or metadata addresses are refused, because these URLs come from
  scraped data.
* **Politeness and safety.** robots.txt must allow the URL. One polite GET per source per day
  while on trial; a 429 or `Retry-After` counts as a failure. Responses ≤ 2 MB. JSON shape or
  RSS/Atom structure must match (DOCTYPE/ENTITY declarations refused).
* **Measured yield.** The share of items that become valid leads, how many carry a recognisable
  stack, and how many carry a buying-intent signal.

A source becomes **active** after healthy probes on 3 separate days (error rate ≤ 20%, ≥ 60%
valid items, at least one buying-intent signal), up to 20 active. From then on the aggregator
ingests it every cycle, read from SQLite, so nothing restarts. Robots disallow or 3 failures
in a row reject a source. An active source failing most of its recent runs is suspended and
re-trialled a week later. **Terms:** robots.txt says what may be fetched, not what may be
resold. ATS board APIs exist for republishing a company's own openings; review the terms of
any aggregator feed before relying on it (`source_seed_feeds`, or disable discovery).

### Landing-page copy bandit (`tools/copy_bandit.py`)

Two slots are tested on every dataset lander, each as its own bandit:

| Slot | Variants (control first) |
|---|---|
| Headline | `hiring_stack` "Python hiring & stack intelligence: 42 companies, updated daily" · `migration_urgency` "7 Python companies are migrating or hiring urgently right now" · `verified_leads` "12 verified Python developer leads with live careers pages" |
| Call to action | `instant_feed` "Get instant access: $9.00" · `free_sample` "Download the free sample" · `developer_api` "Query the Developer API: $29.00/month" |

* **Honest copy.** Numbers are computed from the page's data, and a variant whose claim isn't true
  for that page (no verified careers pages, no API tier yet) isn't offered there.
* **Reward.** Conversions per view: free-sample signups plus purchases. Clicks are recorded and
  shown but not rewarded, because the CTAs lead to different places and optimising clicks would
  just learn that free things get clicked.
* **Allocation.** Epsilon-greedy by default: the arm with the best posterior mean gets 80% of
  traffic and the rest share 20%. Or `copy_bandit_algorithm = "thompson"` (traffic in proportion
  to the probability of being best). The prior is empirical Bayes: the slot's pooled rate worth 50
  views, so an untested arm isn't mistaken for a winner.
* **Retirement.** After 200 views, an arm 2 standard errors below the **control** (two-proportion
  z-test) is retired. The control, the winner and the last arm are never retired, so exploration
  never stops.
* **Serving.** The site is static, so each visitor's arms are picked in their browser from the
  allocation baked into the page, and stay the same until the policy changes. Views and clicks
  are beaconed to `/t/e` through the tunnel (validated, one per visitor, page, event and day,
  per-IP limited). Signups carry the variant in a hidden form field. Purchases carry it in
  Stripe's `client_reference_id` (`am--devto--w40--hm_cs`), alongside the channel attribution.
  Crawlers and no-JS visitors see the current winner. The `<h1>` (product title) never varies,
  so search rankings aren't affected.
* The `tune_copy` task recomputes the policy each cycle. Pages are only rebuilt when an allocation
  moves ≥ 5 points or an arm is retired.

### Satellite niches (`strategies/satellite_orchestrator.py`)

Up to `max_active_niches` (3) niches at once: the engine's primary hypothesis runs as before,
and up to 2 satellites run a lighter pipeline (collect, radar, package, checkout). Each niche's
**capacity share** is proportional to its verified net revenue over the last 14 days, with a 15%
floor so a new niche gets a chance, and equal shares while nothing has sold. Shares drive how
often each satellite is refreshed, which niche the next syndicated article covers (the one
furthest below its share), and how the SEO page budget (`seo_max_pages`) is split. A satellite
with no revenue and no subscribers after 21 days is retired and replaced by the next untried,
demand-ranked niche. Satellites share the per-cycle API budget and stop for the cycle when it's
spent.

## Acquisition analytics

Every outbound link is tagged: lander and showcase links get `utm_source` (devto, hashnode,
github, rss, substack…), `utm_medium` and `utm_campaign`, and links straight to checkout get
Stripe's `client_reference_id` (`am--<source>--<campaign>`). On the lander, a small script
carries the visitor's UTM values into the checkout links, and remembers the first touch for 30
days (`localStorage`), so a Dev.to reader who lands, browses to another dataset and buys there
days later is still attributed to Dev.to. Stripe copies `client_reference_id`
onto every Checkout Session, completed or abandoned, and webhooks/polling decode it onto the
order.

```bash
automonetize analytics --period=30d      # 7d, 30d, 12w, 3m, 1y, all; add --json for machine output
```

* Revenue, payments and gross by **acquisition channel**, **niche** and **pricing tier**
  (one-off tiers and subscription invoices listed separately).
* **Checkout conversion per channel** = completed ÷ started Checkout Sessions. A static site
  can't see page visits, so this is measured from the checkout step on.
* **MRR** (active and trialing subscribers, weekly plans × 52/12), new and canceled
  subscribers, **churn** = cancelled in period ÷ active at its start.
* **Net revenue per day** vs the $10 goal, today's net, days the goal was met, and how much of
  the goal MRR alone covers.

The dashboard shows a Recurring row (MRR, active, past due, canceled).

## Pricing engine

`agent/pricing_engine.py` runs one price experiment per live dataset, each with its own
Stripe Price and Payment Link, so every order maps to the price that produced it. Links from
earlier experiments stay attributable. It tracks showcase views gained since the experiment
started, checkout initiations (all Checkout Sessions: open, complete or expired), completed
orders and cart drop-off.

| Condition | Action |
|---|---|
| ≥ 3 abandoned checkouts, 0 orders, ≥ 48h | new Price one tier down (people reached Stripe and left: the clearest price signal) |
| > 20 views, 0 orders, experiment ≥ 48h old | new Price one tier down ($19 → $14 → $9 → $5) |
| no view tracking, 0 orders after 2 × 48h | step down anyway: an unsold price is never held forever |
| same at $5 | 2-for-1 bundle with another niche's dataset (prefers adjacent clusters), at the higher price |
| ≥ 2 orders at the current price | test one tier up, unless it already earned less per view |
| 1 order in > 20 views after 48h | explore the cheaper tier once, then settle on the best |
| neighbouring tiers tested and worse | **converge**: hold the best revenue-per-view tier |
| converged, but no sale in the last 96h | re-open: demand shifted, step down and re-explore |
| > 2 sales in 24h for the niche | scraping depth +1 (more Arbeitnow pages, more HN comments, broader keywords) and a $19 premium deep-dive (per-company profiles), at most weekly |

Experiments belong to the *product*, not the data version: a routine refresh (new leads → new
asset version) keeps the running experiment, its clock, its tested-tier history and its live
price. Views come from a running total of GitHub's rolling 14-day counts (a lower bound), so
later experiments still see new traffic after the first peak.

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

## Autopilot (Phases 12-15)

**Going live and connecting marketing** (`cli/go_live.py`, `cli/connect_marketing.py`):

* `go-live` asks for the live Stripe key, then checks it with Stripe: it must be a live key, and
  the account must accept charges. It also checks the mailbox login, trying the other standard
  port and Google's app password without spaces if the first attempt fails, and saves whichever
  works. Only then does it write the live key, `STRIPE_MODE=live` and `DRY_RUN=false`. It marks
  test-mode listings for re-creation, restarts the agent, waits for the live checkout and prints
  the link. `go-live --link` lists everything on sale.
* `connect-marketing` checks a Dev.to key and a GitHub token (classic, `repo` + `gist`). It
  creates `<you>.github.io` if needed, commits a first page, switches Pages on for `/docs` (an
  existing Pages site keeps its branch and folder), saves everything, restarts the agent and waits
  for the site to answer.

If a check fails, neither command changes anything.

**Phase 12: uptime and self-repair** (`cli/doctor.py`).
* `automonetize doctor` checks, in plain words:
  * whether the agent is running and starts after a reboot;
  * the engine state and when the last cycle ran;
  * live payments, and what's on sale with its links;
  * email settings, marketing, and whether the Mac stays awake (`pmset`);
  * code updates, and problems in the last 24 hours.
* `--fix` applies the safe fixes: starts the agent, installs autostart, and pulls an update
  (fast-forward only, never over local changes) and restarts.
* It never undoes a pause or kill switch you set.
* `automonetize autostart` hands the running process over to a launchd job
  (`deploy/install_launchd.sh --service agent`), so the agent starts at login and is restarted if
  it stops. It never runs two supervisors at once.

**Phase 13: owner reports** (`strategies/owner_reports.py`, task `report_owner`). These go to
`owner_email` (`OWNER_EMAIL`; defaults to the sender address):
* an email for each new sale or subscriber;
* an email when the agent flags something for a human;
* a daily report after `owner_digest_hour` (8, in `subscription_timezone`). It covers yesterday's
  and the week's revenue against the goal, MRR, live product links, free leads, drafts waiting for
  approval, and the last 24 hours' problems.

The first run starts from "now" (no history flood). Pointers advance only after a successful send,
so a failed email is retried. `owner_reports = false` turns them off.

**Phase 14: everything that's built gets sold** (`tools/catalog.py`, `SatelliteOrchestrator.catch_up`).
* Every cycle, any satellite dataset that's packaged but not on sale (for example, built while
  email was still in dry run) is published at once, instead of on its next refresh.
* `live_products()` is the single list of what's buyable, one row per live link, without
  test-mode links or per-company dossiers. `go-live`, `doctor` and the daily report use it.

**Phase 15: outreach approval in the panel** (`gui/routes/outreach.py`, **Outreach** tab).
* The drafted sales emails are listed best-scored first, with recipient, subject and full text.
  You can approve or reject one, a selection, or all.
* The panel says when approved emails can't be sent yet (missing CAN-SPAM details, dry run).
* Only drafts still pending can change: a decision is never flipped.
* Approved emails go out with the next cycles, within the warm-up limit.

## Business operations (Phases 16-19)

**Phase 16: money you can see** (`strategies/finance.py`, task `sync_finance`). Each cycle the
agent reads your Stripe balance and recent payouts (read-only):
* what's ready to pay out;
* what's still settling;
* the last payout (amount, status, arrival date);
* the total paid out so far.

New Stripe accounts usually get the first payout 7-14 days after the first sale. The figures appear
in the daily report, the control panel (**Stripe balance** card) and `automonetize doctor`.

**Phase 17: customer support autopilot** (`strategies/support_desk.py`, task `answer_support`).
* The agent reads your mailbox over IMAP. For Gmail, Fastmail, Outlook and iCloud this uses the same
  login and app password as sending.
* It opens the mailbox **read-only** and only looks at unread mail from addresses that bought
  something or subscribe. Everything else is never downloaded or touched.
* "I didn't get the file / resend / can't download" → everything that customer bought is re-sent
  at once, through the order-recovery path with its per-address daily cap.
* Refunds, cancellations, disputes and any other question from a customer → never answered
  automatically. You get an alert email with the customer, subject and first lines.
* Each message is handled once (remembered by Message-ID).

**Phase 18: the all-datasets bundle** (`strategies/bundle_engine.py`, task `publish_bundle`).
* Once two or more niches (`bundle_min_niches`) are on sale, one product bundles every niche's
  newest dataset at `bundle_discount` (40%) off the sum, rounded to whole dollars.
* It's an ordinary product (a zip of the datasets plus a Payment Link). Payment matching, delivery,
  order recovery, revenue and sale emails all work unchanged.
* A new dataset version rebuilds the zip in place, keeping the same link.
* A new niche or price makes a new link and deactivates the old one in Stripe.

**Phase 19: backups** (`agent/backup.py`, task `backup_data`).
* **What:** the agent database, the evolution log, `.env` (mode 600) and `automonetize.toml`.
* **When:** daily, kept 14 days (`backup_keep_days`); the newest is always kept.
* **Where:** outside the project folder (`~/Library/Application Support/AutoMonetize/backups` on
  macOS), so re-cloning or deleting the folder doesn't lose them.
* **How:** copies use SQLite's online backup API, so they're consistent while the agent runs.
* **Commands:**
  * `automonetize backup` backs up now; `automonetize backup list` shows them.
  * `automonetize restore NAME` asks you to type RESTORE, stops the agent and saves the current state
    as a "pre-restore" backup. It then restores and starts the agent again.

The plan has 58 tasks now (Phases 20-94 added twenty-eight; most later phases extend existing tasks). An old `automonetize.toml` that pins
`max_actions_per_cycle` lower is raised to the plan size + 10 automatically, so no cycle is ever
cut short.

## Growth & bookkeeping (Phases 20-23)

**Phase 20: weekly share kit** (`strategies/share_kit.py`, task `refresh_share_kit`).
* The agent never posts as you (that needs your accounts). Instead it writes ready-to-paste posts
  for each product on sale: LinkedIn, X (under 280 characters), Reddit and a direct message.
* Posts use only real numbers from the current dataset (companies hiring, open roles, urgent
  hirers, top signal). A number it doesn't have is left out, never guessed.
* Every link is tracked (`utm_source` on your product page, or `client_reference_id` on the
  checkout link), so the analytics show which channel sold.
* Rebuilt when products or prices change, and weekly. Find it in the Monday daily report, the
  control panel's **Share** tab (one-click **Copy**) and `automonetize share`.

**Phase 21: refunds and disputes** (`strategies/refunds.py`, task `sync_refunds`).
* Reads refunds and disputes from Stripe (read-only) every cycle.
* A refund becomes a negative revenue line, so today's total, the $10/day goal and the reports
  are net of it. The order is marked `refunded` and no longer counts as a sale for pricing.
* A dispute (chargeback) records the amount plus Stripe's $15 dispute fee, marks the order
  `disputed`, and sends you an alert with the reason and the evidence deadline. You answer it in
  the Stripe dashboard. If you win, the amount comes back as a positive line.
* Each refund or dispute is counted exactly once. Nothing is refunded or contested automatically.

**Phase 22: buyer follow-up** (`strategies/buyer_followup.py`, task `follow_up_buyers`).
* Three days after delivery (`buyer_followup_days`), each buyer gets one short email: did it
  arrive, and what would they want in the next version? Replies come to your inbox, where the
  support desk re-sends files and passes everything else to you.
* One email per order, one-off purchases only (never subscriptions, refunds or disputes), never
  to a suppressed address. It carries your postal address and a reply-"unsubscribe" opt-out, and
  live sending waits until `CAN_SPAM_POSTAL_ADDRESS` is set.
* The support desk now honours "unsubscribe" replies by adding the sender to the suppression list.

**Phase 23: monthly bookkeeping** (`strategies/bookkeeping.py`, task `monthly_books`).
* On the 1st of each month (in `subscription_timezone`) the agent writes last month's verified
  revenue (sales, subscription payments, refunds, disputes and fees) to
  `data/exports/books/YYYY-MM.csv` with a totals line, and emails it to you as an attachment.
* `automonetize offers                                  # each offer's discount, emails sent and sales
automonetize privacy export|forget EMAIL             # a customer's data: copy it, or erase it
automonetize phone                                   # phone notifications for sales and alerts (ntfy)
automonetize quiet on|off                            # hold or release all marketing email
automonetize commands [--new]                        # the code for email commands (AM STATUS <code>)
automonetize customers [--report]                   # customer CSV; repeat rate and lifetime value
automonetize uninstall [--deactivate-links] [--delete-data]   # stop and remove the background service
automonetize setup                                   # guided setup: only what's missing
automonetize connections                             # check every outside service (read-only)
automonetize version                                 # which version is running
automonetize expense add|list|delete                 # business expenses for the books
automonetize goal [DOLLARS]                          # show or change the daily goal
automonetize features                               # every feature: on, off, or waiting for you
automonetize settings-log                           # what changed in the settings, and when
automonetize export-all                              # everything as CSVs in one zip
automonetize todo                                    # the few things only you can do, most valuable first
automonetize heartbeat [URL]                         # get an email if the agent stops (healthchecks.io)
automonetize pace                                    # 7-day pace vs the daily goal, and the next step
automonetize books [YYYY-MM]` builds any month by hand.

## Store care (Phases 24-27)

**Phase 24: storefront health** (`strategies/storefront_health.py`, task `check_storefront`).
* Every `storefront_check_hours` (6), for each product on sale, the agent checks three things:
  * the Stripe Payment Link is still active;
  * the public product page loads;
  * the download file exists and is a readable zip.
* A newly broken product sends you one alert. Recoveries are logged.
* Results appear in `automonetize doctor` ("Checkout & downloads") and in the daily report.
* Nothing is changed automatically.

**Phase 25: new-release emails** (`strategies/release_announcer.py`, task `announce_releases`).
* When a dataset for a **new niche** goes on sale, past buyers who don't own it get one short
  email with a tracked link.
* Who gets it:
  * only people who bought before (not refunded or disputed), never anyone else;
  * never suppressed addresses;
  * at most one such email per buyer every `announce_min_gap_days` (14);
  * only in the first 14 days after a release.
* New versions of a niche don't count as new. Stale datasets are never announced.
* The first run only records what's already on sale, so switching it on emails nobody about old
  products.
* Every email carries your postal address, a reply-"unsubscribe" opt-out and `List-Unsubscribe`.
  Live sending waits for `CAN_SPAM_POSTAL_ADDRESS`.

**Phase 26: goal pacing** (`strategies/goal_pacing.py`, task `pace_goal`).
* Once a day it works out:
  * the 7-day net per day against your goal, and the projected month;
  * the best channel and niche;
  * how many people reached checkout and how many paid.
* It then names **one next step**, most basic first: go live, fix a broken product, get traffic
  (connect marketing, post the share kit), fix the page or price, or do more of what works.
* Shown in the daily report, the control panel (**Goal pace** card), `automonetize doctor` and
  `automonetize pace`.

**Phase 27: stale-data guard** (`strategies/freshness_guard.py`, task `guard_freshness`).
* A dataset whose niche has had no new or re-seen job posting for `stale_after_days` (7) is marked
  stale and sends you one alert.
* Stale datasets are left out of the share kit and the release emails. They stay on sale with
  their "data updated" date; taking them down is your call.
* The mark clears by itself when postings flow again.

## Protecting buyers and repeat sales (Phases 28-31)

**Phase 28: heartbeat** (`agent/heartbeat.py`, task `send_heartbeat`).
* The agent can only email you while it runs. If the Mac is off, asleep or offline, or the agent
  crashed, an outside service has to notice.
* Each cycle the agent pings `HEALTHCHECK_URL`. A free check at healthchecks.io emails you when
  the pings stop.
* Setup: run `automonetize heartbeat`, which prints the three steps. Then run
  `automonetize heartbeat <ping URL>`: it tests the URL, saves it and restarts the agent. The URL
  can also be set in the control panel (Settings → Monitoring, which also has your report email).
* `automonetize doctor` warns until a heartbeat is set.

**Phase 29: release gate** (`strategies/release_gate.py`, used by the packager). Before a new
dataset version replaces the one on sale, it's checked:
* More than `release_gate_max_drop` (50%) of rows lost → held. If the drop still holds after
  `release_gate_hold_days` (3), it's treated as real and released.
* Over 20% of rows without a company or job title → held until fixed (a parser problem). Time
  never releases this one.
* While held, buyers keep getting the previous version. You get one alert, and the hold shows in
  the doctor and the daily report.

**Phase 30: refresh offers** (`strategies/refresh_offers.py`, task `offer_refresh`). A buyer gets
one offer to buy the current dataset at `refresh_discount_pct` (50%) off when:
* their copy is at least `refresh_after_days` (30) old; and
* the dataset now has at least `refresh_min_new_rows` (25) more rows than theirs.

The email shows real numbers ("your copy has 140 rows; today's has 212").
* The code is a single-use Stripe promotion code for that product, valid 14 days. Its link
  applies the code at checkout.
* Never sent for refunded or disputed orders, to suppressed addresses, to buyers who already
  bought a newer copy, or for stale datasets.
* Shares the one-email-per-14-days limit with the new-release emails.
* In dry run, the email is logged with a placeholder code and nothing is created in Stripe.

**Phase 31: launch codes** (`strategies/launch_promos.py`, task `create_launch_promos`).
* Each newly released niche gets one code: `launch_discount_pct` (20%) off that product only, for
  `launch_promo_days` (7) after the release. Stripe enforces the expiry, so the deadline in the
  copy is true.
* The new-release emails and the share kit show the code, with links that apply it. The share
  kit drops it when it expires.
* Live mode only. Payment Links get `allow_promotion_codes` switched on when their first code is
  created.

## Hands-off upkeep and repeat customers (Phases 32-35)

**Phase 32: self-update** (`agent/self_update.py`, task `self_update`). You no longer need
`git pull`. Every `auto_update_hours` (6), the agent checks the branch your checkout tracks:
1. **Sandbox:** new commits are checked out in a temporary git worktree, merged with any local
   commits (e.g. self-evolution's). A conflict stops the update and alerts you.
2. **Dependencies:** if the requirements changed, they're installed first.
3. **Checks:** the full test suite and the end-to-end simulator must pass in the sandbox, with no
   secrets in its environment.
4. **Switch:** the branch fast-forwards and the agent reloads, keeping the webhook socket open.
5. **Canary:** for 60 minutes, a crash, a new kind of failure or an exception in a changed file
   rolls back to the previous commit and reloads. That version is then skipped until a newer one
   is published.

It never updates over uncommitted changes, a detached HEAD, or while a self-evolution canary is
running. Tests, the simulator and evolution checks set `AM_NO_SELF_UPDATE`, so they can never
touch the real checkout. Off with `auto_update = false`. Status appears in `automonetize doctor`.

**Phase 33: owner to-do** (`strategies/owner_todo.py`). One short, ranked list of what only you can
do, each item with why it matters, about how long it takes, and the exact command or place:
* go live;
* answer a dispute;
* deliver an order by hand;
* add the postal address;
* connect marketing;
* review sales emails;
* turn on the heartbeat;
* unblock self-update.

It sits at the top of the control panel, opens every daily report and is printed by
`automonetize todo`. Items disappear once done.

**Phase 34: referral rewards** (`strategies/referrals.py`, task `reward_referrals`).
* Each buyer's follow-up email carries a personal link: the normal checkout, attributed to them.
* When a friend buys through it, and the friend's order is 7 days old and not refunded or
  disputed, the referrer gets the newest version of that dataset free, as an attachment.
* No self-referrals, and at most one reward per referrer per 30 days. It costs nothing.
* Off with `referrals = false`.

**Phase 35: subscriber win-back** (`strategies/winback.py`, task `win_back`).
* 7-30 days after a subscription is canceled, the former subscriber gets one email: what changed
  in their niche since they left (real numbers), and `winback_discount_pct` (50%) off the first
  month back with a single-use 14-day code on a link that applies it.
* Never sent to anyone who has resubscribed or is suppressed, and only once ("the only time I'll
  ask").
* Postal address and opt-out included. In dry run nothing is created in Stripe.

## Turning contacts into sales (Phases 36-39)

All the offers below use real Stripe promotion codes (`tools/promo.py`). Stripe enforces each
code's product, amount, expiry and redemption limit, and every link applies its code at checkout.
They never go to suppressed addresses. In dry run nothing is created in Stripe.

**Phase 36: bundle upgrade credit** (`strategies/bundle_upgrade.py`, task `offer_bundle_upgrade`).
* Once the all-datasets bundle is on sale, buyers who own some but not all of its datasets get one
  email, `bundle_upgrade_after_days` (10) after their last purchase.
* What they paid counts: a single-use, fixed-amount code for the bundle only, valid 14 days.
  Refunded and disputed orders don't count, and they always pay at least $1.
* Shares the one-email-per-14-days gap with the other offers.

**Phase 37: sample-to-paid offer** (`strategies/sample_offer.py`, task `offer_sample_upgrade`).
* Confirmed free-sample signups who haven't bought after `sample_offer_after_days` (14) get one
  single-use code: `sample_offer_pct` (25%) off their niche's dataset, valid 7 days.
* Same compliance as the weekly sample email: their one-click unsubscribe link, the RFC 8058
  header and the postal address. It waits until both exist.

**Phase 38: testimonials** (`strategies/testimonials.py`).
* The follow-up email invites a one-line reply with "OK to quote".
* The support desk turns such replies into pending quotes, in the customer's own words. Sentences
  with emails, links or phone numbers are dropped, and nothing without the consent phrase is ever
  used.
* You approve or reject each quote in the control panel (**Share → Customer quotes**); the to-do
  list reminds you.
* Each product page shows up to three approved quotes as "Verified buyer".

**Phase 39: quarterly sale** (`strategies/seasonal_sale.py`, task `run_sale`).
* Every `sale_every_days` (90), once the store has been open 30 days, the agent creates one code:
  `sale_pct` (25%) off every dataset and the bundle for `sale_days` (3).
* Past buyers get one email listing only what they don't own, and the share kit shows the code on
  every product.
* The daily report mentions the sale. Live mode only.

## Trust: reputation, privacy and security (Phases 40-44)

**Phase 40: contact policy** (`tools/contact_policy.py`). Every marketing email (follow-ups, new
releases, refresh, win-back, bundle upgrade, free-sample offer, sale) goes through one gate and is
logged in the table `contact_log`:
* never to suppressed addresses;
* nothing while the bounce guard has paused marketing email;
* at most one *offer* per person per `announce_min_gap_days` (14), whatever the offer type (a
  follow-up after a purchase doesn't count);
* at most `promo_daily_cap` (150) marketing emails a day in total, well under Gmail's limit.

Purchases, receipts, support replies and referral rewards are not marketing and are never held.

**Phase 41: bounce guard** (`strategies/bounce_guard.py`, task `process_bounces`).
* Reads bounce notices (only from `mailer-daemon@` / `postmaster@`, read-only) and suppresses
  hard-bounced addresses for good. Temporary failures, such as a full mailbox, are ignored.
* If more than `bounce_pause_rate` (5%) of the last 7 days' emails bounced (with at least 20
  sent), all marketing email, including approved sales emails, pauses for 7 days and you get an
  alert. Purchases and support replies still go out.

**Phase 42: offer tuner** (`strategies/offer_tuner.py`, task `tune_offers`).
* Weekly, per offer type (refresh, win-back, free-sample, sale), it compares emails sent with the
  sales they brought, counting since the discount last changed:
  * 30 or more emails and no sale → the discount goes up 10 points;
  * 15% or more converting → it comes down 5 points.
* Bounded per offer (e.g. refresh 20-60%). `automonetize offers` shows the table. Off with
  `offer_tuning = false`.

**Phase 43: privacy requests** (`tools/privacy.py`).
* The support desk recognises "delete my data" / GDPR / CCPA emails and alerts you with the exact
  commands. The request also stays on the to-do list (the law gives you 30 days).
* `automonetize privacy export <email>` writes everything stored about the person to a JSON file.
* `automonetize privacy forget <email>` (asks you to type FORGET):
  * deletes their drafts, contact log, quotes, referral link, free signup and email-log lines;
  * anonymises orders and subscriptions, keeping amounts for your books;
  * redacts logs;
  * keeps a suppression entry, so they're never emailed again.

**Phase 44: security self-audit** (`agent/security_audit.py`, task `audit_security`, daily).
* **Fixes by itself:** `.env` and data permissions, and `.env` missing from `.gitignore`.
* **Reports** (alert, doctor and to-do):
  * `.env` committed to git;
  * secrets in `automonetize.toml`;
  * the control panel reachable from other machines;
  * live payments without a webhook secret;
  * a full `sk_live_` key. The fix lists the exact permissions for a *restricted* key, which
    limits the damage if a key ever leaks.
* In sandboxes (tests, simulator, evolution checks) it only reports.

## Long-running reliability (Phases 45-49)

**Phase 45: CI on Linux and macOS** (`.github/workflows/tests.yml`).
* Every push runs lint, the full suite and the end-to-end rehearsal on a macOS runner and on Linux
  (Python 3.11 and 3.13).
* The first run found real Python 3.13 failures: unclosed database handles. They're fixed: stores
  now close themselves when dropped, and the CLI tracks open stores by object, not `id()`.
* This matters because self-update (Phase 32) runs the same suite on your Mac before installing
  anything.

**Phase 46: pre-change backups and integrity** (`agent/backup.py`).
* Self-update and self-evolution take a "pre-update" / "pre-evolution" backup first.
* The daily backup first runs `PRAGMA quick_check`. If a database is damaged, you get an alert
  with the exact restore command, and no old backup is pruned.

**Phase 47: housekeeping** (`agent/housekeeping.py`, task `housekeeping`, daily).
* Deletes actions after `log_keep_days` (90) and errors after twice that.
* Deletes handled webhook events after 90 days and the contact log after 400.
* Gzips the email audit log once it passes 5 MB and keeps the archives for a year.
* Deletes files of dataset versions beyond the newest `keep_versions` (3), **except any version
  someone bought** (order recovery re-sends exactly that file). Database rows are never deleted.

**Phase 48: disk guard** (`agent/disk_guard.py`, task `check_disk`, first in every cycle).
* Below `disk_warn_gb` (2 GB) free: one alert a day naming the biggest agent folders.
* Below `disk_critical_gb` (0.5 GB): housekeeping runs immediately and new dataset builds pause
  until there's room. The databases and purchased files are never what fails.

**Phase 49: config check** (`agent/config_check.py`, in `automonetize doctor`).
* Flags misspelt settings in `automonetize.toml`, with "did you mean …?" (otherwise they're
  silently ignored).
* Flags out-of-range values and malformed emails and URLs.

## Delivery and payment safety (Phases 50-54)

**Phase 50: download links for big files** (`tools/download_links.py`).
* A dataset too big to attach (over 8 MB) is no longer marked "needs manual delivery". The email
  carries a private link served by the agent's public server at `/d/<token>`.
* The link expires after `download_link_days` (7) and works `download_link_uses` (5) times.
* Only the token's hash is stored, and only files inside the data folder can be served.
* Order recovery issues fresh links the same way.

**Phase 51: webhook health** (`strategies/webhook_health.py`, task `check_webhook`, every 6 h when
live).
* Checks that the public URL answers (the tunnel is up).
* Checks that Stripe's endpoint for it exists, is enabled and has every event. If not, it's
  repaired with setup's own code, and a new signing secret means a reload.
* Alerts once a day if Stripe failed to deliver events. Polling still picks up the orders.

**Phase 52: card-testing guard** (`strategies/payment_guard.py`, task `guard_payments`).
* `card_testing_threshold` (10) or more failed charges in an hour raises an alert, at most every
  6 hours, with the Radar rules to turn on.
* Deactivating a link stays your decision, because it stops real sales too.

**Phase 53: duplicate purchases.** The same buyer paying twice for the same dataset within 7 days
gets one alert and a to-do item with both order ids. The refund stays your decision.

**Phase 54: Stripe Tax.**
* When Stripe Tax is active on the account, new Payment Links get `automatic_tax` and existing
  live links are switched over once.
* When it isn't, the doctor tells you, because whether you must collect sales tax or VAT depends
  on where you and your buyers are.

## More ways to buy (Phases 55-59)

**Phase 55: team license** (`strategies/plans.py`, task `publish_team_license`).
* For each dataset on sale: the same file plus `LICENSE-TEAM.txt`, for up to `team_license_seats`
  (10) people in one organisation, at `team_license_multiplier` (3x) the price.
* It's shown on the dataset's page ("Buying for a team?"), not as a separate page or post.
* A new dataset version rebuilds the team file behind the same link. A new price makes a new link
  and retires the old one.

**Phase 56: yearly plan** (task `publish_annual_plan`).
* For each monthly subscription, a yearly option at `annual_months_paid` (10) months' price.
* It's synced and delivered like the monthly plan.

**Phase 57: thank-you page** (`tools/offer_pages.py`, task `checkout_thank_you`).
* The site gets `thanks/` (noindex), which confirms the file is on its way and offers the bundle,
  weekly updates and the team license.
* Once that page is confirmed online, every live Payment Link redirects there after payment, and
  new links do too.

**Phase 58: disposable emails refused** (`tools/disposable.py`).
* The free-sample form refuses throwaway inboxes (mailinator, yopmail, 10minutemail and about 45
  more, including subdomains) with a polite page.
* Add your own with `blocked_signup_domains`.

**Phase 59: pricing page.** The site gets `pricing/`: every dataset with its one-off, weekly,
yearly and team prices side by side, plus the bundle.

## Running it from your phone (Phases 60-64)

**Phase 60: phone notifications** (`tools/notify.py`).
* `automonetize phone` makes a private topic, saves `NTFY_TOPIC`, sends a test and tells you what
  to tap in the free ntfy app (no account).
* Every sale and alert then buzzes your phone. Messages never contain a customer's address.

**Phase 61: email commands** (`agent/owner_commands.py`).
* From your owner address, email the sender address with the subject `AM STATUS <code>`. The code
  is at the bottom of every daily report, and `automonetize commands --new` replaces it.
* Commands:
  * `STATUS`, `TODO`;
  * `PAUSE`, `RESUME` (cycles stop; deliveries continue);
  * `QUIET`, `LOUD` (hold or release marketing email);
  * `HELP`.
* The agent replies to confirm. Commands are read before the pause check, so `RESUME` works while
  paused. Mail without the code, or from anyone else, is ignored.

**Phase 62: quiet mode** (`tools/contact_policy.py`).
* `automonetize quiet on|off`, the Health tab, or `AM QUIET`/`AM LOUD` by email.
* Holds every marketing email and approved sales email. Purchases, receipts and support replies
  always go out.

**Phase 63: report preferences.**
* `sale_alerts` = `each` (default) | `daily` (in the digest only) | `off`.
* `owner_digest` = `daily` | `weekly` (Mondays) | `off`.

**Phase 64: Health tab** (control panel, `gui/routes/health.py`).
* The doctor's checks, problems first, with **Fix** buttons for the safe automatic fixes (start the
  agent, autostart, install an update).
* A quiet-mode switch. CSRF-protected like every other button.

## Site quality and customer insight (Phases 65-69)

**Phase 65: site audit** (`tools/site_audit.py`, run on every site build, before publishing).
* Checks for broken internal links, a missing title or meta description (best 50-160
  characters), anything but exactly one `<h1>`, images without alt text, and pages over 500 KB.
* New broken links raise one alert, and the doctor shows the rest.
* Its first run found two real problems, now fixed:
  * meta descriptions are now cut at a word boundary at 158 characters;
  * the `intel/` index always exists, because every page links to it.

**Phase 66: version history pages.** Each dataset gets `<dataset>/changelog/`, listing every
version with its date, row count and growth, linked from the product page. It's proof the data is
alive, and fresh content for search engines.

**Phase 67: customer list** (`tools/customers.py`, `automonetize customers`).
* Writes `data/exports/customers.csv` with one row per buyer: first and last purchase, number of
  purchases, total spent, products and first channel.
* Also shows whether they're subscribed or suppressed and when they last got an email.
* Refunded, disputed and erased customers aren't included.

**Phase 68: lifetime value** (`automonetize customers --report`).
* Shows the repeat-purchase rate, average lifetime value, and lifetime value by first channel and
  by first-purchase month.
* A one-line summary appears in Monday's report.

**Phase 69: uninstall** (`automonetize uninstall`, asks you to type UNINSTALL).
* Takes a final backup, stops the agent and removes the launchd jobs.
* Keeps your data unless you add `--delete-data` (which asks you to type DELETE).
* `--deactivate-links` also switches off every live Payment Link, so nobody can buy what will no
  longer be delivered.

## First run (Phases 70-74)

**Phase 70: `automonetize setup`.** One guided command that only asks about what's still missing,
most important first. It's safe to run again: finished steps are just ticked off. In order:
1. Python version.
2. Real payments (`go-live`).
3. Postal address and report email.
4. Public site and articles (`connect-marketing`).
5. Phone notifications and heartbeat.
6. Start at login.
7. A connections check and the remaining to-do list.

**Phase 71: `automonetize connections`** (`tools/connections.py`). A read-only check of every
outside service, with what to do when one fails. Nothing is created or sent:
* Stripe (live or test);
* SMTP login and IMAP login;
* the public URL through the tunnel and the website;
* GitHub and Dev.to;
* the heartbeat.

**Phase 72: "agent started" notice** (`agent/startup_notice.py`). The first cycle after every
start sends you one email (and a phone notification): which Mac, which version, connection results
and how many to-do items are left. At most one every 6 hours.

**Phase 73: version and what's new.**
* `automonetize version` shows what's running. The control panel and the daily report show it too.
* After a self-update passes its canary, you get one email listing what changed.

**Phase 74: crash-loop rollback** (`agent/last_good.py`).
* A commit that runs 24 hours without a crashed cycle becomes "last known good".
* Three crashed cycles in a row on any other commit reset the checkout to it (`git reset --keep`,
  never over uncommitted edits). The agent then reloads, self-update skips that version, and you
  get an alert.

## What buyers receive (Phases 75-79)

Every dataset zip now also contains (`strategies/dataset_extras.py`):
* **Phase 75, `QUALITY.md`:** an honest coverage report computed from the file itself:
  * rows and distinct companies;
  * share of rows with a domain, a posting link, a salary or a stack;
  * remote share, median posting age and share posted in the last 7 days;
  * sources, and verified careers pages.
* **Phase 76, `FIELDS.md`:** what every column means.
* **Phase 77, `leads-excel.csv`:** the same rows with a UTF-8 byte-order mark, so Excel shows
  accented names correctly. `leads.csv` stays plain UTF-8.

**Phase 78: quick-start in the delivery email.** How to open the files in Excel or Google Sheets,
where to start, and a sorting tip.

**Phase 79: customer requests log** (`strategies/customer_requests.py`).
* Replies that ask for something ("could you add…", "do you have one for Rust?") are recorded with
  the technologies they mention. They still reach you as before.
* Monday's report shows the last 30 days' count, the most-asked topics and the latest requests.

## Site trust (Phases 80-84)

`tools/site_extras.py`:

**Phase 80: legal pages.**
* `legal/terms/`, `legal/privacy/`, `legal/refunds/` and `contact/`, written from your settings
  (name, contact email, postal address, `refund_policy_days`).
* Linked from every page's footer.
* Payment processors expect a visible refund policy and contact details. These are templates from
  facts, not legal advice: read them once in `data/site/`.

**Phase 81: sale banner.** While a sale or launch code runs, the product and pricing pages say so
at the top.

**Phase 82: FAQ on product pages.** Format, delivery, freshness, refunds, plus updates and team use
when those exist, with `FAQPage` structured data.

**Phase 83: search engine verification.** `google_site_verification` / `bing_site_verification`
add the meta tags Search Console and Bing Webmaster Tools ask for.

**Phase 84: 404 page.** `404.html` with absolute links to every dataset and pricing, so a stale
link still lands somewhere useful.

The site audit (Phase 65) checks all of these. It caught a wrong-depth footer link during this
batch before it shipped.

## Money admin (Phases 85-89)

**Phase 85: expenses** (`tools/expenses.py`).
* `automonetize expense add 12.00 "domain renewal" [--date YYYY-MM-DD] [--category tools]`, plus
  `expense list [YYYY-MM]` and `expense delete ID`.
* Stripe's fees are already in the revenue rows: don't add them here.

**Phase 86: profit and tax set-aside.**
* Monthly books now list expenses, then profit (net revenue minus expenses), then a suggested tax
  set-aside of `tax_set_aside_pct` (25%) of profit. That's a rule of thumb, not tax advice.
* The books email leads with profit.

**Phase 87: year-end books.** In January, the agent emails the whole previous year as one CSV with
the same totals. `automonetize books 2026` builds any year by hand.

**Phase 88: payout watch** (`strategies/payout_watch.py`, task `watch_payouts`). An alert when a
payout to your bank fails or is canceled, and when money has sat in Stripe for `payout_watch_days`
(30) without a payout (a manual schedule or an unverified bank account).

**Phase 89: goal ladder.** After 7 days in a row on goal, the to-do list suggests a goal 50% higher,
rounded up to $5. `automonetize goal 15` sets it. It's never raised automatically.

## Transparency (Phases 90-94)

**Phase 90: key-age reminders** (`tools/key_age.py`).
* The agent keeps a 12-character fingerprint of each secret (never the secret) and the date it
  first saw it.
* A Stripe key older than 180 days, or another key older than a year, shows up on the to-do list
  with where to roll it.

**Phase 91: settings change log** (`tools/settings_log.py`, `automonetize settings-log`). At each
start, the settings are compared with the last ones seen and every change is recorded ("dry_run:
true → false"). Secrets appear only as set, changed or removed.

**Phase 92: Sent tab.** Every email the agent sent (or would have sent in dry run), newest first,
with its text, in the control panel.

**Phase 93: `automonetize export-all`** (`tools/export_all.py`).
* Writes every table as a CSV plus the settings store into one zip (mode 600), for a spreadsheet
  or an accountant.
* Keys stay in `.env`, and the email-command code is left out.

**Phase 94: `automonetize features`** (`tools/features.py`). Every major feature, grouped, and
whether it's **on**, **off** (and which setting), or **waiting** (and for what from you).

## Richer datasets (Phases 95-99)

Every new dataset zip also contains:
* **Phase 95, `CHANGES.md`:** companies new since the previous version, and those no longer hiring.
* **Phase 96, `TOP20.md`:** the 20 companies to contact first, by hiring urgency (or open roles),
  with their stack.
* **Phase 97, `regions/`:** `leads-us.csv`, `leads-europe.csv` and `leads-remote.csv`, split by the
  posted location.
* **Phase 98, `leads.jsonl` and `schema.sql`:** one JSON object per line, and a file that loads into
  SQLite or Postgres as is.

**Phase 99: niches customers ask for come first.**
* The customer-requests log also recognises technologies seen in the collected job postings.
* When the agent picks its next niche, anything two or more customers asked for goes first: the
  configured niche it belongs to, or a new niche built around that technology.

`max_actions_per_cycle` now defaults to 60. The engine raises any lower cap, including a supplied
toolkit's, to the plan size + 10.

## Conversion (Phases 100-104)

* **Phase 100, trust row:** under the buy buttons, every product page says "🔒 Secure checkout by
  Stripe · Instant delivery by email · N-day refund", linking to the refunds page
  (`refund_policy_days`).
* **Phases 101-102, 1-click ratings:** the buyer follow-up email carries 😀 / 😐 / ☹️ links.
  * Each link opens a page with one confirm button, so mail-scanner clicks don't count as votes.
  * Each order can be rated once. The token is random and only its hash is stored.
  * "Not good" raises an alert and adds "Reach out to N unhappy buyer(s)" to the to-do list.
  * "Great" invites a quotable one-liner.
  * Monday's report shows the counts.
* **Phase 103, most popular:** the dataset with the most orders kept (not refunded or disputed) in
  30 days, with at least two, is badged on the home and pricing pages.
* **Phase 104, per-dataset feed:** `<dataset>/changelog/feed.xml` gets one RSS item per version, and
  the product page advertises it.

**Tunnel routes: re-run the tunnel setup once.**
* Download links (`/d/…`) and rating links (`/r/…`) are now routed through the Cloudflare tunnel.
  Earlier configs didn't route the download links, so buyers clicking them got a 404 from Cloudflare.
* If your config predates this, the to-do list says so. Re-run
  `bash deploy/tunnel/setup_tunnel.sh <your hostname>` to fix it.

## Ops robustness (Phases 105-109)

A new `ops_checks` task runs every cycle (`agent/ops_checks.py`). Its results appear in `automonetize
doctor` and the Health tab.
* **Phase 105, job-source health:** a job board that returned no postings for 3 runs in a row
  triggers an alert (at most one a day per board) that includes its last error.
* **Phase 107, clock check:** once a day, the Mac's clock is compared with GitHub's HTTPS `Date`
  header. If it's more than 2 minutes off you get an alert, because Stripe rejects webhook signatures
  that are more than 5 minutes off.
* **Phase 108, battery saver:** on a MacBook running on battery, these tasks wait until it's plugged
  in again: dataset builds, intel builds, site builds, source discovery, satellites and code
  evolution. Sales, deliveries and email keep running. Turn it off with `battery_saver = false`.

Two changes elsewhere:
* **Phase 106, monthly VACUUM:** housekeeping compacts the database once a month. It skips this when
  free disk is less than twice the database size.
* **Phase 109, grouped errors:** the daily report lists each distinct problem once, with a count
  (`task:build_site (×5): …`), instead of repeating the same error.

## Owner experience (Phases 110-114)

* **Phase 110, HTML report:** the daily report email now also has a tidy HTML version, with
  headings, lists and clickable links. It loads no images and has no tracking. Plain text is still
  sent alongside it.
* **Phase 111, phone summary:** with `ntfy_topic` set, each report also sends one line to your phone,
  e.g. "Yesterday $19.00 · 7 days $57.00 · 0 alert(s) · 1 thing(s) only you can do". Turn it off with
  `digest_push = false`.
* **Phase 112, quiet for a while:** `automonetize quiet on --days 7` holds marketing email for a
  week, then lets it go out again by itself.
* **Phase 113, task timings:** `automonetize marketplace-export          # listing kits to upload to Gumroad / Lemon Squeezy / data marketplaces
automonetize affiliate [add EMAIL | paid CODE]   # affiliates and what you owe them
automonetize products                     # every product ranked by revenue
automonetize marketing [plan|scores|report|done ID|skip ID]   # drafts to post, the week's plan, what works
automonetize marketing directories [done NAME]        # where to list the catalog, and what's done
automonetize sponsor [list] | sponsor approve ORDER_ID "line" URL   # sponsorships (you approve each one)
automonetize factory [--now|--next|--types|--csv]  # the catalog; make one now; preview the next; by type; spreadsheet
automonetize audit                        # every self-check in one report; exit 1 if anything failed
automonetize explain TASK                 # what a task does and its last runs
automonetize what-changed [--days N]      # settings, prices, versions, updates, resting tasks, mutes
automonetize mute SOURCE [--days N] | mute --list   # pause alert emails from one source (still logged)
automonetize unmute SOURCE                # alert emails from that source again
automonetize report [--now|--print]       # today's report, now
automonetize forecast                     # where this month is heading at the recent pace
automonetize price-history                # every price change, newest first
automonetize timings [--days N]` shows how long each task takes.
  Monday's report names the three slowest.
* **Phase 114, milestones:** you get one email (and a phone buzz) for each milestone, sent once:
  * your first sale;
  * the first day at or above the daily goal;
  * $100 total net;
  * $1,000 total net.

  An existing install doesn't celebrate past milestones.

## Hygiene (Phases 115-119)

* **Phase 115, libraries:** `automonetize deps` (also run by `doctor` and the Health tab) checks
  that the installed packages meet `pyproject.toml`. That catches a `git pull` without a
  `pip install`. The fix is always `pip install -e '.[dev]'`.
* **Phase 116, every setting documented:** `automonetize config-docs [--markdown]` lists all ~250
  settings, with defaults and descriptions, read from `agent/config.py` itself. Every setting now has
  a description.
* **Phase 117, data retention:** housekeeping deletes old data nobody needs:
  * job postings not seen for `lead_keep_days` (365);
  * free-sample sign-ups that never confirmed within `unconfirmed_signup_days` (30).
* **Phase 118, accessibility:** the site audit also flags:
  * pages without `lang`;
  * unlabelled form fields;
  * links and buttons with no text.
* **Phase 119, consistency checks:** `tests/test_consistency.py` checks that these agree:
  * every public route is exposed by the tunnel;
  * every planned task has a handler, and every handler is planned;
  * every CLI command is in this README;
  * every setting is explained.

### Audit after Phase 119

A full review: static analysis (pyflakes, ruff with bugbear and bandit rules), a security pass over
every public route, the control panel, SQL construction and secrets (including git history), the
consistency checks, and both test suites (Python 3.11 and 3.13). Fixed:
* **Download links weren't reachable through the tunnel.** `/d/` and `/r/` are now routed. Re-run
  the tunnel setup once (the to-do list says so).
* **`/healthz` revealed sales volume.** Through the tunnel it showed webhook event counts. It now
  answers `{"ok": true}` publicly; the counts are only shown to local callers.
* **Download and rating links had no rate limit.** They now allow 300 requests per IP per hour and
  send `nosniff` and `no-referrer` headers.
* **Two simultaneous clicks could double-count.** The last download of a link and a rating are now
  taken atomically.
* **A second rating click showed the wrong page.** It said "thanks" for a vote that wasn't recorded;
  it now says the rating is already in.
* **The battery saver could hold builds indefinitely.** If its checks stopped, an old "on battery"
  reading kept heavy builds paused. Readings older than 3 hours are now ignored.
* **A failed VACUUM repeated every cycle.** If the database is busy, compaction now waits a day
  instead of failing housekeeping each cycle.
* **Documentation gaps:** 36 settings had no description, and three commands were missing from
  the README.

## More of the site (Phases 120-124)

* **Phase 120, compare page:** `compare/` shows every dataset side by side: rows, companies, open
  roles, last update and price.
* **Phase 121, complete sitemap:** the sitemap now also lists the pricing, compare, version-history,
  legal and contact pages.
* **Phase 122, `llms.txt`:** a plain-text summary of what's sold, with prices and links, for AI
  assistants.
* **Phase 123, home page structured data:** the home page carries `Organization` and `WebSite`
  data for search engines.
* **Phase 124, `security.txt`:** `/.well-known/security.txt` (RFC 9116) gives a contact for
  security reports and expires after a year; every build refreshes it.
  * It's only built when a contact address is set.
  * A `.nojekyll` file makes GitHub Pages serve it.

## Money insight (Phases 125-129)

* **Phase 125, forecast:** the daily report says where the month is heading, at the last 14 days'
  pace, against the month's goal. `automonetize forecast` shows the same.
* **Phase 126, price history:** every price change is recorded, whether the pricing strategy made
  it or you did. `automonetize price-history` lists them.
* **Phase 127, refund guard:** if more than `refund_alert_rate` (10%) of the last 30 days' orders
  were refunded or disputed (with at least 5 orders), "Look into refunds" goes on the to-do list.
* **Phase 128, abandoned checkouts:** Monday's report counts checkouts started but not paid in the
  last week, and what they were worth.
* **Phase 129, niche trends:** Monday's report compares each niche's last 14 days with the 14
  before.

## Reliability (Phases 130-134)

* **Phase 130, failing tasks rest:** a task that fails 3 times in a row is skipped for 24 hours, with
  one alert naming it and its last error. Everything else keeps running.
  * Its first success clears the count.
  * Tasks that handle money or customers (sync, delivery, dunning, refunds, support) never rest.
  * `doctor` lists resting tasks.
* **Phase 131, `data/agent.log` rotates:** at most 10 MB, with 3 older copies.
* **Phase 132, service logs are trimmed:** housekeeping cuts launchd's
  `~/Library/Logs/automonetize/*.log` files back to their last 2 MB once they pass 20 MB.
* **Phase 133, backups are restore-tested:** once a week the newest backup is restored into a
  temporary folder and opened.
  * A backup that doesn't open raises an alert.
  * `doctor` shows the last result.
* **Phase 134, secrets are redacted:** API keys, tokens and passwords are stripped from every
  stored error and traceback before it reaches the database, the reports or the backups.

## Owner control (Phases 135-139)

* **Phase 135:** `automonetize explain build_site` says what a task does (from its own code), where
  it runs in the cycle and its last 5 runs.
* **Phase 136, two more email commands:**
  * `AM REPORT <code>` replies with today's full report.
  * `AM QUIET 7 <code>` holds marketing email for 7 days.
* **Phase 137, mute an alert source:** `automonetize mute site_audit --days 3` stops alert emails and
  phone buzzes from that source. The alerts are still recorded and shown in `doctor`.
  `automonetize mute --list` shows mutes and recent sources; `automonetize unmute <source>` ends one.
* **Phase 138:** `automonetize what-changed [--days N]` lists, newest first: settings, prices, new
  dataset versions, installed updates, resting tasks and mutes.
* **Phase 139:** `automonetize report --now` emails today's report right away; `--print` shows it
  in the terminal instead.

## Product factory (Phases 145-149)

The agent no longer builds one product per niche and stops: it keeps making new ones.

* **Phase 145, product ideas:** it takes every job posting collected from every board and slices
  them into narrower datasets people search for.
  * Each technology alone, and crossed with a region (US, Europe), remote-only, and senior or
    junior: "Companies Hiring Rust Engineers in Europe", "Remote Companies Hiring for Senior
    Kubernetes Engineers".
  * Ideas are ranked by how many companies they cover, and topics customers asked for go first.
* **Phase 146, building:** each product is a zip like the main datasets: CSV, Excel CSV, JSON,
  JSONL, SQL, QUALITY.md, FIELDS.md, TOP20.md and a README. It also gets a 5-row public preview.
* **Phase 147, publishing:**
  * a Stripe product and payment link, priced by size (`factory_prices`: $5, $9 or $14);
  * a page on the site;
  * delivery like every other dataset.
* **Phase 148, every 10 minutes:**
  * Under `automonetize supervise`, a separate `factory` worker (with its own API budget) makes one
    product every `factory_interval_seconds` (600).
  * Under `automonetize run`, the `run_factory` task catches up each cycle, up to 6 products.
  * `automonetize factory` shows the catalog; `--now` makes one immediately; `--next` previews the
    next one.
* **Phase 149, quality floor.** Making thin or duplicate products would hurt search ranking and your
  Stripe account's standing, so a product needs:
  * at least `factory_min_rows` (20) postings from `factory_min_companies` (8) companies, posted in
    the last `factory_max_age_days` (60);
  * to be distinct: a slice whose postings are 85% the same as an existing product's (counted over
    both together) is skipped, while a narrower slice such as "Rust in Europe" next to "Rust" is fine.

  Two more limits keep the catalog worth browsing:
  * at most `factory_max_live` (500) factory products are on sale at once;
  * a product with no sale after `factory_retire_days` (60) is retired: its link is deactivated and
    its page removed.

  When nothing new clears the floor, the factory waits for more postings rather than padding. More
  job sources (self-discovered ones too) mean more products.

The default caps per cycle are now 100 actions and 200 API calls, so the site can publish the new
pages as they appear.

## More product types (Phases 150-154)

The factory takes these in turn with the slices, so the catalog grows in several directions:

* **Phase 150, salary benchmarks** (`salary-<tech>`, $7): postings that state a salary, with the
  median and middle half by seniority and region.
  * Only yearly figures between 10k and 1M are used, so hourly rates don't skew the numbers.
  * Currencies aren't converted, and the README says so.
* **Phase 151, top companies** (`top-companies-<tech>`, $9): one row per company, ranked by open
  roles, with titles, locations, remote share and stack.
* **Phase 152, remote-first employers** (`remote-first-employers[-<tech>]`, $9): companies with 3+
  open roles, 70%+ of them remote.
* **Phase 153, starter packs** (`pack-<tech>`): 3 or 4 live products about one technology in one
  download, at 30% off their total.
* **Phase 154, weekly refresh:** every live factory product is rebuilt from fresh postings once a
  week.
  * The new version goes behind the same checkout and price, so buyers always get current data.
  * A sale on any version keeps a product from being retired.

## More ways to get paid (Phases 155-159)

The `publish_offers` task creates each offer once (it needs live payments and working email). The
site's `more/` page lists them.
* **Phase 155, custom dataset ($29):**
  * Checkout asks "Technology, region or seniority?".
  * The agent reads the answer, builds that slice from every current posting and emails it.
  * If it can't, the order waits for you with an alert quoting the request. You reply or refund:
    money decisions stay yours.
* **Phase 156, name your price (from $3):** a supporter product. The buyer picks the amount and gets
  the catalog index: every product with its link.
* **Phase 157, sponsorship ($49 a week):** after payment you get an alert. Nothing shows until you
  run `automonetize sponsor approve ORDER_ID "line" https://url`. The line then appears at the top
  of the home and pricing pages for 7 days, marked as sponsored.
* **Phase 158, lifetime pass ($149):** download links for everything on sale now, then a weekly
  email with links to products first released that week. It needs the tunnel for download links.
* **Phase 159, gift card ($25):** the buyer gets a one-time code worth what they paid, valid for a
  year, usable on any product. New payment links accept promotion codes.

Storefront health now checks a rotating window of 40 products per run, so a large catalog doesn't
use up the API budget.

## Upsells (Phases 160-164)

* **Phase 160, related datasets email** (`related_offers`): 5-12 days after an order, one email
  listing up to 3 live products about the same technology that the buyer doesn't own.
  * It's a marketing email: the contact rules apply (one offer per person per 14 days, quiet mode,
    unsubscribes).
  * It carries the unsubscribe line and your postal address.
* **Phase 161, often bought together:** each product page lists up to 3 related products, the
  starter pack first when there is one.
* **Phase 162, more of what sells:** the factory ranks ideas about technologies that sold in the
  last 30 days higher, so new products follow demand.
* **Phase 163, thank-you page:** after paying, buyers also see the lifetime pass and the custom
  dataset offer.
* **Phase 164, best sellers:** the home page lists the products with the most kept orders this
  month.

## More places to sell (Phases 165-169)

* **Phase 165:** `automonetize marketplace-export` writes a ready-to-upload kit for every live
  product into `data/exports/marketplace/`, plus an index:
  * the zip;
  * a listing text with title, description, price and tags.

  Upload the kits under your own Gumroad, Lemon Squeezy or data-marketplace account: the agent never
  signs up anywhere in your name.
* **Phase 166, affiliates:**
  * People apply from the site's `affiliates/` page.
  * You approve them with `automonetize affiliate add <email>`, which emails them their links.
  * The links carry `utm_source=aff`, and the product pages pass that into checkout.
  * They earn `affiliate_rate` (30%) of kept sales. `automonetize affiliate` shows what you owe;
    `affiliate paid <code>` records a payout you made yourself.
* **Phase 167, public catalog:** `GET /v1/catalog` (through the tunnel, rate-limited) returns every
  product as JSON for tools, aggregators and AI assistants.
* **Phase 168, free teasers on GitHub:** with `github_samples_repo` set, each product gets a 10-row
  teaser CSV (no contact details) in that public repo. The repo's README links to the full products.
* **Phase 169, product leaderboard:** `automonetize products` ranks every product by revenue and
  flags the ones that never sold.

## Marketing strategy engine (Phases 170-174)

* **Phase 170, playbook:** every traffic strategy the agent knows. Each turns the current catalog
  (best sellers first, then the newest products) into concrete *plays*:
  * **automatic plays** run by themselves: articles, feeds, teasers, SEO pages, the newsletter, and
    more added by the next phases;
  * **draft plays** are ready-to-post texts for places that don't allow bots or need your account:
    Reddit, LinkedIn, X, Show HN, Indie Hackers, Product Hunt and newsletter pitches. Each reminds
    you of that community's rules.
* **Every play is tracked:** its link carries `utm_source=<channel>&utm_campaign=mkt<id>`, so its
  checkouts and sales are counted.
* **Phase 171, scheduler** (`plan_marketing`, daily):
  * each strategy runs at its own rhythm (X daily, LinkedIn every 2 days, Reddit every 4, Show HN
    every 90...);
  * at most `marketing_drafts_per_day` (3) drafts are queued, chosen by score.
* **Phase 172, scoring:** per strategy over 60 days: plays, checkouts, kept orders and revenue per
  play, plus a bonus for strategies tried only a few times. Strategies that earn get scheduled more.
* **Phase 173, weekly calendar:** Monday's report and `automonetize marketing plan` show the next 7
  days of plays.
* **Phase 174, daily line:** the daily report says how many automatic plays ran, which drafts wait
  for you, and the best strategy this month.

To see your drafts, run `automonetize marketing`. Post one, then run
`automonetize marketing done <id>`, or `skip <id>`. `automonetize marketing scores` shows what's
working.

## SEO at scale (Phases 175-179)

* **Phase 175, unique content:** every factory product page states its own numbers: postings,
  companies, the companies with the most roles, top locations and remote share. They're computed
  when the product is built, so hundreds of pages are never thin copies of each other.
* **Phase 176, technology hubs:** `hiring/<tech>/` lists every dataset about one technology with
  size and price, and `hiring/` lists the technologies. The home page links to them.
* **Phase 177, answer pages:** `answers/how-many-companies-are-hiring-<tech>-engineers/` answers that
  question with the current numbers and date, with `FAQPage` data, linking to the hub.
* **Phase 178, breadcrumbs:** product pages carry `BreadcrumbList` data (Home > tech hub > product)
  and a "More <tech> datasets" link. Hubs link to the intel page for the same technology.
* **Phase 179, Google Dataset Search:** factory product pages also carry `Dataset` markup (size,
  format, date). That is what Google Dataset Search reads.

The site audit checks all of these pages, and the sitemap lists them.

## Content (Phases 180-184)

* **Phase 180, blog (daily):** a post on your site (`blog/<slug>/`) about the technology with the
  freshest data not covered in 30 days.
  * It covers how many companies are hiring, who has the most roles, remote share and seniority
    mix, plus salaries when postings state them.
  * It links to the technology hub and goes into the RSS feed.
* **Phase 181, weekly roundup:** "New hiring datasets this week" is syndicated through the usual
  publishers at their normal cadence, with links tagged per platform:
  * Dev.to, Hashnode and GitHub Discussions;
  * RSS and the Substack export.
* **Phase 182, weekly thread:** a ready-to-post X / LinkedIn thread of the week's new products,
  among your drafts.
* **Phase 183, newsletter:** the weekly Tech Pulse goes only to people who confirmed the free sample.
  It now ends with "New this week": up to 5 new datasets, links tagged `utm_source=newsletter`.
* **Phase 184, new-products feed:** `feeds/products.xml` lists the newest 50 products. The home page
  advertises it.

Draft strategies are now scored by their own plays only, even when two share a channel.
Automatic plays whose effect can't be measured, such as the blog, are never paused for lack of sales.

## Distribution (Phases 185-189)

* **Phase 185, embeddable badges:** `badges/<tech>.svg` ("Rust hiring: 24 companies") is rebuilt
  daily. The site's `embed/` page has copy-paste snippets for bloggers and communities. Every embed
  links back with `utm_source=embed`.
* **Phase 186, Hacker News answer drafts** (every 2 days):
  * The agent finds recent HN stories and comments about hiring for a technology it has data on,
    using the public Algolia API.
  * For each, it drafts a short reply with the real numbers and one link.
  * Nothing is posted automatically; you decide whether to post (HN dislikes promotion).
* **Phase 187, directories:** a checklist of 12 data marketplaces, product directories and
  awesome-lists, plus a ready-made blurb.
  * `automonetize marketing directories` shows them; `... directories done <name>` ticks one off.
  * Until two are done, the to-do list reminds you.
* **Phase 188, monthly data story:** a short press-style draft with the month's numbers, to pitch
  personally to journalists or bloggers who cover tech hiring. It's a draft only, never bulk-sent.
* **Phase 189, hiring heatmap:** `tools/hiring-heatmap/` is a free page of open roles per technology
  and region, updated daily, the kind of page people cite and link to.

## Marketing optimisation (Phases 190-194)

* **Phase 190, product funnel:** for each product over 30 days, checkouts started and kept orders.
  A product with 5+ checkouts and no sale goes on the to-do list and into the report: its price,
  description or preview is probably putting buyers off.
* **Phase 191, title tests:**
  * A product with enough traffic (10+ checkouts in 30 days) shows a benefit-led title for a week
    ("Rust Jobs in Europe: 24 Companies Hiring Now").
  * After that week, the title with more checkouts stays. The Stripe product name never changes.
  * Products with little traffic aren't tested, because the result would be noise.
* **Phase 192, paused strategies are visible:** the daily report names any strategy the engine is
  resting (8 plays without a checkout). It comes back by itself after 30 days.
* **Phase 193, return on your time:** for the drafts you post, revenue per minute you spent. The
  report names the best use of your time.
* **Phase 194:** `automonetize marketing report` shows it all in one place: funnel, title tests,
  strategy scores, return on time and paused strategies.

### Audit after Phase 194

This review covered:
* static analysis with security rules on all the new modules;
* a review of the new public surface: the catalog endpoint, sponsor lines, gift codes and the
  checkout answers of custom requests;
* both test suites (Python 3.11 and 3.13) and the simulator;
* a load test of the factory with 5,000 postings and 300 existing products.

At that size there are about 670 possible products, and choosing the next one takes about 1 second.

Fixed:
* Product ideas crashed on a fresh database, before the factory had made anything.
* The duplicate check re-read every existing product for each candidate, which would have slowed
  each tick to minutes at a few hundred products. It now reads each product once per tick.

## More job sources (Phases 195-199)

The factory can only make as many distinct products as the data allows, so the intake is wider now.
Four more public job feeds are on by default:
* **Phase 195:** Remotive (`remotive`), its public remote-jobs API.
* **Phase 196:** Jobicy (`jobicy`), its public API, with yearly salaries.
* **Phase 197:** Himalayas (`himalayas`), its public jobs API.
* **Phase 198:** We Work Remotely (`weworkremotely`), its programming-jobs RSS feed.
* **Phase 199, polite and credited:**
  * Each of these boards is fetched at most every 6 hours (Remotive asks for about 4 requests a
    day). A board waiting its turn isn't counted as a failure.
  * `robots.txt` is respected, as for every source.
  * The site's `sources/` page credits every board in use, with a link.

Remove a board from `lead_sources` to stop using it. An existing `automonetize.toml` that lists
`lead_sources` keeps its own list: add the new names there to use them.

## Control panel: products, marketing, money (Phases 200-204)

Three new tabs in `automonetize gui`:
* **Phase 200, Products:**
  * the factory's catalog (on sale, waiting, retired) and what it makes next;
  * a "Make a product now" button;
  * products ranked by revenue;
  * products with checkouts but no sales.
* **Phase 201, Marketing:**
  * drafts waiting for you, with the full text and tracked link, plus "I posted it" and "Skip"
    buttons;
  * the week's plan, what works, and the optimiser's notes.
* **Phase 202, Money:**
  * the month's forecast and the offers with their links;
  * affiliates and what you owe them, plus a form that approves an affiliate and emails them their
    links.
* **Phase 203, sponsor approval:** paid sponsorships wait in the Money tab. Enter their line and
  https link and approve; nothing shows on the site before that.
* **Phase 204, `AM DRAFTS <code>` email command:** replies with the drafts waiting to be posted, so
  you can post from your phone.

Every action needs the panel's sign-in and CSRF token. The panel only listens on this Mac.

## Buyer experience (Phases 205-209)

* **Phase 205, your library:** on the site's `library/` page, a buyer enters their email and
  everything they bought is re-sent to that address.
  * It uses the existing order-recovery endpoint, with its limits: 3 requests per network per hour,
    3 emails per address per day, and the same answer whether or not the address bought anything.
  * A person using the form now gets a readable page back; scripts still get JSON.
* **Phase 206, free sample:** every product page links `sample.csv`, the same 5 public rows as a
  file.
* **Phase 207, real ratings:** from 3 ratings by verified buyers (the 1-click ratings), a product
  page shows the counts and carries `AggregateRating` data. Below that nothing is shown, and nothing
  is ever made up.
* **Phase 208, request a dataset:** the site's `request/` page posts to `/v1/requests` (rate-limited).
  * Requests count towards what the factory builds next.
  * With "email me when it's ready" ticked, the address is used for one email when a matching
    product goes on sale, then forgotten (or after 90 days).
* **Phase 209, what's in the download:** factory product pages list the files in the zip.

## Catalog hygiene (Phases 210-214)

* **Phase 210, retired pages come down:** the site publisher deletes pages that are no longer built
  (a retired product, an empty hub).
  * Only files it published itself (in its manifest) are ever deleted, at most 25 per build.
  * If a build is less than half the size of what's published, nothing is deleted and you get an
    alert instead: a bug can't wipe the site.
* **Phase 211, Stripe stays tidy:** when the factory retires a product, its Stripe product is
  archived as well as its Payment Link closed.
* **Phase 212, prices follow demand:** at the weekly refresh, a product with 3 or more kept orders
  in 30 days moves up one price step ($5, $7, $9, $14, $19, $29). The new price gets a new checkout
  and the old link is closed. Prices never go down by themselves or past the top step.
* **Phase 213, disk stays tidy:** housekeeping deletes the files of products retired more than 30
  days ago that nobody bought (bought files stay, so order recovery can re-send them).
* **Phase 214, current numbers at checkout:** each refresh updates the Stripe product's description
  with the new row count.

## Ops at scale (Phases 215-219)

These run inside `ops_checks` and show in `automonetize doctor` (`agent/scale_checks.py`).

* **Phase 215, factory health:** the factory worker records every run. You get one alert a day if:
  * it hasn't run for 3 intervals (at least 30 minutes);
  * its last run failed;
  * it hasn't made a new product for 24 hours (with its reason, usually "waiting for more postings").
* **Phase 216, indexes:** the queries a large catalog runs most (orders by product, products by
  slug, factory products by status, recent task runs) use database indexes.
* **Phase 217, database growth:** the database size is recorded daily. Growth over 200 MB a day, or a
  database over 2 GB, gets one alert a day.
* **Phase 218, slow tasks:** a task that took over 5 minutes on each of its last 3 runs (a very
  large site build, say) gets one alert a day; it still runs.
* **Phase 219, scale test:** a test builds a catalog of 1,500 products with 4,500 orders and checks
  that the hot paths stay fast and indexed.

### Audit after Phase 219

This review covered:
* static analysis with security rules on the new modules;
* a review of the new public surface: the request form, the library form's page reply, and the
  removal of site pages;
* both test suites (Python 3.11 and 3.13) and the simulator;
* a load test: 150 products made back to back from 5,000 postings across 40 technologies (each
  under 1 second), then the site built from them (617 files, well under a second).

Fixed:
* A job board on the 6-hour cadence that failed was retried every cycle instead of waiting its turn.
* A malformed JSON post to `/v1/requests` answered with a server error; it now gets a 400.
* Automatic price rises (Phase 212) were missing from the Money tab's price history.

## Market trends (Phases 220-224)

From `strategies/market_trends.py`:

* **Phase 220, weekly counts:** new postings per technology per week for the last 8 weeks, by
  posting date. They are worked out at most every 6 hours.
* **Phase 221, rising and falling:** the last two weeks are compared with the two weeks before.
  * Rising: at least 10 recent postings and up 25% or more.
  * Cooling: down 25% or more.
* **Phase 222, trends page:** `trends/` on the site lists rising and cooling technologies, with an
  8-week table for the 20 busiest.
* **Phase 223, the factory follows demand:** within each product type, products about a rising
  technology are made first.
* **Phase 224, fastest-hiring companies** (`fast-hiring-<tech>`, $9): a sixth product type.
  * It lists companies with 3 or more new roles for a technology in the last 14 days, ranked by new
    roles.
  * It needs 8 such companies.
  * It takes its turn in the factory's rotation like the other types.

## Site discovery (Phases 225-229)

With hundreds of products, visitors need a way to find the right one (`strategies/site_discovery.py`).
The home page links each of these pages that exists.

* **Phase 225, search index:** `search.json` lists every product on sale: title, link, price,
  technology, type and rows.
* **Phase 226, search page:** `search/` filters the list as you type, in the browser. Nothing is
  sent anywhere. Without JavaScript it points to the catalog.
* **Phase 227, full catalog:** `catalog/` lists every product grouped by technology, 200 per page
  (`catalog/2/` and so on), so every product is one click from a crawlable page.
* **Phase 228, status page:** `status/` shows only counts and board names, nothing private:
  * when the data last changed;
  * postings seen in the last 7 days;
  * products on sale;
  * which job boards are answering.
* **Phase 229, what's new:** `changes/` lists, week by week for the last 8 weeks, products added
  and retired, and price changes. Retired products are named, not linked, since their pages are
  gone.

## Posting quality (Phases 230-234)

From `strategies/posting_quality.py`:

* **Phase 230, still listed:** postings the job boards stopped listing are left out of new products
  and refreshes, since they are most likely filled.
  * That means postings last seen 21 days before the newest one seen.
  * The cutoff is measured from the newest posting, not today, so an agent that was off for a
    month keeps its data.
* **Phase 231, one company, one name:** "Acme, Inc.", "ACME Inc" and "Acme GmbH" count as one
  company. Company lists show the most common spelling.
* **Phase 232, employers, not agencies:** staffing and recruitment agencies are left out of the
  company lists (top companies, remote-first employers, fastest-hiring). Their postings stay in
  posting-level datasets, where they are real openings.
* **Phase 233, where the rows come from:** each posting-level dataset's README names its job boards
  and each one's share of the rows.
* **Phase 234, how current it is:** the README also says how many postings were seen on their board
  in the last 7 days.

## Catalog insight (Phases 235-239)

What the factory is making, and what of it sells (`strategies/catalog_insight.py`).

* **Phase 235, by product type:** `automonetize factory --types` shows, for each type:
  * products on sale and retired;
  * orders and revenue in the last 90 days;
  * revenue per product.

  `--csv` writes the whole catalog to `data/exports/catalog.csv`.
* **Phase 236, by email:** `AM CATALOG <code>` replies with the same summary, the five best sellers
  and the next product.
* **Phase 237, catalog spreadsheet:** the control panel's Products tab has "Download the catalog
  (CSV)".
* **Phase 238, weekly line:** Monday's report says how many products were made, retired and
  repriced in the past week, and which type earns most per product.
* **Phase 239, choose the product types:** `factory_types` lists the types the factory makes
  (default: empty, meaning every type, including the ones added later).
  * Leave one out to stop making it.
  * What's already on sale stays on sale and keeps refreshing.

## Catalog reliability (Phases 240-244)

A catalog that runs for months without surprises (`strategies/catalog_reliability.py`).

* **Phase 240, current version after a price change:** if a price rise closes a checkout while
  someone is paying, their order still gets the current version of the dataset.
* **Phase 241, no dead ends for old links:** for 90 days after a product is retired, its address
  shows a short "no longer on sale" page.
  * Search engines are asked not to index it.
  * It links the same technology's current products and the catalog.
* **Phase 242, no pile-up without a checkout:** when 25 products are already waiting for a
  checkout, the factory stops making more until they go on sale. This happens when payments aren't
  set up yet or Stripe refuses. Doctor shows why.
* **Phase 243, catalog integrity:** once an hour the factory checks that every product on sale
  still has its download file.
  * A missing file (after a disk restore or a deleted folder) is rebuilt at once, behind the same
    checkout.
  * You get one alert a day while it happens.
* **Phase 244, rehearsed end to end:** `automonetize test-full-loop` now also has the factory make a
  product and checks it reaches the site, the catalog and search with a checkout.

### Audit after Phase 244

This review covered:
* static analysis with security rules on the new modules;
* a review of the new public pages: search, catalog, status, what's new, trends and the retired-product
  pages. They hold only counts, board names and product links; nothing private.
* both test suites (Python 3.11 and 3.13) and the simulator (now 14 steps);
* a load test: 150 products made back to back from 5,000 postings across 40 technologies (each under
  1 second), then the full site built (under 1 second) with no broken links.

Fixed:
* Trends and fastest-hiring counted postings without a date by the day the agent first saw them,
  so on a fresh install everything looked new. Only dated postings count now.
* Rising technologies outranked what customers asked for when choosing technology slices. Rising
  is now a score bonus, so requests and sales still count most.
* A product whose download was missing and couldn't be rebuilt (its postings gone) stayed on sale.
  It is now taken off sale an hour later, with an alert.
* The "What's new" page linked retired products, whose pages are gone. They are now named without
  a link.

## Role families (Phases 245-249)

New product types run on a small shared framework (`strategies/product_kinds.py`). Each type
registers there and the factory rotates, refreshes and retires it like the others. This batch adds
products by kind of job (`strategies/kinds_roles.py`):

* **Phase 245, role families:** a posting's title puts it in one or more families, matched on whole
  words only:
  * AI & Machine Learning, Data, DevOps & SRE, Security;
  * Mobile, Frontend, Backend, QA & Testing;
  * Engineering Management.
* **Phase 246, role datasets** (`role-<family>`): every current posting in a family, e.g. "Companies
  Hiring AI & Machine Learning Engineers".
* **Phase 247, by region** (`role-<family>-us|europe|remote`): the same, narrowed to a region when
  there are enough postings.
* **Phase 248, top employers by family** (`role-employers-<family>`, $9): one row per company,
  ranked by open roles (at least 10 companies).
* **Phase 249, family trends:** the trends page also shows new postings per role family, week by
  week.

## Safety and consistency (Phases 140-144)

* **Phase 140, email lint:** a last check before any email leaves.
  * Marketing email (follow-ups, releases, refresh and upgrade offers, win-back, sales, sample
    offers) must carry an unsubscribe instruction, your postal address and a `List-Unsubscribe`
    header, with no leftover template placeholders.
  * Every email needs a clean subject.
  * A failing email isn't sent: you get one alert for it, and the sending task retries.
* **Phase 141, more setting checks:** `doctor` now flags:
  * an unknown time zone;
  * hours outside 0-23 and weekdays outside 0-6;
  * rates written as percentages (`10` instead of `0.10`).
* **Phase 142, security headers everywhere:** every response from the public server carries
  `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`. A test probes every route.
* **Phase 143, every task documented:** the "Every task in a cycle" table below lists each task in
  run order, and a test keeps it complete.
* **Phase 144, `automonetize audit`:** every self-check in one report, exiting 1 if anything
  failed:
  * security;
  * settings and libraries;
  * tunnel routes;
  * the site audit;
  * the backup restore test;
  * resting tasks, muted alerts and held emails.

### Audit after Phase 144

This review covered:
* static analysis (pyflakes, and ruff with bugbear and bandit rules);
* a manual review of every new module;
* a check that error redaction never garbles a real alert message;
* a run of every new command on a fresh install;
* a secrets scan of the new commits;
* both test suites (Python 3.11 and 3.13).

Found and fixed:
* **`security.txt` could be given extra fields.** A contact address containing a line break would
  have added them; such an address is now refused.
* **The weekly backup restore test didn't quote table names.** It now quotes them safely before
  reading the backup.
* **Two problems the new consistency tests caught during this batch:**
  * eight public routes (the webhook and API among them) lacked `nosniff`;
  * five tasks were mentioned nowhere in the README.

## Every task in a cycle

Each cycle runs these in order (lower numbers first). `automonetize explain <task>` says more
and shows its last runs.

| Order | Task | What it does |
|---|---|---|
| 5 | `check_disk` | Free disk space; below the critical mark, dataset builds pause until there's room. |
| 6 | `ops_checks` | Job-source health, clock check, battery saver. |
| 10 | `aggregate_leads` | Collect job postings from the public boards and keep the ones matching the niche. |
| 12 | `discover_sources` | Find and trial new public job sources before they feed the data. |
| 15 | `build_intel` | Turn postings into company profiles: stack, open roles, hiring urgency. |
| 20 | `package_asset` | Build the next dataset version (zip with CSV, JSON, Excel, SQL and reports). |
| 21 | `run_factory` | Make new products from slices of all postings (catch-up when the factory worker isn't running). |
| 25 | `publish_listing` | Create or update the product and its Stripe payment link. |
| 29 | `tune_copy` | Learn which product-page wording sells (copy bandit). |
| 30 | `publish_showcase` | Publish a 5-record preview with the checkout link (GitHub repo or Gist). |
| 31 | `syndicate` | Post a data-driven article to Dev.to, Hashnode or GitHub Discussions. |
| 32 | `build_site` | Build and publish the website: product, intel, pricing, compare and legal pages. |
| 33 | `track_hn` | Publish a free monthly stack summary of HN's "Who is hiring?" thread as a Gist. |
| 34 | `publish_subscription` | Offer weekly updates as a subscription. |
| 34 | `publish_teasers` | Publish free 10-row teaser CSVs to your GitHub samples repo, linking to the products. |
| 35 | `stage_outreach` | Draft outreach emails for your review (never sent without your OK). |
| 36 | `publish_api_tier` | Offer the developer API as a subscription. |
| 37 | `publish_dossier_tier` | Offer per-company dossiers. |
| 38 | `publish_annual_plan` | Offer a yearly plan. |
| 39 | `publish_offers` | Create the custom-dataset, name-your-price, sponsorship, lifetime and gift-card offers. |
| 40 | `dispatch_outreach` | Send the outreach emails you approved, within limits. |
| 44 | `process_bounces` | Read bounces; pause marketing email if too many bounce. |
| 45 | `sync_revenue` | Record new sales from Stripe (and other storefronts). |
| 46 | `guard_payments` | Watch for card testing and duplicate charges. |
| 46 | `sync_refunds` | Record refunds and disputes. |
| 46 | `sync_subscriptions` | Keep subscription statuses in step with Stripe. |
| 47 | `run_dunning` | Remind subscribers whose payment failed. |
| 48 | `sync_finance` | Read the Stripe balance and payouts. |
| 49 | `answer_support` | Re-send files to buyers who say they didn't get them; pass everything else to you. |
| 49 | `watch_payouts` | Alert if a Stripe payout fails or is late. |
| 50 | `deliver_orders` | Deliver every paid order by email. |
| 51 | `deliver_subscriptions` | Send subscribers their weekly update. |
| 52 | `nurture_leads` | Send free-sample subscribers their weekly sample. |
| 53 | `follow_up_buyers` | One "did it arrive?" email per order, with 1-click ratings. |
| 53 | `serve_passes` | Weekly new-dataset email to lifetime-pass holders; end expired sponsorships. |
| 54 | `notify_requests` | Email people who requested a dataset once it's on sale (one email, then the address is forgotten). |
| 54 | `create_launch_promos` | Create a launch discount code for a new dataset. |
| 55 | `announce_releases` | Tell past buyers about a new version. |
| 55 | `collect_metrics` | Record views, impressions and purchases per niche. |
| 56 | `guard_freshness` | Stop promoting a dataset that has no new postings. |
| 57 | `publish_team_license` | Offer a team license. |
| 57 | `refresh_share_kit` | Write this week's ready-to-paste posts with tracked links. |
| 58 | `run_satellites` | Work the smaller niches alongside the main one. |
| 59 | `publish_bundle` | Offer all datasets as one bundle. |
| 60 | `optimize_pricing` | Test prices and keep the one that earns most. |
| 61 | `check_storefront` | Check every product can be bought and downloaded. |
| 62 | `check_webhook` | Check the Stripe webhook and tunnel answer; repair if possible. |
| 62 | `offer_refresh` | Offer past buyers the newer version at a discount. |
| 63 | `checkout_thank_you` | Send buyers to the thank-you page after paying. |
| 63 | `reward_referrals` | Send a free update to buyers whose link sold. |
| 64 | `win_back` | Invite cancelled subscribers back, once. |
| 65 | `offer_bundle_upgrade` | Offer buyers the bundle with what they paid counted. |
| 65 | `offer_related` | Email buyers up to 3 related datasets 5-12 days after an order (contact rules apply). |
| 66 | `offer_sample_upgrade` | Offer free-sample subscribers the full dataset. |
| 67 | `run_sale` | Run the quarterly sale. |
| 68 | `tune_offers` | Adjust discounts by what they sold. |
| 69 | `plan_marketing` | Plan the day's traffic plays: automatic ones run, drafts wait for you to post. |
| 70 | `optimize_marketing` | Product funnels (checkouts vs sales) and weekly title tests where traffic allows. |
| 92 | `housekeeping` | Tidy logs, old versions and the database; delete data nobody needs. |
| 93 | `audit_security` | Fix file permissions; report exposed secrets and risky settings. |
| 94 | `send_heartbeat` | Ping your heartbeat URL so you hear if the Mac stops. |
| 95 | `pace_goal` | Track progress against the daily goal. |
| 96 | `monthly_books` | Email last month's books (revenue, fees, expenses, profit). |
| 97 | `backup_data` | Daily backup; weekly restore test. |
| 98 | `report_owner` | Your emails: sales, alerts, milestones, the daily report. |
| 99 | `evolve_code` | Self-evolution (opt-in): propose, test and merge code improvements. |
| 100 | `self_update` | Install updates that pass their checks; roll back if not. |

## Autonomous code evolution (`agent/evolution/`, opt-in)

The agent can diagnose code-level bottlenecks in its own telemetry and patch its heuristics to
fix them, without a human, but only inside tight boundaries. **It is off until you switch it
on**: `ENABLE_AUTONOMOUS_CODE_EVOLUTION=true` in `.env`, or Settings → *Autonomous code evolution*
in `automonetize gui`. While it's off, the diagnosis still runs every cycle and is shown on the
dashboards, so you can see what it would change first (`automonetize evolution diagnose --diff`).

### What it diagnoses (`diagnostics.py`)

| Bottleneck | Evidence (from `data/agent_state.db`) | Patch |
|---|---|---|
| **Failing job-board parser** | A source produced 0 leads in 3 runs while its feed still returned objects. The aggregator records each run's payload *shape*: key names and value kinds, never values | Teaches the parser the renamed fields (e.g. `position` → `title`) in `FIELD_ALIASES`, `strategies/b2b_lead_aggregator.py`. An unreachable feed is reported, not patched: no code change fixes an outage |
| **Unrecognised technology tags** | Tags on ≥ 5 postings from ≥ 3 companies in 14 days, mostly next to known technologies, that don't match everyday prose. This screens out "marketing" or "go" | Adds them to `EVOLVED_FINGERPRINTS`, `strategies/tech_stack_intel.py`, so stacks, radars, the API and dossiers count them |
| **Revenue plateau** | 5 engine cycles in a row with no checkout while a product is on sale | One new factual headline in `seeds/copy_variants.json`. The copy bandit tests it against the control and retires it if it loses. At most one a week |

Each proposal is an *evolution hypothesis*: target file, full diff, the metric it should move
(e.g. "remoteok leads parsed per fetch: 0 → 97"), and the evidence.

### Safety boundaries (`evolver.py`)

* **Scope.** A patch may touch only `strategies/`, `tools/syndication/`, `tools/page_builder.py`
  and `seeds/*.json`. The following are never touched, and a patch touching them is rejected
  before anything runs:
  * `agent/supervisor.py`, `tools/storefront/webhook_listener.py` and `agent/recovery.py`;
  * every test (`tests/`, `test_*.py`, `conftest.py`);
  * `agent/evolution/` itself, so the agent can't loosen its own gate.
* **Data, not code.** Inside an allowed Python file, only an *evolvable block* can change: the
  lines between `# <evolved:NAME>` and `# </evolved:NAME>`. The block must stay one assignment of
  a pure literal (checked with `ast.literal_eval`), and everything outside it must be
  byte-identical. Imports, control flow and new modules can't be introduced; the code that uses
  the tables is written and reviewed by people. Seeds must be valid JSON, and the copy loader
  drops any headline that states a number the page doesn't have.
* **Your work comes first.** The agent never evolves when:
  * the checkout has uncommitted changes to tracked files, or HEAD is detached;
  * the patch was computed against an older version of the file;
  * the checkout moved while the checks ran.

  It never pushes; every change is a local commit.

### The shadow-branch workflow

1. `git worktree add -b auto/evolution-<timestamp>` in a temporary directory, at HEAD.
2. Apply the patch there.
3. Lint: syntax, pyflakes, and ruff when installed.
4. Run `pytest -v -W error` and `automonetize test-full-loop` inside the worktree, with secrets and
   `AUTOMONETIZE_*` overrides stripped from the environment. The suite runs against the built-in
   heuristics; a contract test and the simulator exercise the evolved values.
5. **Any failure** (lint, a test, the simulator, a timeout): the worktree and branch are deleted,
   the output and traceback are stored in `data/evolution_log.db`, the patch is blacklisted, and
   evolution cools down for 24 h.
6. **All green**: the patch is committed as `AutoMonetize Agent` with the subject
   `[Auto-Evolution] Improved parser heuristics for remoteok` and a body carrying the rationale,
   metric, attempt id and patch fingerprint. It is then fast-forward merged into the active branch.

At most one attempt per `evolution_interval_hours` (6), and `evolve_code` is the last task of a
cycle.

### Hot reload and automatic rollback (`hot_reload.py`)

After a merge, the supervisor reloads gracefully:

1. The engine finishes its cycle.
2. The webhook listener stops accepting and waits for in-flight fulfilment; the WAL is
   checkpointed.
3. The process re-executes itself (same PID, same arguments). The webhook's **listening socket is
   inherited** across the exec, so the port never closes: requests arriving during the switch
   queue in the kernel and are answered by the new code. The Cloudflare tunnel is a separate
   process and isn't touched. In a live test, 30 of 30 requests fired during a reload got `200`.

`kill -USR1 <pid>` triggers the same reload by hand. `SIGHUP`, `SIGTERM` and `SIGINT` still stop
gracefully.

The first hour after a merge is a **canary**. Rollback happens when any of these occurs:

* an engine cycle crash or a worker crash;
* a *new* operational failure (a task that was already failing before the merge doesn't count);
* any exception whose traceback runs through an evolved file.

The rollback runs `git revert` on the evolution commit (`[Auto-Evolution] Rollback: …`),
blacklists the patch, starts the 24 h cooldown and reloads. The canary passes once the hour is
over and at least one full cycle has run on the new code. If the revert can't be applied (you
edited the file meanwhile), evolution **halts itself** and raises an alert.
`automonetize evolution resume` clears the halt once you've sorted it out.

### Inspecting it

```bash
automonetize evolution                    # on/off, attempted vs merged, last commit, canary, cooldown
automonetize evolution log                # every attempt: status, title, commit, detail
automonetize evolution show 7 --output    # rationale, metric, full diff, captured lint/pytest/simulator output
automonetize evolution diagnose --diff    # what it would do right now (read-only)
git log --author="AutoMonetize Agent"     # the commits themselves; `git revert <sha>` works as usual
sqlite3 data/evolution_log.db "SELECT id, created_at, status, title, commit_sha FROM attempts ORDER BY id DESC"
```

`automonetize dashboard` and the GUI control panel show the same thing:
* attempts vs merges;
* the last evolution commit and its description;
* rollback health (the canary's state);
* the cooldown timer;
* the latest findings.

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
tail -f ~/Library/Logs/automonetize/*.log       # macOS; Linux: journalctl --user -u automonetize -f
```

### Set-and-forget on macOS

Three commands, once:

```bash
deploy/tunnel/setup_tunnel.sh hooks.example.com   # permanent public URL (Cloudflare Tunnel)
automonetize setup-autonomous --live               # validate, preflight, register, load, verify
sudo visudo -f /etc/sudoers.d/automonetize         # optional: let the agent wake the Mac (line below)
```

**1. Tunnel** (`deploy/tunnel/setup_tunnel.sh <hostname>`). Installs `cloudflared` with
Homebrew if it's missing, logs in once, creates (or reuses) a named tunnel, routes the DNS
record, and writes `~/.cloudflared/automonetize.yml`. The tunnel forwards only `^/webhook$` and
`^/healthz$` to `127.0.0.1:8443`; everything else gets a 404 at Cloudflare's edge. It saves
`PUBLIC_WEBHOOK_URL=https://<hostname>/webhook` to `.env` (mode 600) and `data/tunnel.json`,
and installs the companion launchd job `com.automonetize.tunnel` (`RunAtLoad` + `KeepAlive`).
The config parsing and `.env` editing are in `agent/tunnel.py`, so they're tested.

**2. `automonetize setup-autonomous`**. Every step is idempotent; re-run it after any change.

| Step | What it checks or does |
|---|---|
| Validate `.env` | Stripe key shape and mode (`--live` fails on a test key), `whsec_` secret, public URL (https, real hostname, path matches `webhook_path`), mailer credentials for the configured backend, `DRY_RUN`, `.env` permissions |
| Preflight | network, `GET /v1/balance` with the key, a real mailer login (SMTP `STARTTLS`+`AUTH`, SendGrid scopes or Postmark server), DB `quick_check` and write, free disk |
| Register | finds or creates the Stripe webhook endpoint for the public URL with exactly the events the listener handles. Stripe returns the signing secret only on creation, so it's written straight to `.env` and never printed. An existing endpoint that isn't ours is never touched |
| Load launchd | `deploy/install_launchd.sh`: agent + tunnel jobs, (re)started so they pick up the new secret. `--daemon` installs them as LaunchDaemons instead (start at boot, no login needed, runs as you; asks for sudo) |
| Handshake | POSTs a signed `automonetize.handshake` event to the **public** URL: Cloudflare → tunnel → listener → signature check → recorded under a random nonce. Retries for 20 s while things start. If no agent is serving the port (e.g. `--skip-launchd`), a temporary listener is started for the test |

Exit status 0 means the whole path works. `--json` gives a machine-readable report.

**3. Power** (`agent/power.py`). While a cycle runs or a webhook is being verified and
fulfilled, the agent holds an IOKit `PreventUserIdleSystemSleep` assertion
(`IOPMAssertionCreateWithName` via ctypes; falls back to `caffeinate -i -w <pid>`, which dies
with the process). Holds are reference-counted across the engine and webhook threads and
released as soon as both are idle, so the Mac sleeps normally between cycles. Waits between
cycles run on the wall clock, so a cycle that fell due during sleep runs within 30 s of
waking. To *wake* a sleeping Mac for the next cycle, the agent asks
`sudo -n pmset schedule wake`. That needs root, so it only works with this sudoers line
(setup-autonomous prints it with your username):

```
you ALL=(root) NOPASSWD: /usr/bin/pmset schedule wake *
```

**launchd jobs** (`deploy/com.automonetize.agent.plist`, `deploy/tunnel/com.automonetize.tunnel.plist`):
`RunAtLoad` and `KeepAlive` true, `ThrottleInterval` 30 s (15 s for the tunnel), logs under
`~/Library/Logs/automonetize/` (`agent.stdout.log`, `agent.stderr.log`, `tunnel.*.log`).
`WorkingDirectory` is the checkout, and `HOME`, `PATH` and `AUTOMONETIZE_ENV_FILE` are set in the
plist. Secrets are not: `deploy/run_agent.sh` sources `.env` at every start, with no terminal or
login shell involved, then `exec`s `automonetize supervise` so launchd's SIGTERM reaches the
supervisor directly (`ExitTimeOut` 90 s).

```bash
launchctl print gui/$(id -u)/com.automonetize.agent | head -30   # state, PID, last exit
launchctl kickstart -k gui/$(id -u)/com.automonetize.agent        # restart
deploy/install_launchd.sh uninstall    # stop and remove both jobs
```

**Honest limits.** These can't be engineered away:

* The tunnel needs a domain on your Cloudflare account (free plan is fine) and one browser
  login. `trycloudflare.com` quick tunnels change URL on every restart, so they're refused.
* A **LaunchAgent** (default) starts at login. With FileVault on, a Mac that reboots after a
  power cut waits at the unlock screen until someone logs in; `--daemon` (LaunchDaemons)
  starts at boot but still needs the disk unlocked once.
* A sleeping Mac receives nothing. Webhooks that arrive while it sleeps fail, Stripe retries
  them for up to 3 days, and the hourly `sync_revenue` poll records and delivers those orders
  on the next cycle. For instant delivery around the clock, keep it on power with
  *Prevent automatic sleeping when the display is off*, or add the sudoers line.
* Without the sudoers line, a cycle that falls due while the Mac sleeps runs when it next wakes.

### Self-healing (`agent/recovery.py`)

| Failure | Response |
|---|---|
| Syndication 5xx / 429 / timeout (Dev.to, Hashnode, Discussions, HN Algolia) | That platform gets an exponential cooldown (15 min doubling, capped at 24 h, `Retry-After` honoured, persisted across restarts). Other platforms and the loop carry on, and the post is retried after the cooldown. Permanent errors (401/403) start at the long end |
| SQLite `database is locked` / busy | Each statement already waits `busy_timeout` (5 s); after that it's retried up to 5 times with randomised exponential jitter (50 ms → 2 s). `BEGIN IMMEDIATE` and `COMMIT` too |
| Circuit breaker trips (5 consecutive systemic failures) | **Quarantine** instead of a permanent stop: an alert (error log, `data/ALERTS.log`, a macOS notification), then a 2 h cooldown (4 h, 8 h … up to 24 h if it trips again within a day). After the cooldown a self-diagnostic runs (DB integrity and write, disk space, network). Pass → breaker reset, a clean cycle runs, alert "resumed". Fail → another 2 h. Only offline → waits without adding hours |
| Manual stop (`automonetize stop`, `data/EMERGENCY_STOP`) | Never lifted automatically. `supervise` still starts (rather than exiting into a launchd restart loop) with the engine paused, so the webhook keeps fulfilling orders |

Set `quarantine_hours = 0` for the old behaviour (a trip waits for `automonetize resume`).

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
operational failures quarantine the engine (above) and the quarantine survives restarts
(`touch data/EMERGENCY_STOP` or `automonetize stop` to halt by hand, `automonetize resume` to clear).

## Configuration reference

Resolution order: defaults → `automonetize.toml` → `AUTOMONETIZE_<KEY>` environment variables
(lists comma-separated; dict/list-of-list keys as JSON). Secrets and `DRY_RUN` also accept
their conventional unprefixed names. Unknown keys are rejected.

| Key | Default | Meaning |
|---|---|---|
| `interval_seconds` | `3600` | Time between cycles |
| `signal_window_iterations` / `pivot_after_iterations` | `12` / `24` | Pivot windows (views+sales / revenue) |
| `min_hypothesis_days` / `stale_revenue_days` | `10` / `14` | Minimum niche age before a zero-traction pivot; "traction faded" window |
| `daily_target_cents` | `1000` | The $10.00/day goal |
| `max_actions_per_cycle` / `max_api_calls_per_cycle` / `max_consecutive_errors` | `100` / `200` / `5` | Circuit breakers (raised to the plan size + 10 when set lower) |
| `storefront_provider` | `auto` | `auto`, `stripe`, `lemonsqueezy` or `gumroad` |
| `price_tiers` | `[[0,900],[25,1400],[75,1900]]` | Starting one-off price by company count, clamped to $5-$19 |
| `price_matrix` / `pricing_min_views` / `pricing_window_hours` | `[900,1400,1900]` / `20` / `48` | One-off price experiments |
| `subscription_price_cents` / `subscription_interval` | `1000` / `month` | Recurring tier (0 disables) |
| `subscription_delivery_weekday` / `_hour` / `subscription_timezone` | `0` (Mon) / `8` / `UTC` | Weekly delta delivery |
| `hn_tracker_enabled` / `hn_gist_refresh_hours` / `og_images` | `true` / `24` / `true` | HN gist, PNG OG cards |
| `demand_sales_threshold` / `premium_price_cents` / `max_scrape_depth` | `3` / `1900` / `3` | Demand expansion |
| `stripe_webhook_secret` / `webhook_host` / `webhook_port` / `webhook_path` | env / `127.0.0.1` / `8443` / `/webhook` | Webhook listener |
| `network_check_hosts` | Stripe + GitHub API | Offline probe (`[]` disables) |
| `lead_magnet_enabled` / `lead_magnet_double_opt_in` / `lead_magnet_sample_size` | `true` / `true` / `10` | Free sample and confirm-before-nurture |
| `lead_magnet_max_per_hour` / `lead_magnet_max_per_ip_hour` | `60` / `5` | Capture abuse brakes |
| `lead_nurture_weekday` / `lead_nurture_hour` / `lead_nurture_signals` | `0` (Mon) / `9` / `3` | Weekly Tech Pulse (in `subscription_timezone`) |
| `seo_matrix_enabled` / `seo_min_companies` / `seo_min_migrations` / `seo_max_pages` | `true` / `5` / `3` / `60` | Search-intent pages |
| `indexnow_enabled` / `indexnow_key` | `true` / generated | IndexNow submission of changed pages |
| `gui_host` / `gui_port` | `127.0.0.1` / `8080` | Control panel (loopback only) |
| `api_enabled` / `api_price_cents` / `api_daily_quota` / `api_degraded_quota` | `true` / `2900` / `500` / `50` | Developer API tier |
| `api_burst` / `api_rate_per_second` / `api_max_page_size` / `api_auth_failures_per_ip_hour` | `10` / `2.0` / `100` / `30` | API throttling |
| `source_discovery_enabled` / `source_discovery_interval_hours` / `source_trial_days` | `true` / `24` / `3` | Source discovery |
| `source_min_valid_ratio` / `source_max_error_rate` / `source_max_active` / `source_probes_per_run` / `source_seed_feeds` | `0.6` / `0.2` / `20` / `5` / `[]` | Source trial rules |
| `copy_bandit_enabled` / `copy_bandit_algorithm` / `copy_bandit_epsilon` / `copy_bandit_min_views` / `copy_bandit_deprecate_sd` | `true` / `epsilon_greedy` / `0.2` / `200` / `2.0` | Copy bandit |
| `max_active_niches` / `satellite_window_days` / `satellite_min_share` / `satellite_max_days_without_revenue` | `3` / `14` / `0.15` / `21` | Satellite niches |
| `quarantine_hours` / `quarantine_max_hours` / `alert_notifications` | `2` / `24` / `true` | Self-healing cooldown after a breaker trip (`0` = stop for a human) |
| `public_webhook_url` (`PUBLIC_WEBHOOK_URL`) | empty | Set by `setup_tunnel.sh`; used by `setup-autonomous` |
| `power_assertions` / `schedule_wake` | `true` / `true` | Stay awake while working; `pmset` wake for the next cycle (needs the sudoers line) |
| `github_pages_branch` / `github_pages_dir` / `site_title` | `""` / `docs` / `Tech Stack Intel` | Site publishing |
| `syndication_publish` / `syndication_interval_days` / `syndication_min_companies` | `true` / `5` (floor) / `10` | Syndication |
| `allow_manual_fulfillment` | `false` | Sell even when the agent can't email the file |
| `intel_max_url_checks` / `high_urgency_threshold` | `15` / `60` | Careers URL checks per cycle; "hot" cutoff |
| `github_showcase_repo` / `github_showcase_mode` / `github_pages_repo` / `pages_base_url` | empty / `repo` | Publishing targets |
| `dunning_grace_days` / `dunning_reminder_days` | `7` / `[0,3,6]` | Grace period before API keys are suspended; reminder schedule |
| `recovery_per_ip_hour` / `recovery_per_email_day` | `3` / `3` | Order-recovery limits |
| `dossier_price_cents` / `dossier_min_score` / `dossier_pulse_min_intent` | `4900` / `75` / `80` | Dossier price (0 disables), eligibility, Pulse button cutoff |
| `enable_autonomous_code_evolution` (`ENABLE_AUTONOMOUS_CODE_EVOLUTION`) | `false` | Master switch for self-modification |
| `evolution_interval_hours` / `evolution_cooldown_hours` / `evolution_canary_minutes` | `6` / `24` / `60` | Attempt spacing, cooldown after a failure or rollback, post-merge watch window |
| `evolution_check_timeout_seconds` / `evolution_run_simulator` / `evolution_repo` | `1200` / `true` / this checkout | Worktree checks and the repository to evolve |
| `evolution_min_tag_postings` / `evolution_plateau_cycles` | `5` / `5` | Diagnosis thresholds |
| `owner_reports` / `owner_email` (`OWNER_EMAIL`) / `owner_digest_hour` | `true` / sender email / `8` | Sale emails, alerts, daily report |
| `bundle_min_niches` / `bundle_discount` | `2` / `0.4` | All-datasets bundle |
| `backups_enabled` / `backup_dir` / `backup_keep_days` | `true` / Application Support / `14` | Daily backups |
| `buyer_followup` / `buyer_followup_days` | `true` / `3` | One check-in email per order after delivery |
| `bookkeeping` | `true` | Monthly revenue CSV emailed to `owner_email` |
| `storefront_check_hours` | `6` | How often checkout links, product pages and downloads are checked |
| `release_announcements` / `announce_min_gap_days` | `true` / `14` | New-niche emails to past buyers |
| `stale_after_days` | `7` | Days without new postings before a dataset stops being promoted |
| `auto_update` / `auto_update_hours` | `true` / `6` | Install verified updates of the tracked branch by itself |
| `referrals` | `true` | Personal referral links in follow-ups; referrers get the newest version free |
| `winback` / `winback_after_days` / `winback_discount_pct` | `true` / `7` / `50` | One discounted invitation back after a cancellation |
| `bundle_upgrade` / `bundle_upgrade_after_days` | `true` / `10` | Bundle offer with what the buyer paid counted |
| `sample_offer` / `sample_offer_after_days` / `sample_offer_pct` | `true` / `14` / `25` | One discount for free-sample signups |
| `seasonal_sale` / `sale_pct` / `sale_days` / `sale_every_days` / `sale_min_store_age_days` | `true` / `25` / `3` / `90` / `30` | Quarterly store-wide sale |
| `promo_daily_cap` | `150` | Marketing emails per day in total |
| `bounce_pause_rate` | `0.05` | Pause marketing email for a week above this hard-bounce rate |
| `offer_tuning` | `true` | Adjust offer discounts from measured sales |
| `tax_set_aside_pct` / `payout_watch_days` | `25` / `30` | Books' tax set-aside; days before idle Stripe money alerts |
| `refund_policy_days` | `14` | Stated on the refunds page and in the FAQ |
| `google_site_verification` / `bing_site_verification` | empty | Search Console / Bing verification codes |
| `NTFY_TOPIC` (`ntfy_topic`) / `ntfy_server` | empty / ntfy.sh | Phone notifications (`automonetize phone`) |
| `sale_alerts` / `owner_digest` | `each` / `daily` | Sale emails: each, daily or off; digest: daily, weekly or off |
| `owner_commands` | `true` | Email commands from your owner address with the command code |
| `team_license` / `team_license_multiplier` / `team_license_seats` | `true` / `3.0` / `10` | Team license per dataset |
| `annual_plan` / `annual_months_paid` | `true` / `10` | Yearly subscription option |
| `checkout_thank_you` | `true` | Redirect buyers to the site's thank-you page after paying |
| `blocked_signup_domains` | `[]` | Extra disposable domains to refuse at the sample form |
| `download_link_days` / `download_link_uses` | `7` / `5` | Private links for files too big to attach |
| `webhook_check_hours` / `card_testing_threshold` | `6` / `10` | Webhook check interval; failed charges per hour that alert |
| `log_keep_days` / `keep_versions` | `90` / `3` | Housekeeping: log retention, dataset versions kept (sold ones always kept) |
| `disk_warn_gb` / `disk_critical_gb` | `2` / `0.5` | Disk guard thresholds |
| `HEALTHCHECK_URL` (`heartbeat_url`) | empty | Ping URL of an outside check that emails you if the agent stops |
| `release_gate_max_drop` / `release_gate_hold_days` | `0.5` / `3` | Hold a new version that lost rows |
| `refresh_offers` / `refresh_after_days` / `refresh_min_new_rows` / `refresh_discount_pct` | `true` / `30` / `25` / `50` | Discounted update offers to past buyers |
| `launch_promos` / `launch_discount_pct` / `launch_promo_days` | `true` / `20` / `7` | Launch code for each new niche |
| `imap_host` / `imap_username` / `IMAP_PASSWORD` | from the SMTP login for Gmail, Fastmail, Outlook, iCloud | Support inbox (read-only) |
| `dry_run` | `true` | Master switch for all email |
| `warmup_start_per_day` / `warmup_step_per_week` / `dispatch_max_per_day` | `5` / `5` / `30` | Cold email warm-up |
| `blocked_recipient_tlds` | EU/EEA/UK/CH | Recipients never emailed |
| `lead_sources`, `niches`, `min_leads_for_asset` | see `automonetize init` | Data collection |
| `sender_*`, `outreach_*` | see `automonetize init` | Draft personalization and guardrails |

## CLI

```
automonetize go-live [--link]                       # switch to real payments (checks first); list live links
automonetize connect-marketing                      # public GitHub Pages site + Dev.to articles
automonetize doctor [--fix]                         # plain-words health check; safe fixes
automonetize autostart                              # macOS: start at login, restart if stopped
automonetize backup [list] | restore NAME [--yes]   # daily backups happen by themselves
automonetize share                                   # this week's ready-to-paste posts with tracked links
automonetize books [YYYY-MM|YYYY]                    # books for a month (default: last month) or a year
automonetize test-full-loop [--keep] [--no-curl] [--json] [--no-color]   # sandboxed end-to-end rehearsal
automonetize evolution [status|log|show ID [--output]|diagnose [--diff]|resume]   # self-evolution audit
automonetize gui [--port P] [--no-browser]          # local control panel on 127.0.0.1
automonetize api keys | usage [--days N] | reissue SUBSCRIBER_ID | revoke KEY_ID
automonetize pause [--reason R] | resume            # skip cycles; webhook and lead capture stay up
automonetize setup-autonomous [--live] [--daemon] [--json] [--skip-register|--skip-launchd|--skip-handshake]
automonetize supervise [--no-webhook] [--headless]   # daemon: engine + webhook listener
automonetize webhook [--port P] [--selftest]         # listener only
automonetize run [--once|--cycles N] [--interval S] [--headless]
automonetize site | syndicate [--drafts] | pricing status|run
automonetize analytics [--period=30d] [--json]       # revenue by channel/niche/tier, MRR, churn, net/day
automonetize subscriptions list [--status S] | deliver
automonetize dashboard [--watch] | status [--json]
automonetize hypotheses                   # every niche idea tried and how it went
automonetize timings [--days N]           # how long each task takes
automonetize deps                         # are the installed libraries new enough?
automonetize config-docs [--markdown]     # every setting, its default and what it does
automonetize quiet on [--days N] | off    # hold marketing email (purchases always go out)
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
pytest     # 865 tests, ~40 s, no network
```

See [AUDIT.md](AUDIT.md) for the operational audit and its 16 regression-tested fixes.

```bash
pytest -W error              # the audit's strict mode; also clean
```

Phases 16 to 19 add 18 tests in `tests/test_business_ops.py`:
* **Balance:** balance and payouts read, described, and errors reported.
* **Bundle:** needs two niches; zip contents and price; refreshed in place; replaced with the old
  link deactivated; delivered like any dataset.
* **Support desk:** the IMAP login follows SMTP; classification reads only the customer's words;
  resend and escalation, each once; the inbox scan is read-only and never downloads a stranger's
  mail.
* **Backups:** backup, prune and restore (including into an open database); the daily task; the
  restore command (confirmation, pre-restore backup, stop/start).
* **Action cap:** never below the plan size.

Phases 12 to 15 add 10 tests in `tests/test_autopilot.py`, plus 16 for `go-live` and
`connect-marketing`:
* **Catalog:** the catalog lists one row per live link.
* **Catch-up:** catch-up publishing of satellite datasets.
* **Owner reports:** sale and alert emails once each, with no history flood; one digest a day
  after the hour; failed sends retried; reports can be switched off.
* **Doctor:** findings and safe fixes; never pulls over local changes; autostart hands the
  process over to launchd.
* **Outreach review:** best first, CSRF-protected, decisions never flipped, bad input refused.

Phase 11 adds 55 tests in `tests/test_evolution.py`:

* **Boundaries:**
  * every immutable-core, test and out-of-scope path is refused;
  * a forbidden patch is rejected before any git or test command runs;
  * the literal-only rule for evolvable blocks (code, imports, renames and edits outside a block
    are all rejected).
* **The worktree workflow on a real git repo:**
  * a passing patch is fast-forwarded as an agent commit and the worktree and branch are removed;
  * a failing test, a timeout, a dirty checkout, a stale patch or a checkout that moves mid-check
    each leave `main` untouched, with the outcome logged, blacklisted and cooled down as
    appropriate.
* **The canary:**
  * rollback on an exception in evolved code, an engine crash or a new operational failure (but
    not an old one);
  * a pass after the window and a clean cycle;
  * self-halt when the revert can't apply, and `resume`.
* **Hot reload:**
  * socket inheritance across exec;
  * a supervisor that drains and re-execs while a request queued on the port is answered by the
    "new process";
  * SIGUSR1 reloads while SIGHUP stops.
* **Diagnostics:**
  * a renamed feed field → the parser alias that fixes it;
  * outages without a patch;
  * technology tags learned while everyday words aren't;
  * a plateau → one factual headline, and the copy loader dropping unsafe arms.
* **The task gates:** off by default, canary, interval, no repeats.
* **The dashboard, GUI and CLI telemetry.**
* **A contract test on whatever the agent has learned so far.**

An autouse fixture in `tests/conftest.py` runs every other test against the built-in heuristics, so
learned data never changes what existing assertions mean.

Phases 8 to 10 add 31 tests:

* `tests/test_retention.py` covers:
  * the dunning case, the degraded key and the portal-link email;
  * no duplicate reminders on Stripe retries, and reminders on days 0, 3 and 6;
  * grace expiry → suspended → `402`, with the suspension surviving a status sync;
  * recovery by `invoice.paid` or by the polling sweep, and the past-due polling path;
  * churn events for voluntary and involuntary cancellation, logged once;
  * the portal fallback.
* `tests/test_recovery.py` covers:
  * validation;
  * identical responses for strangers;
  * 3 per IP per hour, then `429` with `Retry-After`;
  * the per-address daily cap and attachments sent to the buyer only;
  * the public HTTP route;
  * the API-key confirm link (GET changes nothing, POST reissues once, expiry after 24 h,
    stored hashed).
* `tests/test_dossier.py` covers:
  * eligibility, every section, the department growth windows and no personal data;
  * PDF structure: xref offsets, page count, escaping, pagination and "Page n of N";
  * the one-off $49 price and the tunnel/Stripe gating;
  * checkout 303 and its metadata;
  * webhook → delivered PDF;
  * Stripe outage → 503;
  * the API upsell fields;
  * the Pulse button above intent 80.
* `tests/test_test_loop.py` runs the full simulator with curl and with urllib, in text, JSON and
  colour, checks that it finishes well under 60 s, and checks that the CLI command is wired.

Phases 6 and 7 add 69 tests. `tests/test_api.py`: key randomness and hash-only storage, auth
errors and the failed-auth brake, filters and cross-niche merging, OpenAPI conformance of every
response shape (validated against the published schema), cursor pagination (complete, no
duplicates, bound to filters, expiring on refresh), burst limit then daily quota (surviving a
restart, resetting at midnight UTC), rotation, routing/docs/OpenAPI, and the Stripe lifecycle
(checkout provisioning and email, idempotency, payment failure degrading, recovery, cancellation
revoking, the polling sweep, dry-run reissue, the $29 product). `tests/test_bandit.py`: 80/20
allocation, posterior winner and priors, Thompson sampling, the deprecation z-test, simulated
convergence and revenue lift for both algorithms, versioning, telemetry validation and dedupe,
purchase and signup attribution, honest-copy gating, and the lander script itself, run in Node
against a stub DOM. `tests/test_source_discovery.py`: the SSRF guard, ATS and RSS discovery, every
adapter (including RFC 2822 dates and refused DOCTYPEs), sandbox probes (robots, 429, size,
structure), the three-day lifecycle, live ingestion without a restart, rejection, suspension and
the active cap. `tests/test_satellites.py`: revenue shares with a floor, quota and deficit maths,
satellite start and refresh, revenue-driven refresh frequency, retirement and replacement, budget
exhaustion, and syndication and SEO capacity following the shares.

Phase 5 adds 91 tests. `tests/test_gui.py` covers loopback-only binding, sign-in (token, one-time
launch links, brute-force throttle), DNS-rebinding, CSRF and cross-origin refusals, the CSP,
secrets never sent to the browser, every validation rule including `.env` injection, in-place
`.env` updates that keep comments, secret keep/clear, override warnings, preflight results,
control dispatch (kill needs confirmation), status and redacted logs. The service controller's
start/pause/resume/kill/restart are tested against fake launchctl, process and signal hooks.
`tests/test_intent.py` covers each signal family, false positives, tech resolution, score
ordering, tag format, and the radar/CSV surfacing. `tests/test_seo_matrix.py` covers page
thresholds, slugs, velocity stats, internal links, Dataset JSON-LD (and `</script>` breakout),
no contact or quoted text on public pages, sitemap validity, the key file and forms,
IndexNow's changed-only submission, and a publish that resumes across cycles when the API
budget runs out. `tests/test_lead_magnet.py` covers validation and abuse brakes, free rows kept
out of MRR, live and dry-run sample emails (CSV, cheatsheet, unsubscribe headers,
attribution), confirm/unsubscribe, prefetch-safe links, the Monday pulse (confirmed-only, once
a week, payers skipped, blocked without a postal address), and the public HTTP endpoints.

Set-and-forget (`tests/test_autonomy.py`, 56 tests) adds: backoff maths and transient-error
classification, persisted per-platform cooldowns with `Retry-After`, SQLite lock retries against
a real competing writer, quarantine entry, escalation, cap, failed-diagnostic extension,
offline-only waiting and clean-cycle recovery (manual stops never auto-lifted); power holds
shared by the engine cycle and the aiohttp webhook, reference counting across threads, the IOKit
calls through a fake ctypes library, `caffeinate -w`, non-Darwin no-ops, `sudo -n pmset`
wake scheduling, and wall-clock waits across a simulated 3-hour sleep; cloudflared config
render/parse round trips, restricted ingress, hostname validation, `tunnel create` / `tunnel
list` parsing, `.env` updates read back by bash; both launchd jobs rendered and parsed
(LaunchAgent and LaunchDaemon); and setup-autonomous validation, preflight, endpoint
creation and reuse, secret handling, and the handshake through a simulated tunnel (including
a tunnel that's still connecting and a secret mismatch).

Phase 4 adds: Dev.to/Hashnode payloads, mocked responses and duplicate-title refusal, per-channel
UTM and `client_reference_id` link rendering, the 5-day cadence floor, the HN tracker (thread
discovery, sanitized gist, refresh window, RSS item); Stripe recurring Price and Payment Link
payloads, subscription activation, `invoice.paid` revenue (both invoice API shapes, idempotent),
status changes and cancellation, polling reconciliation, delta packaging, Monday delivery
(timezone, once per week, past-due exclusion, catch-up, dry-run → live); UTM parsing,
reference encoding within Stripe's limits, channel/niche/tier breakdowns, checkout conversion,
MRR, churn and goal maths, the analytics CLI; SVG badge and OG card validity, PNG cards,
JSON-LD with subscription offers, real-data social proof, and binary asset publishing to `docs/`.

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
