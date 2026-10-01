"""Expenses (Phase 85): what the business spends, so the books show profit, not just revenue.

``automonetize expense add 12.00 "domain renewal" [--date 2026-09-03] [--category tools]`` records
one; ``expense list [YYYY-MM]`` and ``expense delete <id>`` manage them. Monthly and yearly books
(``strategies/bookkeeping.py``) list them and subtract them. Stripe's own fees are already in the
revenue rows; don't enter those here. Table ``expenses``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

_SCHEMA = """CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    spent_on TEXT NOT NULL,          -- YYYY-MM-DD (your local date)
    amount_cents INTEGER NOT NULL,
    description TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'general',
    created_at TEXT NOT NULL
)"""


def ensure(state: Any) -> None:
    state._exec(_SCHEMA)


def parse_amount(text: str) -> int:
    value = float(str(text).replace("$", "").replace(",", "").strip())
    if value <= 0 or value > 1_000_000:
        raise ValueError("amount must be between 0.01 and 1,000,000")
    return round(value * 100)


def add(state: Any, amount_cents: int, description: str, spent_on: str, category: str = "general") -> int:
    ensure(state)
    datetime.strptime(spent_on, "%Y-%m-%d")  # validates
    if not description.strip():
        raise ValueError("describe the expense")
    cur = state._exec("INSERT INTO expenses (spent_on, amount_cents, description, category, created_at) VALUES (?,?,?,?,?)",
                      (spent_on, int(amount_cents), description.strip()[:200], (category or "general").strip()[:40], state.now()))
    return int(cur.lastrowid)


def between(state: Any, first_day: str, end_day: str) -> list[dict[str, Any]]:
    """Expenses with first_day <= spent_on < end_day (YYYY-MM-DD strings)."""
    ensure(state)
    return state._all("SELECT * FROM expenses WHERE spent_on >= ? AND spent_on < ? ORDER BY spent_on, id", (first_day, end_day))


def delete(state: Any, expense_id: int) -> bool:
    ensure(state)
    return state._exec("DELETE FROM expenses WHERE id = ?", (int(expense_id),)).rowcount == 1
