"""Persistent agent state in an isolated SQLite database."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import weakref
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from agent.recovery import retry_sqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hypotheses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    strategy TEXT NOT NULL,
    description TEXT NOT NULL,
    params TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',      -- active | deprecated
    iterations INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle INTEGER NOT NULL,
    hypothesis_id INTEGER,
    name TEXT NOT NULL,
    status TEXT NOT NULL,                        -- ok | failed | skipped
    detail TEXT,
    duration REAL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'error',          -- error | operational_failure | circuit
    message TEXT NOT NULL,
    traceback TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revenue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    gross_cents INTEGER NOT NULL,
    fee_cents INTEGER NOT NULL DEFAULT 0,
    net_cents INTEGER NOT NULL,
    verified INTEGER NOT NULL DEFAULT 0,
    hypothesis_id INTEGER,
    product_ref TEXT,
    note TEXT,
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE (source, external_id)
);
CREATE TABLE IF NOT EXISTS backlog (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hypothesis_id INTEGER,
    task TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    priority INTEGER NOT NULL DEFAULT 100,
    status TEXT NOT NULL DEFAULT 'pending',      -- pending | done | failed | cancelled
    result TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT NOT NULL,
    niche TEXT NOT NULL,
    data TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    UNIQUE (dedupe_key, niche)
);
CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hypothesis_id INTEGER,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    path TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    lead_count INTEGER NOT NULL DEFAULT 0,
    price_cents INTEGER NOT NULL DEFAULT 0,
    product_ref TEXT,
    status TEXT NOT NULL DEFAULT 'staged',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outreach_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hypothesis_id INTEGER,
    lead_key TEXT NOT NULL,
    channel TEXT NOT NULL,
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    score REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_review', -- pending_review | approved | rejected | sent
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    UNIQUE (hypothesis_id, lead_key, channel)
);
CREATE INDEX IF NOT EXISTS idx_backlog_pending ON backlog (hypothesis_id, status, priority);
CREATE INDEX IF NOT EXISTS idx_revenue_day ON revenue (occurred_at);
CREATE INDEX IF NOT EXISTS idx_outreach_status ON outreach_queue (status);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    order_id TEXT NOT NULL,
    email TEXT,
    gross_cents INTEGER NOT NULL,
    product_ref TEXT,
    asset_id INTEGER,
    hypothesis_id INTEGER,
    status TEXT NOT NULL DEFAULT 'paid',       -- paid | delivered | needs_manual_delivery | refunded
    delivery_attempts INTEGER NOT NULL DEFAULT 0,
    delivered_at TEXT,
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE (provider, order_id)
);
CREATE TABLE IF NOT EXISTS suppression (
    email TEXT PRIMARY KEY,
    reason TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS funnel_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hypothesis_id INTEGER NOT NULL,
    metric TEXT NOT NULL,                      -- impressions | views | purchases
    source TEXT NOT NULL,
    value INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    UNIQUE (hypothesis_id, metric, source)
);
CREATE TABLE IF NOT EXISTS webhook_events (
    event_id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    status TEXT NOT NULL,                      -- processed | ignored | failed
    detail TEXT,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS price_experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL,
    hypothesis_id INTEGER,
    price_cents INTEGER NOT NULL,
    provider TEXT NOT NULL,
    product_ref TEXT,                          -- payment link id: orders on it belong to this experiment
    checkout_url TEXT,
    views_at_start INTEGER NOT NULL DEFAULT 0,
    views INTEGER NOT NULL DEFAULT 0,
    initiations INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',    -- running | ended | converged
    reason TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT
);
CREATE TABLE IF NOT EXISTS checkout_sessions (
    session_id TEXT PRIMARY KEY,
    product_ref TEXT,
    payment_intent TEXT,
    status TEXT NOT NULL,                      -- open | complete | expired
    payment_status TEXT,
    email TEXT,
    amount_cents INTEGER,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subscribers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    subscription_id TEXT NOT NULL UNIQUE,
    customer_id TEXT,
    email TEXT,
    niche TEXT,
    asset_id INTEGER,
    hypothesis_id INTEGER,
    price_cents INTEGER NOT NULL DEFAULT 0,
    interval TEXT NOT NULL DEFAULT 'month',
    subscription_status TEXT NOT NULL DEFAULT 'active',  -- active | trialing | past_due | canceled | unpaid | incomplete
    channel TEXT,
    campaign TEXT,
    started_at TEXT NOT NULL,
    canceled_at TEXT,
    current_period_end TEXT,
    last_delivered_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subscription_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscriber_id INTEGER NOT NULL,
    period_key TEXT NOT NULL,                  -- e.g. 2026-W40, or "welcome"
    status TEXT NOT NULL,                      -- delivered | dry_run | failed
    delta_count INTEGER NOT NULL DEFAULT 0,
    detail TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (subscriber_id, period_key)
);
"""

# Hot-path indexes (the audit found every per-cycle query doing a full table scan).
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_orders_hyp_time ON orders (hypothesis_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_orders_ref ON orders (product_ref, occurred_at);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders (status);
CREATE INDEX IF NOT EXISTS idx_assets_ref ON assets (product_ref);
CREATE INDEX IF NOT EXISTS idx_assets_hyp_kind ON assets (hypothesis_id, kind, version);
CREATE INDEX IF NOT EXISTS idx_hypotheses_status ON hypotheses (status);
CREATE INDEX IF NOT EXISTS idx_subscribers_status ON subscribers (subscription_status, niche);
CREATE INDEX IF NOT EXISTS idx_subscribers_tier ON subscribers (tier, subscription_status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_subscribers_token ON subscribers (token) WHERE token IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sessions_pi ON checkout_sessions (payment_intent);
CREATE INDEX IF NOT EXISTS idx_sessions_ref ON checkout_sessions (product_ref, updated_at);
CREATE INDEX IF NOT EXISTS idx_experiments_asset ON price_experiments (asset_id, status);
CREATE INDEX IF NOT EXISTS idx_experiments_ref ON price_experiments (product_ref);
CREATE INDEX IF NOT EXISTS idx_revenue_hyp ON revenue (hypothesis_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_leads_first_seen ON leads (first_seen);
"""

# Columns added after the first release; applied to existing databases on open.
MIGRATIONS: dict[str, dict[str, str]] = {
    "assets": {
        "checkout_url": "TEXT",
        "provider": "TEXT",
        "showcase_url": "TEXT",
        "lander_url": "TEXT",
        "niche": "TEXT",
        "kind_meta": "TEXT",
    },
    "orders": {
        "channel": "TEXT",
        "campaign": "TEXT",
        "kind": "TEXT NOT NULL DEFAULT 'one_off'",  # one_off | subscription
    },
    "checkout_sessions": {
        "channel": "TEXT",
        "campaign": "TEXT",
        "mode": "TEXT",
    },
    "subscribers": {
        # paid = Stripe subscription; free = lead-magnet signup (provider "lead_magnet").
        "tier": "TEXT NOT NULL DEFAULT 'paid'",
        "token": "TEXT",          # confirm / unsubscribe links (free tier)
        "confirmed_at": "TEXT",
    },
    "outreach_queue": {
        "sent_at": "TEXT",
        "message_id": "TEXT",
        "dry_run_at": "TEXT",
        "send_attempts": "INTEGER NOT NULL DEFAULT 0",
    },
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class StateStore:
    _instances: "weakref.WeakSet[StateStore]" = weakref.WeakSet()

    @classmethod
    def open_stores(cls) -> list["StateStore"]:
        return [s for s in list(cls._instances) if not s.closed]

    @classmethod
    def open_count(cls) -> int:
        return len(cls.open_stores())

    def __init__(self, db_path: str | Path, clock: Callable[[], datetime] = utc_now, busy_timeout: float = 5.0,
                 lock_retry_sleep: Callable[[float], None] | None = None):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._lock = threading.RLock()
        # SQLite waits up to busy_timeout inside each statement; retry_sqlite adds up to 5 jittered
        # attempts on top for the rare lock that outlasts it (a CLI command, a long checkpoint).
        self._retry_kwargs: dict[str, Any] = {} if lock_retry_sleep is None else {"sleep": lock_retry_sleep}
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None, timeout=busy_timeout)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.db_path != ":memory:":
            self._retry(lambda: self.conn.execute("PRAGMA journal_mode = WAL"))
        self._retry(lambda: self.conn.executescript(SCHEMA))
        self._retry(self._migrate)
        self._retry(lambda: self.conn.executescript(INDEXES))  # after migrations: some index columns were added by them
        self.closed = False
        StateStore._instances.add(self)

    def _migrate(self) -> None:
        for table, columns in MIGRATIONS.items():
            existing = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, decl in columns.items():
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def close(self) -> None:
        if not getattr(self, "closed", False):
            self.conn.close()
            self.closed = True

    def now(self) -> str:
        return iso(self.clock())

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            # BEGIN IMMEDIATE takes the write lock up front, so it is the point that can find the
            # database locked; a COMMIT that hits a busy checkpoint is retried the same way.
            self._retry(lambda: self.conn.execute("BEGIN IMMEDIATE"))
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self._retry(lambda: self.conn.execute("COMMIT"))

    def _retry(self, fn: Callable[[], Any]) -> Any:
        return retry_sqlite(fn, **self._retry_kwargs)

    def _all(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return self._retry(lambda: [dict(r) for r in self.conn.execute(sql, args).fetchall()])

    def _one(self, sql: str, args: tuple = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._retry(lambda: self.conn.execute(sql, args).fetchone())
            return dict(row) if row else None

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._retry(lambda: self.conn.execute(sql, args))

    # -- key/value ------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        row = self._one("SELECT value FROM kv WHERE key = ?", (key,))
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value: Any) -> None:
        self._exec(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    def incr(self, key: str, amount: int = 1) -> int:
        with self.tx():
            value = int(self.get(key, 0)) + amount
            self.set(key, value)
        return value

    # -- hypotheses -----------------------------------------------------------
    def create_hypothesis(self, key: str, strategy: str, description: str, params: dict[str, Any]) -> int:
        now = self.now()
        cur = self._exec(
            "INSERT INTO hypotheses (key, strategy, description, params, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (key, strategy, description, json.dumps(params), now, now),
        )
        return int(cur.lastrowid)

    @staticmethod
    def _hyp(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is not None:
            row["params"] = json.loads(row["params"])
        return row

    def get_hypothesis(self, hypothesis_id: int) -> dict[str, Any] | None:
        return self._hyp(self._one("SELECT * FROM hypotheses WHERE id = ?", (hypothesis_id,)))

    def active_hypothesis(self) -> dict[str, Any] | None:
        return self._hyp(self._one("SELECT * FROM hypotheses WHERE status = 'active' ORDER BY id DESC LIMIT 1"))

    def list_hypotheses(self) -> list[dict[str, Any]]:
        return [self._hyp(r) for r in self._all("SELECT * FROM hypotheses ORDER BY id")]  # type: ignore[misc]

    def hypothesis_keys(self) -> set[str]:
        return {r["key"] for r in self._all("SELECT key FROM hypotheses")}

    def set_hypothesis_status(self, hypothesis_id: int, status: str, reason: str | None = None) -> None:
        self._exec(
            "UPDATE hypotheses SET status = ?, reason = ?, updated_at = ? WHERE id = ?",
            (status, reason, self.now(), hypothesis_id),
        )

    def update_hypothesis_params(self, hypothesis_id: int, params: dict[str, Any]) -> None:
        self._exec(
            "UPDATE hypotheses SET params = ?, updated_at = ? WHERE id = ?",
            (json.dumps(params), self.now(), hypothesis_id),
        )

    def increment_hypothesis_iterations(self, hypothesis_id: int) -> int:
        self._exec(
            "UPDATE hypotheses SET iterations = iterations + 1, updated_at = ? WHERE id = ?",
            (self.now(), hypothesis_id),
        )
        row = self._one("SELECT iterations FROM hypotheses WHERE id = ?", (hypothesis_id,))
        return int(row["iterations"]) if row else 0

    # -- actions & errors -----------------------------------------------------
    def log_action(
        self, cycle: int, hypothesis_id: int | None, name: str, status: str, detail: str = "", duration: float = 0.0
    ) -> int:
        cur = self._exec(
            "INSERT INTO actions (cycle, hypothesis_id, name, status, detail, duration, created_at) VALUES (?,?,?,?,?,?,?)",
            (cycle, hypothesis_id, name, status, detail, duration, self.now()),
        )
        return int(cur.lastrowid)

    def recent_actions(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM actions ORDER BY id DESC LIMIT ?", (limit,))

    def count_actions(self, status: str | None = None) -> int:
        if status:
            return int(self._one("SELECT COUNT(*) AS n FROM actions WHERE status = ?", (status,))["n"])  # type: ignore[index]
        return int(self._one("SELECT COUNT(*) AS n FROM actions")["n"])  # type: ignore[index]

    def log_error(self, source: str, message: str, traceback: str = "", kind: str = "error") -> int:
        cur = self._exec(
            "INSERT INTO errors (source, kind, message, traceback, created_at) VALUES (?,?,?,?,?)",
            (source, kind, message[:2000], traceback[:20000], self.now()),
        )
        return int(cur.lastrowid)

    def recent_errors(self, limit: int = 20, kind: str | None = None) -> list[dict[str, Any]]:
        if kind:
            return self._all("SELECT * FROM errors WHERE kind = ? ORDER BY id DESC LIMIT ?", (kind, limit))
        return self._all("SELECT * FROM errors ORDER BY id DESC LIMIT ?", (limit,))

    def count_errors(self, kind: str | None = None) -> int:
        if kind:
            return int(self._one("SELECT COUNT(*) AS n FROM errors WHERE kind = ?", (kind,))["n"])  # type: ignore[index]
        return int(self._one("SELECT COUNT(*) AS n FROM errors")["n"])  # type: ignore[index]

    # -- revenue --------------------------------------------------------------
    def record_revenue(
        self,
        source: str,
        external_id: str,
        gross_cents: int,
        fee_cents: int,
        net_cents: int,
        verified: bool,
        occurred_at: str | None = None,
        hypothesis_id: int | None = None,
        product_ref: str | None = None,
        note: str | None = None,
    ) -> bool:
        """Insert a revenue event. Returns False if (source, external_id) already exists."""
        cur = self._exec(
            "INSERT OR IGNORE INTO revenue (source, external_id, gross_cents, fee_cents, net_cents, verified, "
            "hypothesis_id, product_ref, note, occurred_at, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                source, external_id, gross_cents, fee_cents, net_cents, int(verified),
                hypothesis_id, product_ref, note, occurred_at or self.now(), self.now(),
            ),
        )
        return cur.rowcount == 1

    def revenue_between(self, start: datetime, end: datetime, verified_only: bool = True) -> dict[str, int]:
        sql = (
            "SELECT COALESCE(SUM(gross_cents),0) AS gross, COALESCE(SUM(fee_cents),0) AS fees, "
            "COALESCE(SUM(net_cents),0) AS net, COUNT(*) AS n FROM revenue WHERE occurred_at >= ? AND occurred_at < ?"
        )
        if verified_only:
            sql += " AND verified = 1"
        row = self._one(sql, (iso(start), iso(end)))
        return {k: int(v) for k, v in row.items()}  # type: ignore[union-attr]

    def revenue_for_day(self, day: datetime | None = None, verified_only: bool = True) -> dict[str, int]:
        day = (day or self.clock()).astimezone(timezone.utc)
        start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        return self.revenue_between(start, start + timedelta(days=1), verified_only)

    def revenue_for_hypothesis(self, hypothesis_id: int, verified_only: bool = True) -> int:
        sql = "SELECT COALESCE(SUM(net_cents),0) AS net FROM revenue WHERE hypothesis_id = ?"
        if verified_only:
            sql += " AND verified = 1"
        return int(self._one(sql, (hypothesis_id,))["net"])  # type: ignore[index]

    def revenue_with_orders(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Verified revenue rows in [start, end) joined with their order (channel, kind, asset)."""
        return self._all(
            "SELECT r.source, r.external_id, r.gross_cents, r.fee_cents, r.net_cents, r.hypothesis_id, r.occurred_at, "
            "o.channel, o.campaign, o.kind, o.asset_id FROM revenue r LEFT JOIN orders o "
            "ON o.provider = r.source AND o.order_id = r.external_id "
            "WHERE r.verified = 1 AND r.occurred_at >= ? AND r.occurred_at < ? ORDER BY r.occurred_at",
            (iso(start), iso(end)),
        )

    def first_revenue_at(self) -> str | None:
        row = self._one("SELECT MIN(occurred_at) AS t FROM revenue WHERE verified = 1")
        return row["t"] if row else None

    def list_revenue(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM revenue ORDER BY occurred_at DESC LIMIT ?", (limit,))

    # -- backlog --------------------------------------------------------------
    def add_task(self, hypothesis_id: int | None, task: str, payload: dict[str, Any] | None = None, priority: int = 100) -> int:
        now = self.now()
        cur = self._exec(
            "INSERT INTO backlog (hypothesis_id, task, payload, priority, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (hypothesis_id, task, json.dumps(payload or {}), priority, now, now),
        )
        return int(cur.lastrowid)

    @staticmethod
    def _task(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is not None:
            row["payload"] = json.loads(row["payload"])
        return row

    def next_task(self, hypothesis_id: int) -> dict[str, Any] | None:
        return self._task(
            self._one(
                "SELECT * FROM backlog WHERE hypothesis_id = ? AND status = 'pending' ORDER BY priority, id LIMIT 1",
                (hypothesis_id,),
            )
        )

    def pending_tasks(self, hypothesis_id: int | None = None) -> list[dict[str, Any]]:
        if hypothesis_id is None:
            rows = self._all("SELECT * FROM backlog WHERE status = 'pending' ORDER BY priority, id")
        else:
            rows = self._all(
                "SELECT * FROM backlog WHERE status = 'pending' AND hypothesis_id = ? ORDER BY priority, id",
                (hypothesis_id,),
            )
        return [self._task(r) for r in rows]  # type: ignore[misc]

    def finish_task(self, task_id: int, status: str, result: str = "", attempts: int = 1) -> None:
        self._exec(
            "UPDATE backlog SET status = ?, result = ?, attempts = attempts + ?, updated_at = ? WHERE id = ?",
            (status, result[:4000], attempts, self.now(), task_id),
        )

    def cancel_tasks(self, hypothesis_id: int) -> int:
        cur = self._exec(
            "UPDATE backlog SET status = 'cancelled', updated_at = ? WHERE hypothesis_id = ? AND status = 'pending'",
            (self.now(), hypothesis_id),
        )
        return cur.rowcount

    # -- leads ----------------------------------------------------------------
    def upsert_lead(self, dedupe_key: str, niche: str, data: dict[str, Any]) -> bool:
        """Insert or refresh a lead. Returns True if it was new for this niche."""
        now = self.now()
        with self.tx() as conn:
            exists = conn.execute(
                "SELECT 1 FROM leads WHERE dedupe_key = ? AND niche = ?", (dedupe_key, niche)
            ).fetchone()
            if exists:
                conn.execute(
                    "UPDATE leads SET data = ?, last_seen = ? WHERE dedupe_key = ? AND niche = ?",
                    (json.dumps(data, sort_keys=True), now, dedupe_key, niche),
                )
                return False
            conn.execute(
                "INSERT INTO leads (dedupe_key, niche, data, first_seen, last_seen) VALUES (?,?,?,?,?)",
                (dedupe_key, niche, json.dumps(data, sort_keys=True), now, now),
            )
            return True

    def leads_for_niche(self, niche: str) -> list[dict[str, Any]]:
        rows = self._all("SELECT dedupe_key, data, first_seen FROM leads WHERE niche = ? ORDER BY id", (niche,))
        out = []
        for r in rows:
            d = json.loads(r["data"])
            d["dedupe_key"] = r["dedupe_key"]
            d["first_seen"] = r["first_seen"]
            out.append(d)
        return out

    def niche_last_seen(self, niche: str) -> str | None:
        row = self._one("SELECT MAX(last_seen) AS t FROM leads WHERE niche = ?", (niche,))
        return row["t"] if row else None

    def count_leads(self, niche: str | None = None) -> int:
        if niche:
            return int(self._one("SELECT COUNT(*) AS n FROM leads WHERE niche = ?", (niche,))["n"])  # type: ignore[index]
        return int(self._one("SELECT COUNT(DISTINCT dedupe_key) AS n FROM leads")["n"])  # type: ignore[index]

    def tag_frequencies(self, limit: int = 25) -> list[tuple[str, int]]:
        counts: dict[str, int] = {}
        seen: set[str] = set()
        for r in self._all("SELECT dedupe_key, data FROM leads"):
            if r["dedupe_key"] in seen:
                continue
            seen.add(r["dedupe_key"])
            d = json.loads(r["data"])
            for tag in {t.lower() for t in (d.get("tags") or []) + (d.get("stack") or [])}:
                counts[tag] = counts.get(tag, 0) + 1
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]

    # -- assets ---------------------------------------------------------------
    def add_asset(
        self, hypothesis_id: int | None, kind: str, title: str, path: str, version: int,
        lead_count: int, price_cents: int, product_ref: str | None = None,
    ) -> int:
        cur = self._exec(
            "INSERT INTO assets (hypothesis_id, kind, title, path, version, lead_count, price_cents, product_ref, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (hypothesis_id, kind, title, path, version, lead_count, price_cents, product_ref, self.now()),
        )
        return int(cur.lastrowid)

    def list_assets(self, hypothesis_id: int | None = None) -> list[dict[str, Any]]:
        if hypothesis_id is None:
            return self._all("SELECT * FROM assets ORDER BY id DESC")
        return self._all("SELECT * FROM assets WHERE hypothesis_id = ? ORDER BY id DESC", (hypothesis_id,))

    def latest_asset(self, hypothesis_id: int, kind: str) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM assets WHERE hypothesis_id = ? AND kind = ? ORDER BY version DESC LIMIT 1",
            (hypothesis_id, kind),
        )

    def get_asset(self, asset_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM assets WHERE id = ?", (asset_id,))

    def update_asset(self, asset_id: int, **fields: Any) -> None:
        allowed = {"checkout_url", "provider", "showcase_url", "lander_url", "product_ref", "status", "niche", "price_cents", "kind_meta"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"cannot update asset fields {sorted(bad)}")
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE assets SET {cols} WHERE id = ?", (*fields.values(), asset_id))

    def asset_for_product(self, product_ref: str) -> dict[str, Any] | None:
        """The newest asset carrying ``product_ref``, only if the ref is unambiguous across hypotheses.

        Falls back to price experiments, so orders on a retired price's link still find their dataset."""
        rows = self._all("SELECT * FROM assets WHERE product_ref = ? ORDER BY id DESC", (product_ref,))
        if not rows:
            exp = self._one(
                "SELECT asset_id FROM price_experiments WHERE product_ref = ? ORDER BY id DESC LIMIT 1", (product_ref,)
            )
            return self.get_asset(exp["asset_id"]) if exp else None
        if len({r["hypothesis_id"] for r in rows}) > 1:
            return None
        return rows[0]

    def all_product_refs(self) -> list[str]:
        refs = {r["product_ref"] for r in self._all("SELECT product_ref FROM assets WHERE product_ref IS NOT NULL")}
        refs |= {r["product_ref"] for r in self._all("SELECT product_ref FROM price_experiments WHERE product_ref IS NOT NULL")}
        return sorted(refs)

    def asset_by_title(self, title: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM assets WHERE lower(title) = lower(?) ORDER BY id DESC LIMIT 1", (title.strip(),))

    def link_product(self, asset_id: int, product_ref: str) -> None:
        self._exec("UPDATE assets SET product_ref = ?, status = 'listed' WHERE id = ?", (product_ref, asset_id))

    def hypothesis_for_product(self, product_ref: str) -> int | None:
        row = self._one(
            "SELECT hypothesis_id FROM assets WHERE product_ref = ? ORDER BY id DESC LIMIT 1", (product_ref,)
        )
        return row["hypothesis_id"] if row else None

    # -- outreach -------------------------------------------------------------
    def stage_outreach(
        self, hypothesis_id: int | None, lead_key: str, channel: str, recipient: str,
        subject: str, body: str, score: float,
    ) -> int | None:
        cur = self._exec(
            "INSERT OR IGNORE INTO outreach_queue (hypothesis_id, lead_key, channel, recipient, subject, body, score, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (hypothesis_id, lead_key, channel, recipient, subject, body, score, self.now()),
        )
        return int(cur.lastrowid) if cur.rowcount == 1 else None

    def list_outreach(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if status:
            return self._all(
                "SELECT * FROM outreach_queue WHERE status = ? ORDER BY id LIMIT ?", (status, limit)
            )
        return self._all("SELECT * FROM outreach_queue ORDER BY id LIMIT ?", (limit,))

    def set_outreach_status(self, outreach_id: int, status: str) -> bool:
        cur = self._exec(
            "UPDATE outreach_queue SET status = ?, reviewed_at = ? WHERE id = ?", (status, self.now(), outreach_id)
        )
        return cur.rowcount == 1

    def outreach_staged_since(self, since: datetime) -> int:
        return int(
            self._one("SELECT COUNT(*) AS n FROM outreach_queue WHERE created_at >= ?", (iso(since),))["n"]  # type: ignore[index]
        )

    def recipient_contacted_since(self, recipient: str, since: datetime) -> bool:
        return (
            self._one(
                "SELECT 1 FROM outreach_queue WHERE recipient = ? AND created_at >= ? AND status != 'rejected' LIMIT 1",
                (recipient, iso(since)),
            )
            is not None
        )

    def mark_outreach_sent(self, outreach_id: int, message_id: str) -> None:
        self._exec(
            "UPDATE outreach_queue SET status = 'sent', sent_at = ?, message_id = ?, "
            "send_attempts = send_attempts + 1 WHERE id = ?",
            (self.now(), message_id, outreach_id),
        )

    def mark_outreach_dry_run(self, outreach_id: int) -> None:
        self._exec("UPDATE outreach_queue SET dry_run_at = ? WHERE id = ?", (self.now(), outreach_id))

    def record_send_failure(self, outreach_id: int, max_attempts: int = 3) -> None:
        self._exec("UPDATE outreach_queue SET send_attempts = send_attempts + 1 WHERE id = ?", (outreach_id,))
        self._exec(
            "UPDATE outreach_queue SET status = 'failed' WHERE id = ? AND send_attempts >= ?", (outreach_id, max_attempts)
        )

    def outreach_sent_for(self, hypothesis_id: int) -> int:
        return int(
            self._one(
                "SELECT COUNT(*) AS n FROM outreach_queue WHERE hypothesis_id = ? AND status = 'sent'", (hypothesis_id,)
            )["n"]  # type: ignore[index]
        )

    def sent_since(self, since: datetime) -> int:
        return int(
            self._one("SELECT COUNT(*) AS n FROM outreach_queue WHERE sent_at >= ?", (iso(since),))["n"]  # type: ignore[index]
        )

    def first_send_at(self) -> str | None:
        row = self._one("SELECT MIN(sent_at) AS t FROM outreach_queue WHERE sent_at IS NOT NULL")
        return row["t"] if row else None

    # -- suppression ----------------------------------------------------------
    def suppress(self, email: str, reason: str = "") -> bool:
        cur = self._exec(
            "INSERT OR IGNORE INTO suppression (email, reason, created_at) VALUES (?,?,?)",
            (email.strip().lower(), reason, self.now()),
        )
        if cur.rowcount:
            # Pull any queued drafts for this address so they can never go out.
            self._exec(
                "UPDATE outreach_queue SET status = 'suppressed' WHERE recipient = ? AND status IN ('pending_review','approved')",
                (email.strip().lower(),),
            )
        return cur.rowcount == 1

    def is_suppressed(self, email: str) -> bool:
        return self._one("SELECT 1 FROM suppression WHERE email = ?", (email.strip().lower(),)) is not None

    def list_suppressed(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM suppression ORDER BY created_at DESC")

    # -- orders / fulfilment --------------------------------------------------
    def record_order(
        self, provider: str, order_id: str, email: str | None, gross_cents: int, product_ref: str | None,
        asset_id: int | None, hypothesis_id: int | None, occurred_at: str | None = None, status: str = "paid",
        channel: str | None = None, campaign: str | None = None, kind: str = "one_off",
    ) -> bool:
        cur = self._exec(
            "INSERT OR IGNORE INTO orders (provider, order_id, email, gross_cents, product_ref, asset_id, hypothesis_id, "
            "status, occurred_at, recorded_at, channel, campaign, kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (provider, order_id, (email or "").lower() or None, gross_cents, product_ref, asset_id, hypothesis_id,
             status, occurred_at or self.now(), self.now(), channel, campaign, kind),
        )
        return cur.rowcount == 1

    def orders_to_deliver(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM orders WHERE status = 'paid' ORDER BY id")

    def list_orders(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if status:
            return self._all("SELECT * FROM orders WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit))
        return self._all("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,))

    def set_order_status(self, order_pk: int, status: str, asset_id: int | None = None) -> None:
        if status != "delivering" and self.get(f"delivering:{order_pk}"):
            self.set(f"delivering:{order_pk}", None)
        delivered = self.now() if status == "delivered" else None
        self._exec(
            "UPDATE orders SET status = ?, delivered_at = COALESCE(?, delivered_at), asset_id = COALESCE(?, asset_id), "
            "delivery_attempts = delivery_attempts + CASE WHEN ? IN ('delivered','delivery_failed') THEN 1 ELSE 0 END "
            "WHERE id = ?",
            (status, delivered, asset_id, status, order_pk),
        )

    def claim_order_for_delivery(self, order_pk: int) -> bool:
        """paid → delivering, atomically. Only one worker (webhook or engine sweep) wins."""
        cur = self._exec(
            "UPDATE orders SET status = 'delivering' WHERE id = ? AND status = 'paid'",
            (order_pk,),
        )
        if cur.rowcount == 1:
            self.set(f"delivering:{order_pk}", self.now())
        return cur.rowcount == 1

    def release_order(self, order_pk: int) -> None:
        self._exec("UPDATE orders SET status = 'paid' WHERE id = ? AND status = 'delivering'", (order_pk,))
        self.set(f"delivering:{order_pk}", None)

    def reset_stale_deliveries(self, older_than: timedelta = timedelta(minutes=15)) -> int:
        """Orders stuck in 'delivering' (process died mid-send) go back to 'paid'."""
        cutoff = self.clock() - older_than
        n = 0
        for row in self._all("SELECT id FROM orders WHERE status = 'delivering'"):
            started = self.get(f"delivering:{row['id']}")
            if not started or datetime.fromisoformat(started) < cutoff:
                self.release_order(row["id"])
                n += 1
        return n

    def record_delivery_failure(self, order_pk: int, max_attempts: int = 3) -> None:
        self._exec("UPDATE orders SET delivery_attempts = delivery_attempts + 1 WHERE id = ?", (order_pk,))
        self._exec(
            "UPDATE orders SET status = 'needs_manual_delivery' WHERE id = ? AND delivery_attempts >= ?",
            (order_pk, max_attempts),
        )

    def orders_since(self, hypothesis_id: int, since: datetime) -> int:
        return int(
            self._one(
                "SELECT COUNT(*) AS n FROM orders WHERE hypothesis_id = ? AND occurred_at >= ? AND status != 'refunded'",
                (hypothesis_id, iso(since)),
            )["n"]  # type: ignore[index]
        )

    def orders_for_refs(self, refs: list[str], since: str | None = None) -> int:
        if not refs:
            return 0
        marks = ",".join("?" * len(refs))
        sql = f"SELECT COUNT(*) AS n FROM orders WHERE product_ref IN ({marks}) AND status != 'refunded'"
        args: tuple = tuple(refs)
        if since:
            sql += " AND occurred_at >= ?"
            args += (since,)
        return int(self._one(sql, args)["n"])  # type: ignore[index]

    def get_order(self, provider: str, order_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM orders WHERE provider = ? AND order_id = ?", (provider, order_id))

    # -- webhooks ---------------------------------------------------------------
    def claim_webhook_event(self, event_id: str, event_type: str) -> bool:
        """Atomically mark an event as seen. False means it was already handled (duplicate/replay)."""
        cur = self._exec(
            "INSERT OR IGNORE INTO webhook_events (event_id, type, status, received_at) VALUES (?,?,?,?)",
            (event_id, event_type, "processing", self.now()),
        )
        return cur.rowcount == 1

    def finish_webhook_event(self, event_id: str, status: str, detail: str = "") -> None:
        self._exec("UPDATE webhook_events SET status = ?, detail = ? WHERE event_id = ?", (status, detail[:500], event_id))

    def release_webhook_event(self, event_id: str) -> None:
        """Forget a claim so Stripe's retry of a failed event is processed again."""
        self._exec("DELETE FROM webhook_events WHERE event_id = ?", (event_id,))

    def webhook_event_counts(self) -> dict[str, int]:
        return {r["status"]: r["n"] for r in self._all("SELECT status, COUNT(*) AS n FROM webhook_events GROUP BY status")}

    def upsert_checkout_session(
        self, session_id: str, product_ref: str | None, payment_intent: str | None, status: str,
        payment_status: str | None, email: str | None, amount_cents: int | None,
        channel: str | None = None, campaign: str | None = None, mode: str | None = None,
    ) -> None:
        self._exec(
            "INSERT INTO checkout_sessions (session_id, product_ref, payment_intent, status, payment_status, email, "
            "amount_cents, updated_at, channel, campaign, mode) VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET "
            "product_ref = COALESCE(excluded.product_ref, product_ref), payment_intent = COALESCE(excluded.payment_intent, payment_intent), "
            "status = excluded.status, payment_status = excluded.payment_status, email = COALESCE(excluded.email, email), "
            "amount_cents = COALESCE(excluded.amount_cents, amount_cents), updated_at = excluded.updated_at, "
            "channel = COALESCE(excluded.channel, channel), campaign = COALESCE(excluded.campaign, campaign), "
            "mode = COALESCE(excluded.mode, mode)",
            (session_id, product_ref, payment_intent, status, payment_status, email, amount_cents, self.now(),
             channel, campaign, mode),
        )

    def sessions_between(self, start: str, end: str) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM checkout_sessions WHERE updated_at >= ? AND updated_at < ?", (start, end))

    def session_for_payment_intent(self, payment_intent: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM checkout_sessions WHERE payment_intent = ?", (payment_intent,))

    def initiations_for_refs(self, refs: list[str], since: str | None = None) -> int:
        if not refs:
            return 0
        marks = ",".join("?" * len(refs))
        sql = f"SELECT COUNT(*) AS n FROM checkout_sessions WHERE product_ref IN ({marks})"
        args: tuple = tuple(refs)
        if since:
            sql += " AND updated_at >= ?"
            args += (since,)
        return int(self._one(sql, args)["n"])  # type: ignore[index]

    # -- price experiments --------------------------------------------------------
    def start_experiment(
        self, asset_id: int, hypothesis_id: int | None, price_cents: int, provider: str,
        product_ref: str | None, checkout_url: str | None, views_at_start: int,
    ) -> int:
        self._exec(
            "UPDATE price_experiments SET status = 'ended', ended_at = ? WHERE asset_id = ? AND status = 'running'",
            (self.now(), asset_id),
        )
        cur = self._exec(
            "INSERT INTO price_experiments (asset_id, hypothesis_id, price_cents, provider, product_ref, checkout_url, "
            "views_at_start, started_at) VALUES (?,?,?,?,?,?,?,?)",
            (asset_id, hypothesis_id, price_cents, provider, product_ref, checkout_url, views_at_start, self.now()),
        )
        return int(cur.lastrowid)

    def running_experiment(self, asset_id: int) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM price_experiments WHERE asset_id = ? AND status IN ('running','converged') ORDER BY id DESC LIMIT 1",
            (asset_id,),
        )

    def experiments_for_hypothesis(self, hypothesis_id: int) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM price_experiments WHERE hypothesis_id = ? ORDER BY id", (hypothesis_id,))

    def update_experiment(self, exp_id: int, **fields: Any) -> None:
        allowed = {"views", "initiations", "status", "reason", "ended_at", "asset_id"}
        if set(fields) - allowed:
            raise ValueError(f"cannot update experiment fields {sorted(set(fields) - allowed)}")
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE price_experiments SET {cols} WHERE id = ?", (*fields.values(), exp_id))

    # -- subscriptions ------------------------------------------------------------
    def upsert_subscriber(
        self, provider: str, subscription_id: str, *, customer_id: str | None = None, email: str | None = None,
        niche: str | None = None, asset_id: int | None = None, hypothesis_id: int | None = None,
        price_cents: int | None = None, interval: str | None = None, status: str | None = None,
        channel: str | None = None, campaign: str | None = None, current_period_end: str | None = None,
    ) -> tuple[int, bool]:
        """Create or update a subscriber. Returns (id, created)."""
        now = self.now()
        with self.tx() as conn:
            row = conn.execute("SELECT id FROM subscribers WHERE subscription_id = ?", (subscription_id,)).fetchone()
            if row is None:
                cur = conn.execute(
                    "INSERT INTO subscribers (provider, subscription_id, customer_id, email, niche, asset_id, hypothesis_id, "
                    "price_cents, interval, subscription_status, channel, campaign, current_period_end, started_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (provider, subscription_id, customer_id, (email or "").lower() or None, niche, asset_id, hypothesis_id,
                     price_cents or 0, interval or "month", status or "active", channel, campaign, current_period_end, now, now),
                )
                return int(cur.lastrowid), True
            fields = {
                "customer_id": customer_id, "email": (email or "").lower() or None, "niche": niche, "asset_id": asset_id,
                "hypothesis_id": hypothesis_id, "price_cents": price_cents, "interval": interval,
                "subscription_status": status, "channel": channel, "campaign": campaign,
                "current_period_end": current_period_end,
            }
            fields = {k: v for k, v in fields.items() if v is not None}
            if status == "canceled":
                fields["canceled_at"] = now
            fields["updated_at"] = now
            cols = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(f"UPDATE subscribers SET {cols} WHERE id = ?", (*fields.values(), row["id"]))
            return int(row["id"]), False

    def get_subscriber(self, subscription_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM subscribers WHERE subscription_id = ?", (subscription_id,))

    def list_subscribers(self, status: str | tuple[str, ...] | None = None, niche: str | None = None,
                         tier: str | None = "paid") -> list[dict[str, Any]]:
        """Subscribers, paid ones by default: MRR, deliveries and traction must never count free
        lead-magnet signups. ``tier="free"`` for those, ``tier=None`` for everyone."""
        sql, args = "SELECT * FROM subscribers WHERE 1=1", []
        if tier:
            sql += " AND tier = ?"
            args.append(tier)
        if status:
            statuses = (status,) if isinstance(status, str) else status
            sql += f" AND subscription_status IN ({','.join('?' * len(statuses))})"
            args += list(statuses)
        if niche:
            sql += " AND niche = ?"
            args.append(niche)
        return self._all(sql + " ORDER BY id", tuple(args))

    # -- free tier (lead magnet) ----------------------------------------------------------
    def capture_free_subscriber(self, email: str, niche: str | None, token: str, channel: str | None = None,
                                campaign: str | None = None, status: str = "pending") -> tuple[int, bool]:
        """One row per email (``subscription_id = free:<email>``). A repeat signup refreshes the
        niche but keeps status, token and history. Returns (id, created)."""
        email = email.lower()
        now = self.now()
        with self.tx() as conn:
            row = conn.execute("SELECT id FROM subscribers WHERE subscription_id = ?", (f"free:{email}",)).fetchone()
            if row is not None:
                if niche:
                    conn.execute("UPDATE subscribers SET niche = ?, updated_at = ? WHERE id = ?", (niche, now, row["id"]))
                return int(row["id"]), False
            cur = conn.execute(
                "INSERT INTO subscribers (provider, subscription_id, email, niche, price_cents, interval, subscription_status, "
                "channel, campaign, started_at, updated_at, tier, token) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("lead_magnet", f"free:{email}", email, niche, 0, "week", status, channel, campaign, now, now, "free", token),
            )
            return int(cur.lastrowid), True

    def subscriber(self, subscriber_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM subscribers WHERE id = ?", (subscriber_id,))

    def subscriber_by_token(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        return self._one("SELECT * FROM subscribers WHERE token = ? AND tier = 'free'", (token,))

    def set_free_status(self, subscriber_id: int, status: str) -> None:
        now = self.now()
        extra = ", confirmed_at = COALESCE(confirmed_at, ?)" if status == "active" else ", canceled_at = ?"
        self._exec(f"UPDATE subscribers SET subscription_status = ?, updated_at = ?{extra} WHERE id = ? AND tier = 'free'",
                   (status, now, now, subscriber_id))

    def free_captures_since(self, since: datetime) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM subscribers WHERE tier = 'free' AND started_at >= ?", (iso(since),))
        return int(row["n"]) if row else 0

    def free_subscriber_counts(self) -> dict[str, int]:
        rows = self._all("SELECT subscription_status AS s, COUNT(*) AS n FROM subscribers WHERE tier = 'free' GROUP BY s")
        return {r["s"]: r["n"] for r in rows}

    def record_subscription_delivery(self, subscriber_id: int, period_key: str, status: str,
                                     delta_count: int = 0, detail: str = "") -> None:
        self._exec(
            "INSERT INTO subscription_deliveries (subscriber_id, period_key, status, delta_count, detail, created_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(subscriber_id, period_key) DO UPDATE SET status = excluded.status, "
            "delta_count = excluded.delta_count, detail = excluded.detail, created_at = excluded.created_at",
            (subscriber_id, period_key, status, delta_count, detail[:500], self.now()),
        )
        if status == "delivered":
            self._exec("UPDATE subscribers SET last_delivered_at = ? WHERE id = ?", (self.now(), subscriber_id))

    def subscription_delivery(self, subscriber_id: int, period_key: str) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM subscription_deliveries WHERE subscriber_id = ? AND period_key = ?", (subscriber_id, period_key)
        )

    def list_subscription_deliveries(self, subscriber_id: int | None = None) -> list[dict[str, Any]]:
        if subscriber_id is None:
            return self._all("SELECT * FROM subscription_deliveries ORDER BY id")
        return self._all("SELECT * FROM subscription_deliveries WHERE subscriber_id = ? ORDER BY id", (subscriber_id,))

    # -- durability ---------------------------------------------------------------
    def checkpoint(self) -> None:
        """Flush the WAL into the main database file (safe shutdown point)."""
        if self.db_path != ":memory:":
            with self._lock:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def order_counts(self) -> dict[str, int]:
        return {r["status"]: r["n"] for r in self._all("SELECT status, COUNT(*) AS n FROM orders GROUP BY status")}

    def purchases_for_hypothesis(self, hypothesis_id: int) -> int:
        return int(
            self._one(
                "SELECT COUNT(*) AS n FROM orders WHERE hypothesis_id = ? AND status != 'refunded'", (hypothesis_id,)
            )["n"]  # type: ignore[index]
        )

    # -- funnel metrics -------------------------------------------------------
    def set_metric(self, hypothesis_id: int, metric: str, source: str, value: int) -> None:
        """Store the latest observed value of a counter (snapshots overwrite; counters never go down)."""
        self._exec(
            "INSERT INTO funnel_metrics (hypothesis_id, metric, source, value, observed_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(hypothesis_id, metric, source) DO UPDATE SET value = MAX(value, excluded.value), "
            "observed_at = excluded.observed_at",
            (hypothesis_id, metric, source, int(value), self.now()),
        )

    def add_metric(self, hypothesis_id: int, metric: str, source: str, delta: int = 1) -> None:
        self._exec(
            "INSERT INTO funnel_metrics (hypothesis_id, metric, source, value, observed_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(hypothesis_id, metric, source) DO UPDATE SET value = value + excluded.value, "
            "observed_at = excluded.observed_at",
            (hypothesis_id, metric, source, int(delta), self.now()),
        )

    def metrics_for_hypothesis(self, hypothesis_id: int) -> dict[str, int]:
        rows = self._all(
            "SELECT metric, SUM(value) AS v FROM funnel_metrics WHERE hypothesis_id = ? GROUP BY metric", (hypothesis_id,)
        )
        return {r["metric"]: int(r["v"]) for r in rows}

    def recent_lead_texts(self, since: datetime) -> list[str]:
        """Title + tags + stack of every distinct lead first seen since ``since`` (all niches)."""
        seen: set[str] = set()
        texts = []
        for r in self._all("SELECT dedupe_key, data FROM leads WHERE first_seen >= ? ORDER BY id", (iso(since),)):
            if r["dedupe_key"] in seen:
                continue
            seen.add(r["dedupe_key"])
            d = json.loads(r["data"])
            texts.append(" ".join([d.get("title", ""), " ".join(d.get("tags") or []), " ".join(d.get("stack") or [])]).lower())
        return texts

    def lead_demand(self, keywords: list[str], since: datetime, texts: list[str] | None = None) -> int:
        """How many distinct leads first seen since ``since`` mention any keyword (whole words, so
        "rust" doesn't match "trust")."""
        texts = self.recent_lead_texts(since) if texts is None else texts
        patterns = [re.compile(rf"(?<![\w+#.]){re.escape(k.lower())}(?![\w+#])") for k in keywords if k]
        return sum(1 for t in texts if any(p.search(t) for p in patterns))

    def revenue_for_hypothesis_since(self, hypothesis_id: int, since: datetime) -> int:
        row = self._one(
            "SELECT COALESCE(SUM(net_cents),0) AS net FROM revenue WHERE hypothesis_id = ? AND verified = 1 AND occurred_at >= ?",
            (hypothesis_id, iso(since)),
        )
        return int(row["net"])  # type: ignore[index]

    def outreach_counts(self) -> dict[str, int]:
        return {r["status"]: r["n"] for r in self._all("SELECT status, COUNT(*) AS n FROM outreach_queue GROUP BY status")}
