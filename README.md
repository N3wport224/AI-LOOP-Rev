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
5. **distributes** it inbound, with no approval needed: value-first "State of…" breakdowns
   syndicated to Dev.to, Hashnode, GitHub Discussions and RSS (at most one post per platform
   every 5 days), plus a free monthly Hacker News "Who is hiring?" stack gist. Every link carries
   UTM tags. (Cold outreach still exists, but only sends drafts a human approved.)
6. **sells in real time**: a signature-verified Stripe webhook records verified revenue and
   emails the buyer their zip plus a receipt seconds after payment; polling reconciles anything missed;
7. **sells recurring**: a $10/month subscription next to the one-off $9/$14/$19 price; Monday
   morning delta packages go to every active subscriber, and each paid invoice is verified revenue;
8. **optimizes price**: experiments across the one-off tiers per dataset, steps down or bundles
   2-for-1 when traffic doesn't convert, steps up and converges when it does, and on strong
   demand scrapes deeper and ships a $19 premium deep-dive add-on;
9. **measures** revenue by niche, tier and acquisition channel, checkout conversion per channel,
   MRR, churn and net revenue per day against the $10 goal (`automonetize analytics`);
10. **scores** each hypothesis on its funnel and pivots to an adjacent, higher-demand stack
   cluster when it isn't converting.

It runs as a supervised daemon (engine + webhook listener) under launchd or systemd,
survives crashes, reboots, sleep and network drops, and shuts down cleanly on SIGTERM.
Everything day-to-day (keys, start/pause/kill, revenue, logs) is also available in a local
web control panel: `automonetize gui` (see [Control panel](#control-panel-automonetize-gui)).

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
pytest                     # 455 tests, ~21 s, no network
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
uptime, and free leads (confirmed / pending), refreshed every few seconds. Actions:

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
| `max_actions_per_cycle` / `max_api_calls_per_cycle` / `max_consecutive_errors` | `30` / `60` / `5` | Circuit breakers (the action cap must exceed the 22-task plan) |
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
| `quarantine_hours` / `quarantine_max_hours` / `alert_notifications` | `2` / `24` / `true` | Self-healing cooldown after a breaker trip (`0` = stop for a human) |
| `public_webhook_url` (`PUBLIC_WEBHOOK_URL`) | empty | Set by `setup_tunnel.sh`; used by `setup-autonomous` |
| `power_assertions` / `schedule_wake` | `true` / `true` | Stay awake while working; `pmset` wake for the next cycle (needs the sudoers line) |
| `github_pages_branch` / `github_pages_dir` / `site_title` | `""` / `docs` / `Tech Stack Intel` | Site publishing |
| `syndication_publish` / `syndication_interval_days` / `syndication_min_companies` | `true` / `5` (floor) / `10` | Syndication |
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
automonetize gui [--port P] [--no-browser]          # local control panel on 127.0.0.1
automonetize pause [--reason R] | resume            # skip cycles; webhook and lead capture stay up
automonetize setup-autonomous [--live] [--daemon] [--json] [--skip-register|--skip-launchd|--skip-handshake]
automonetize supervise [--no-webhook] [--headless]   # daemon: engine + webhook listener
automonetize webhook [--port P] [--selftest]         # listener only
automonetize run [--once|--cycles N] [--interval S] [--headless]
automonetize site | syndicate [--drafts] | pricing status|run
automonetize analytics [--period=30d] [--json]       # revenue by channel/niche/tier, MRR, churn, net/day
automonetize subscriptions list [--status S] | deliver
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
pytest     # 455 tests, ~21 s, no network
```

See [AUDIT.md](AUDIT.md) for the operational audit and its 16 regression-tested fixes.

```bash
pytest -W error              # the audit's strict mode; also clean
```

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
