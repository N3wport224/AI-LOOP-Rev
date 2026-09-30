"""Persistent agent state in an isolated SQLite database."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

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
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class StateStore:
    def __init__(self, db_path: str | Path, clock: Callable[[], datetime] = utc_now):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.db_path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def now(self) -> str:
        return iso(self.clock())

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    def _all(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def _one(self, sql: str, args: tuple = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(sql, args).fetchone()
            return dict(row) if row else None

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, args)

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

    def outreach_counts(self) -> dict[str, int]:
        return {r["status"]: r["n"] for r in self._all("SELECT status, COUNT(*) AS n FROM outreach_queue GROUP BY status")}
