# AutoMonetize

An autonomous, goal-directed agent loop that works toward **$10.00/day of verified net
revenue** with zero startup capital. It builds a small, honest business: it pulls public
job-board APIs, packages the data into curated niche directories you can sell, and drafts
personalized outreach that **you** review and send. It keeps testing hypotheses, measures
them against verified sales, and moves to a new niche when one isn't working.

> **Reality check.** Nothing here guarantees income. The agent does the repetitive work
> (collecting, cleaning, packaging, drafting, tracking). Someone still has to list the
> product, approve the messages and decide what's worth selling. Only revenue confirmed by a
> payment provider API, or entered by you with `--verified`, counts toward the target.

## How it works

```
                ┌──────────────────────── one cycle (every interval_seconds) ────────────────────────┐
 guard ──► observe ──► reflect ──────────────► plan ──────────► act (≤ max_actions_per_cycle) ──► persist
 (stop?)   (active     (N iterations with      (queue the       aggregate_leads → package_asset →
            hypothesis, zero verified revenue?   pipeline if     stage_outreach → sync_revenue
            revenue)    → deprecate & pivot)     backlog empty)  each task: 3 attempts, params adjusted
```

* **Hypothesis**: "A curated hiring directory for niche *X* sells at least one copy a day."
  Hypotheses are formulated in this order: configured niches, then niches mined from the
  most frequent tags in the leads collected so far, then revisits of deprecated niches with
  broader keywords (up to `max_hypothesis_generations`).
* **Pivoting** happens when a hypothesis reaches `pivot_after_iterations` cycles with zero
  verified revenue attributed to it, or right away when a task disproves it (for example,
  the sources answered but the niche still has fewer than `min_leads_for_asset` leads after
  a grace period).
* **State** lives in `data/agent_state.db` (SQLite, WAL mode). Tables: `hypotheses`,
  `actions`, `errors` (with full tracebacks), `revenue`, `backlog`, `leads`, `assets`,
  `outreach_queue`, `kv`.

### Layout

| Path | Purpose |
|---|---|
| `agent/engine.py` | The loop: guard, reflect, pivot, plan, act; retries; emergency stop; process lock |
| `agent/hypotheses.py` | Hypothesis formulation and pivot targets |
| `agent/state.py` | SQLite state store |
| `agent/config.py` | Config: defaults < `automonetize.toml` < environment variables |
| `strategies/b2b_lead_aggregator.py` | Fetch → validate → enrich → dedupe → CSV/JSON exports |
| `strategies/digital_asset_packager.py` | Versioned zip bundle, listing draft, GitHub Pages showcase, Gumroad product linking |
| `strategies/outreach_stager.py` | Personalized drafts + submission packages → `pending_review` queue (never sent) |
| `tools/shell_runner.py` | Allowlisted commands, no shell, cwd/argument confinement, directory lock, timeouts |
| `tools/file_io.py` | File I/O confined to `data/`, atomic writes, size limits |
| `tools/http_client.py` | Per-host rate limiting, robots.txt, rotating user agents, self-correcting retries |
| `tools/revenue_tracker.py` | Ledger, Gumroad sales sync, fee estimation, daily target |
| `tools/circuit_breaker.py` | Action/API budgets per cycle, consecutive-error emergency stop, token bucket |
| `tools/recovery.py` | Intercept → log traceback → adjust params → retry (3×) → `OperationalFailure` |
| `dashboard/` | Rich terminal dashboard and the `automonetize` CLI |

## Installation

Requires Python 3.11+.

```bash
git clone <this repo> AutoMonetize && cd AutoMonetize
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'          # or: pip install -r requirements.txt
automonetize init                # writes automonetize.toml and creates data/agent_state.db
pytest                           # full test suite, no network needed
```

`python cli.py <command>` works as well as `automonetize <command>`.

## Configuration

Settings are resolved as **defaults → `automonetize.toml` → environment variables**. Every
key can be set via `AUTOMONETIZE_<KEY>` (lists are comma-separated; `niches` takes JSON).
Unknown keys are rejected.

| Key | Default | Meaning |
|---|---|---|
| `interval_seconds` | `3600` | Time between cycles |
| `pivot_after_iterations` | `24` | Cycles with zero verified revenue before a hypothesis is deprecated |
| `daily_target_cents` | `1000` | The $10.00/day goal |
| `max_actions_per_cycle` | `8` | Circuit breaker: tasks per cycle |
| `max_api_calls_per_cycle` | `60` | Circuit breaker: outbound HTTP calls per cycle |
| `max_consecutive_errors` | `5` | Operational failures in a row that trigger the emergency stop |
| `task_retry_attempts` | `3` | Attempts per task before an operational failure is recorded |
| `http_rate_per_minute` | `20` | Per-host token bucket |
| `respect_robots_txt` | `true` | Honour robots.txt (documented public API endpoints skip this check) |
| `lead_sources` | `remoteok, arbeitnow, hn_hiring` | Which fetchers to use |
| `niches` | 5 developer niches | `[{name, keywords}]` explored in order |
| `min_leads_for_asset` | `10` | Minimum dataset size before packaging |
| `asset_price_cents` | `900` | Suggested listing price |
| `storefront_url` | `""` | Product URL shown on the showcase page and submission drafts |
| `sender_name`, `sender_email`, `sender_skills` | empty | Used to personalize outreach drafts |
| `outreach_offer` | `short-term contract help` | What the pitch offers |
| `outreach_daily_cap` / `outreach_cooldown_days` / `outreach_min_score` | `20` / `30` / `0.6` | Outreach guardrails |
| `platform_fee_pct` / `platform_fee_fixed_cents` | `10` / `50` | Fee estimate used to turn gross sales into net |
| `shell_allowlist` | `git, python3, ls, cat, echo, zip` | Commands the shell tool may run |

### API keys

* **`GUMROAD_ACCESS_TOKEN`** (optional, recommended): create an application in Gumroad →
  Settings → Advanced → Applications and generate an access token. With it, the agent reads
  your sales (`GET /v2/sales`) and products (`GET /v2/products`) to verify revenue and link
  products to hypotheses. Keep it in the environment or in `.env` (see `.env.example`), not in
  the TOML file you might commit.
* The lead sources need no keys.

## Running

```bash
automonetize run                      # perpetual loop; prints the dashboard after each cycle
automonetize run --once               # a single cycle (for cron)
automonetize run --cycles 5 --interval 60
automonetize dashboard --watch        # live dashboard in another terminal
automonetize status [--json]
```

### Headless / background mode

```bash
# simplest
nohup automonetize run --headless > data/nohup.out 2>&1 &
tail -f data/agent.log

# systemd (restarts on failure, clean SIGTERM shutdown)
cp deploy/automonetize.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now automonetize

# or cron, one cycle per hour
crontab -e    # see deploy/crontab.example
```

A file lock (`data/agent.lock`) stops two loops from sharing one database; a second
`run` exits with code 2. `SIGTERM`/`SIGINT` end the loop after the current cycle.

### Emergency stop

The loop halts (and refuses to restart) when any of these holds:

* `max_consecutive_errors` operational failures happen in a row (the circuit breaker trips);
* `data/EMERGENCY_STOP` exists (`touch` it from anywhere);
* you ran `automonetize stop --reason "..."`.

The stop survives restarts. Clear it with `automonetize resume`.

## Your part of the loop

1. **List the product.** After a `package_asset` run, `data/assets/<niche>/<niche>-directory-vN.zip`
   and `listing.json` (title, summary, markdown description, price) are ready. Create a Gumroad
   product with **exactly the listing title** and upload the zip. The agent then finds and links
   it on its own; you can also link it by hand with `automonetize assets link <asset_id> <gumroad_product_id>`.
   Gumroad's public API can't create products or upload files, so this step stays manual.
2. **Publish the showcase (optional).** `data/site/<niche>/index.html` is a static page with a
   free sample. Copy it into a GitHub Pages repo (`docs/` folder) and set `storefront_url`.
3. **Review outreach.**
   ```bash
   automonetize outreach list --full          # pending_review drafts
   automonetize outreach approve 3 5
   automonetize outreach reject 4
   automonetize outreach export               # approved drafts → data/outbox/approved-*.md
   # send them yourself, then:
   automonetize outreach mark-sent 3 5
   ```
   Drafts are only created for employers who published a contact address in their listing.
   Each one carries an opt-out line, respects a 30-day per-recipient cooldown and a daily cap,
   and has to clear a quality score (personalization, length, no spam phrases).

## Verifying revenue tracking

```bash
automonetize revenue sync                  # pull Gumroad sales (idempotent by sale id)
automonetize revenue report --days 7 --ledger
automonetize revenue add 9.00 --verified --external-id paypal-7XK2 --note "direct sale"
automonetize revenue add 9.00 --gross      # unverified; estimated fees; shown but not counted
```

* **Verified** entries are Gumroad sales pulled from the API or manual entries you mark
  `--verified`. Only these count toward the daily target and toward a hypothesis's traction.
* **Net** = gross − fees. Gumroad sales don't include fees, so fees are estimated with
  `platform_fee_pct` + `platform_fee_fixed_cents`. Adjust those to match your plan.
* Refunded, charged-back or disputed sales are skipped. `(source, external_id)` is unique, so
  running the sync again never double-counts.
* Sales are attributed to a hypothesis through the product that was linked to its asset, so
  sales on one niche's directory never keep a different niche alive.

To check the whole path without a real sale, record `automonetize revenue add 10 --verified`
and confirm `automonetize status` reads `today $10.00/$10.00`.

## Data sources & compliance

* Lead sources are **official public JSON APIs** (Remote OK, Arbeitnow, the Algolia HN API
  for "Who is hiring?"). The agent doesn't scrape HTML or log in anywhere.
* Every exported record keeps its `source` and original `url`, and bundles include
  `ATTRIBUTION.md`. **Read each source's terms before you sell a derived dataset.** Remote OK,
  for example, requires attribution and a link back.
* Contact emails are only kept when the employer published them in the listing.
* The HTTP client honours robots.txt, rate-limits per host and redacts tokens from logs.
  Rotating user agents is only there to recover from brittle content negotiation. Don't use
  it to get around a site that is blocking you.

## Testing

```bash
pytest            # 110 tests, about 2 seconds, no network
```

The suite covers the engine loop (planning, pivoting on zero traction, immediate
invalidation, retries with adjusted payloads, operational failures, emergency stop and
persistence across restarts, action and API budgets, crash survival), tool boundaries
(sandbox escapes, symlinks, timeouts, env scrubbing, directory locks, robots.txt, 4xx/5xx/429
handling, secret redaction), circuit breakers and rate limiters, the strategy pipelines
(parsing, validation, enrichment, dedupe, versioned packaging, HTML escaping, outreach
guardrails), revenue sync and attribution, and the dashboard and CLI.

## Extending

Add a strategy by subclassing `strategies.base.Strategy`, declaring the task names it
handles in `tasks`, and passing it to `Engine(strategies=[...])`. Raise on failure (the engine
retries with `ctx.attempt`/`ctx.degraded` set so you can shrink the workload), and return
`TaskResult(invalidates_hypothesis=True)` when a result proves the hypothesis can't work.
