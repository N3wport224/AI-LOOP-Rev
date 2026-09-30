# AutoMonetize: Operational Audit Scorecard

**Date:** 2026-09-30 · **Scope:** `agent/`, `strategies/`, `tools/`, `dashboard/`, deploy templates ·
**Question:** does the system autonomously learn and adapt toward sustained $10/day net revenue?

Every finding below was confirmed by a test that **failed before the fix**
(`tests/test_audit_regressions.py`, 28 tests) or, where noted, by running the real process
against the pre-audit commit.

## Executive scorecard

| Area | Before | After | Evidence |
|---|---|---|---|
| Test suite & static health | B+ | **A** | 308/308 under `pytest -W error`, 3 consecutive runs; pyflakes and ruff bug-class rules clean; 47/47 modules import in isolation (no cycles) |
| Concurrency & resources | B | **A** | 0 SQLite connections leaked (weak-reference probe); webhook executor leaked 3 threads before, 0 after |
| Database integrity | B | **A** | WAL enforced, 5 s busy timeout, idempotency at 3 levels; 10/10 hot-path queries now indexed (were full scans) |
| Price learning | **D** | **B+** | Pricing effectively couldn't act in live operation (F1, F2); now uses orders, abandoned checkouts, views, or time, and re-opens stale convergence |
| Niche discovery & pivots | C | **B+** | Discovery now ranked by live hiring demand; faded traction pivots; products get time to be found (F16) |
| Attribution & telemetry | B | **A−** | First-touch attribution survives page navigation (executed in Node); analytics window boundaries verified exact |
| Safety & resilience | B− | **A−** | Mid-cycle network loss pauses cleanly; empty `DRY_RUN=` can't go live; cadence can't be reset by wiping the DB |
| **Overall autonomy** | **5 / 10** | **8 / 10** | See "Why not 10" below |

## 1. System health

* **Tests:** 280 → **308** (28 audit regressions), all passing under `-W error`, stable across runs.
* **Leaks:** measured, not assumed. Python 3.11 emits no warning for unclosed SQLite
  connections, so a pytest plugin tracked every connection by weak reference plus live threads
  and event loops at session end. **Correction:** my first probe (strong references) reported
  21 leaked connections; the weak-reference probe showed CPython was closing them on refcount
  collection, so that's hygiene (F12), not a leak. The **thread leak was real** (F14).
* **Static analysis:** pyflakes clean. ruff (E/F/W/B/PL/SIM/RUF/S) findings reviewed: S608 (SQL
  f-strings: column names from allow-lists, values always bound), S324 (sha1 as dedupe key) and
  S310 (scheme pre-checked) are false positives. Fixed B904 and unused variables, and removed
  dead code (`report_dict`, `_verified`, `KNOWN_CHANNELS`). Function-level imports
  (PLC0415) are deliberate: optional dependencies and cycle-breaking.
* **Concurrency:** engine and webhook share one lock-guarded connection inside the supervisor
  (no SQLite lock collisions possible); delivery uses an atomic `paid → delivering` claim.

## 2. Database integrity

| Check | Result |
|---|---|
| `PRAGMA journal_mode` | `wal` (set on every file DB open) · `busy_timeout` 5000 ms · `synchronous` FULL |
| Webhook idempotency | `webhook_events.event_id` PRIMARY KEY (the column is `event_id`, not `stripe_event_id`) |
| Order / revenue idempotency | `UNIQUE(provider, order_id)` · `UNIQUE(source, external_id)`: webhook and polling can't double-count |
| Indexes | **Fixed (F13):** `assets` and `price_experiments` had none; 10 per-cycle queries were full scans. 13 indexes added, applied after migrations so existing DBs get them too |

## 3. Findings and fixes

| # | Sev | Finding | Fix |
|---|---|---|---|
| F1 | **Critical** | Every data refresh (new leads → new asset version, typically daily) **re-derived the price from tiers** and **started a new price experiment**. So the 48h window never elapsed and step-downs never happened; the lander could show $5 while the reused Payment Link charged $9 | A live product keeps its price; experiments and tier history follow the product (Payment Link), not the version |
| F16 | **Critical** | Default config (hourly cycles, 24-cycle pivot) **abandoned every product after 24 h**, before its first 48h pricing window or a second syndication post (5-day cadence) | Zero-traction pivots also require `min_hypothesis_days` (10); hard evidence (no data) still pivots at once |
| F9 | High | A **network drop mid-cycle** burned 3 retries per task, logged operational failures, spent pivot budget, and with ≥5 network tasks in a row **tripped the emergency stop**. Real-process check on the old commit: 2 op-failures, 41 error rows, 1 iteration spent | On any task failure, re-probe; if offline, abort the cycle, requeue, count nothing. Real process after: 4 clean `offline` cycles, breaker OK, 0 failures, 0 iterations spent |
| F10 | High | `DRY_RUN=` (empty or typo) parsed as **False → live email** | Booleans flip only on explicit tokens; anything else keeps the safe default |
| F2 | High | Without GitHub view tracking, views are always 0, so an unsold price was **held forever** | Time-based step-down after 2 × window with no sale |
| F3 | Med | Checkout initiations were measured but **never used** in pricing | ≥ 3 abandoned checkouts with no sale → step down |
| F4 | Med | `converged` was terminal: a price that stopped selling was never re-explored | Converged prices re-open when recent orders hit 0 |
| F5 | Med | GitHub's rolling 14-day count was stored as a running max, which **froze after the peak** | Running total of increases (lower bound on real views) |
| F6 | Med | Discovery from live tags only happened after ~11 hard-coded niches/clusters | Every candidate ranked by 7-day demand (word-boundary matching); fixed order only with no data |
| F7 | Med | One early sale made a niche **immortal** | "Traction faded": no sale in 14 days and no active subscribers → pivot |
| F8 | Med | After a pivot, subscribers of the old niche got **"0 changes" every week** (churn risk) | Subscribed niches are refreshed before each Monday delivery |
| F11 | Med | 5-day syndication cadence lived only in the local DB; **a wiped DB meant an immediate repost** | Cadence checked against each platform's own last-post record; fails closed |
| F15 | Med | UTM attribution **lost on page navigation** (verified by running the original script in Node) | First-touch kept in `localStorage` for 30 days, mirrored exactly by the Python decoder |
| F13 | Low* | No indexes on hot paths (*grows with volume) | 13 indexes |
| F14 | Low | Webhook executor threads leaked on every listener shutdown or restart | Executor shut down after fulfilment drains |
| F12 | Hygiene | CLI commands relied on GC to close the DB | Explicit close plus WAL checkpoint in `main()` |

**Verified correct, no change needed:** WAL and busy timeout; webhook/order/revenue
idempotency; cold outreach needs approval + `dry_run=false` + CAN-SPAM fields; the cadence
survives engine restarts (persisted); analytics window edges (first second in, one second
before out, "now" included); no circular imports; supervisor SIGTERM/checkpoint/lock release.

## 4. Why not 10: bottlenecks and risks

1. **Demand is the binding constraint, not code.** $10/day net needs ≈ **1.2 sales/day at $9**
   (net $8.44 after Stripe fees), or ≈ 0.75/day at $14, or **~32 subscribers** at $10/month
   ($9.41 net each). At typical 1–3% checkout conversion that's roughly 40–120 lander visitors
   a day. Inbound channels run at one post per platform per 5 days plus SEO and a monthly HN
   gist, so early traffic will likely be far below that. The system now learns correctly from
   the traffic it gets; it can't create demand.
2. **Unverified live integrations.** This environment blocks outbound hosts, so Stripe, Dev.to,
   Hashnode, GitHub and the job boards were exercised only against recorded API shapes (and the
   real Stripe SDK for signatures). In particular, the Hashnode last-post GraphQL query is
   unverified; because it fails closed, a schema mismatch would silently stop Hashnode posts
   (visible in the error log). First production step: a test-mode run of each integration.
3. **Coarse view signal.** GitHub traffic gives the top 10 paths over a rolling 14 days; the
   counter is a lower bound. Abandoned checkouts and time-based fallbacks now cover the gap.
4. **Single host.** SQLite on one machine; webhooks need a public tunnel (polling covers
   gaps); a sleeping laptop delays cycles (catch-up exists). Stripe stays the ledger of record.
5. **Source and terms concentration.** Three public job APIs; reselling derived data depends
   on their terms.
6. **Sequential exploration.** One active niche at a time, ≥ 10 days each, so ~3 new niches a
   month. Older products stay on sale, so the portfolio still grows.

## 5. How to reproduce

```bash
pytest -W error                                   # 308 passed
pytest tests/test_audit_regressions.py -v         # the 28 audit regressions
ruff check agent strategies tools dashboard --select E,F,W,B,PL,SIM,RUF   # see the ignore list in this report
```

## 6. Addendum: set-and-forget operation on macOS

Follow-up to section 4, bottleneck 4 ("single host"). Test suite: **364 passed** under
`pytest -W error` (56 new in `tests/test_autonomy.py`).

| Gap | Before | Now |
|---|---|---|
| Public webhook URL | Manual tunnel in a terminal | `deploy/tunnel/setup_tunnel.sh`: named Cloudflare Tunnel, ingress limited to `/webhook` and `/healthz`, companion launchd job, URL saved to `.env` |
| Stripe endpoint and secret | Copied by hand from the Dashboard | `setup-autonomous` creates or reuses it; the secret goes straight to `.env` (0600) |
| Verification | None end to end | Signed handshake through the public URL must be verified and recorded by the listener |
| Breaker trip | Permanent stop until a human ran `resume` | Alert, 2 h quarantine (escalating to 24 h), self-diagnostic, automatic clean cycle |
| Manual stop under launchd | `supervise` exited, so launchd restarted it every 30 s | Starts paused; webhook keeps fulfilling |
| Syndication outages | Retried every cycle | Persisted exponential cooldown per platform |
| SQLite lock outlasting `busy_timeout` | Task failure | 5 jittered retries |
| Mac sleep | Cycles delayed by the full interval after wake (monotonic wait) | Wall-clock waits; power assertion only while working; optional `pmset` wake |

Remaining limits, by design or by the platform: a Cloudflare-managed domain and one browser login
are required; a sleeping Mac can't receive webhooks (Stripe retries plus polling mean orders are
delayed, not lost); waking from sleep needs a one-line sudoers rule; a FileVault Mac that reboots
waits at the unlock screen. Real Cloudflare, launchd and IOKit were not exercised here (Linux
container): they're covered through their command lines, config formats and a fake ctypes
library, and `setup-autonomous` is the on-device check.
