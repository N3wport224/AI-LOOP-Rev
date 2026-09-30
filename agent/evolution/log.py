"""The evolution audit log: ``data/evolution_log.db``.

Kept apart from the agent's state database so the full record of every self-modification
survives a state reset, and so it can be inspected with any SQLite tool::

    sqlite3 data/evolution_log.db "SELECT id, created_at, status, title, commit_sha FROM attempts ORDER BY id DESC"

Tables:

* ``attempts``: one row per attempt: hypothesis, diff, outcome
  (``running`` · ``rejected`` · ``failed`` · ``aborted`` · ``merged`` · ``rolled_back``), the branch, the
  commit, and the tail of the test output.
* ``blacklist``: patch fingerprints never to be applied again (rejected, failed or rolled back).
* ``kv``: cooldown, canary, halt flag, last diagnosis.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    metric TEXT NOT NULL DEFAULT '',
    baseline REAL,
    expected REAL,
    fingerprint TEXT NOT NULL,
    files TEXT NOT NULL DEFAULT '[]',
    diff TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running',
    branch TEXT,
    base_sha TEXT,
    commit_sha TEXT,
    detail TEXT NOT NULL DEFAULT '',
    output TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_attempts_fp ON attempts (fingerprint);
CREATE TABLE IF NOT EXISTS blacklist (
    fingerprint TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    attempt_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

OUTPUT_LIMIT = 20_000


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EvolutionLog:
    def __init__(self, path: str | Path, clock: Callable[[], datetime] = utcnow):
        self.path = Path(path)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        # A short-lived connection per call: the engine thread, the supervisor loop and the GUI
        # all read this file, and none of them needs to hold it open.
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def now(self) -> str:
        return self.clock().astimezone(timezone.utc).isoformat(timespec="seconds")

    # -- attempts ---------------------------------------------------------------------------
    def start(self, hyp: Any, branch: str = "", base_sha: str = "") -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO attempts (created_at, kind, title, rationale, metric, baseline, expected, fingerprint, files, diff, "
                "branch, base_sha) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.now(), hyp.kind, hyp.title, hyp.rationale, hyp.metric, hyp.baseline, hyp.expected, hyp.fingerprint,
                 json.dumps([ch.path for ch in hyp.changes]), hyp.diff(), branch, base_sha))
            return int(cur.lastrowid)

    def finish(self, attempt_id: int, status: str, detail: str = "", output: str = "", commit_sha: str | None = None) -> None:
        with self._conn() as c:
            c.execute("UPDATE attempts SET status = ?, finished_at = ?, detail = ?, output = ?, commit_sha = COALESCE(?, commit_sha) "
                      "WHERE id = ?", (status, self.now(), detail[:4000], output[-OUTPUT_LIMIT:], commit_sha, attempt_id))

    def set_status(self, attempt_id: int, status: str, detail: str | None = None) -> None:
        with self._conn() as c:
            if detail is None:
                c.execute("UPDATE attempts SET status = ? WHERE id = ?", (status, attempt_id))
            else:
                c.execute("UPDATE attempts SET status = ?, detail = ? WHERE id = ?", (status, detail[:4000], attempt_id))

    def get(self, attempt_id: int) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
        return dict(row) if row else None

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM attempts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def tried(self, fingerprint: str) -> bool:
        """This exact patch was already attempted (whatever the outcome) or blacklisted."""
        with self._conn() as c:
            hit = c.execute("SELECT 1 FROM attempts WHERE fingerprint = ? AND status != 'aborted' LIMIT 1", (fingerprint,)).fetchone()
            return hit is not None or self.blacklisted(fingerprint, c)

    # -- blacklist --------------------------------------------------------------------------
    def blacklist(self, fingerprint: str, reason: str, attempt_id: int | None = None) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO blacklist (fingerprint, reason, attempt_id, created_at) VALUES (?,?,?,?)",
                      (fingerprint, reason[:500], attempt_id, self.now()))

    def blacklisted(self, fingerprint: str, conn: sqlite3.Connection | None = None) -> bool:
        if conn is not None:
            return conn.execute("SELECT 1 FROM blacklist WHERE fingerprint = ?", (fingerprint,)).fetchone() is not None
        with self._conn() as c:
            return c.execute("SELECT 1 FROM blacklist WHERE fingerprint = ?", (fingerprint,)).fetchone() is not None

    # -- kv ------------------------------------------------------------------------------------
    def get_kv(self, key: str, default: Any = None) -> Any:
        with self._conn() as c:
            row = c.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_kv(self, key: str, value: Any) -> None:
        with self._conn() as c:
            if value is None:
                c.execute("DELETE FROM kv WHERE key = ?", (key,))
            else:
                c.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, json.dumps(value, default=str)))

    def cooldown_until(self) -> datetime | None:
        raw = self.get_kv("cooldown_until")
        until = datetime.fromisoformat(raw) if raw else None
        return until if until and until > self.clock() else None

    def enter_cooldown(self, hours: float, reason: str) -> datetime:
        until = self.clock() + timedelta(hours=hours)
        self.set_kv("cooldown_until", until.isoformat(timespec="seconds"))
        self.set_kv("cooldown_reason", reason[:300])
        return until

    def halted(self) -> str | None:
        return self.get_kv("halted")

    # -- dashboards ------------------------------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        with self._conn() as c:
            counts = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) AS n FROM attempts GROUP BY status")}
            last = c.execute("SELECT id, title, commit_sha, finished_at FROM attempts WHERE status IN ('merged', 'rolled_back') "
                             "AND commit_sha IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
            latest = c.execute("SELECT id, status, title, created_at, detail FROM attempts ORDER BY id DESC LIMIT 1").fetchone()
            blacklisted = c.execute("SELECT COUNT(*) AS n FROM blacklist").fetchone()["n"]
        cooldown = self.cooldown_until()
        canary = self.get_kv("canary") or {}
        return {
            "attempted": sum(n for s, n in counts.items() if s != "aborted"),
            "merged": counts.get("merged", 0) + counts.get("rolled_back", 0),
            "active": counts.get("merged", 0),
            "rolled_back": counts.get("rolled_back", 0),
            "failed": counts.get("failed", 0),
            "rejected": counts.get("rejected", 0),
            "aborted": counts.get("aborted", 0),
            "blacklisted": int(blacklisted),
            "last_commit": dict(last) if last else None,
            "latest": dict(latest) if latest else None,
            "cooldown_until": cooldown.isoformat(timespec="seconds") if cooldown else None,
            "cooldown_seconds": max(0, int((cooldown - self.clock()).total_seconds())) if cooldown else 0,
            "cooldown_reason": self.get_kv("cooldown_reason") if cooldown else None,
            "canary": {k: canary.get(k) for k in ("status", "attempt_id", "commit_sha", "until", "reason")} if canary else None,
            "halted": self.halted(),
            "last_diagnosis": self.get_kv("last_diagnosis"),
        }
