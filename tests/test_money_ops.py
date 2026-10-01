"""Phases 85-89: expenses, profit and tax set-aside, year-end books, payout watch, goal ladder."""

import csv
import io
from datetime import datetime, timezone

import pytest

from strategies.bookkeeping import Bookkeeping, build_books, period_bounds
from strategies.goal_pacing import ladder, next_goal
from strategies.owner_todo import todo
from strategies.payout_watch import PayoutWatch
from tests import test_business_ops
from tests.test_business_ops import ctx
from tests.test_distribution import FakeSMTP
from tools import expenses

kit = test_business_ops.kit  # the same live-mode toolkit fixture


# ------------------------------------------------------------------ Phase 85: expenses
def test_expenses_are_validated_and_listed(state):
    eid = expenses.add(state, expenses.parse_amount("$12.50"), "domain renewal", "2026-09-03", "tools")
    assert eid and expenses.between(state, "2026-09-01", "2026-10-01")[0]["amount_cents"] == 1250
    for bad in ("0", "-5", "abc"):
        with pytest.raises(ValueError):
            expenses.parse_amount(bad)
    with pytest.raises(ValueError):
        expenses.add(state, 100, "x", "2026-13-40")
    assert expenses.delete(state, eid) and not expenses.delete(state, eid)


# ------------------------------------------------------------------ Phase 86: profit and tax set-aside
def test_books_show_expenses_profit_and_tax(state, config):
    config.subscription_timezone, config.tax_set_aside_pct = "UTC", 25
    state.record_revenue("stripe", "cs_a", 5000, 175, 4825, True, "2026-09-03T10:00:00+00:00")
    expenses.add(state, 825, "domain", "2026-09-10")
    expenses.add(state, 999, "next month", "2026-10-02")
    text, totals = build_books(state, config, "2026-09")
    assert totals["expense_cents"] == 825 and totals["profit_cents"] == 4000 and totals["tax_set_aside_cents"] == 1000
    rows = list(csv.reader(io.StringIO(text)))
    assert ["EXPENSES", "", "", "1 entries", "", "", "-8.25"] in rows
    assert any(r[:1] == ["PROFIT"] and r[-1] == "40.00" for r in rows)
    assert any(r[:1] == ["SET ASIDE FOR TAX"] and r[-1] == "10.00" for r in rows)
    state.record_revenue("stripe", "refund:x", -9000, 0, -9000, True, "2026-09-04T10:00:00+00:00")
    assert build_books(state, config, "2026-09")[1]["tax_set_aside_cents"] == 0  # a loss: nothing to set aside


# ------------------------------------------------------------------ Phase 87: year-end books
def test_year_bounds_and_the_january_summary(kit, state, config, clock):
    config.subscription_timezone, config.owner_email = "UTC", "owner@me.example"
    start, end = period_bounds("2026", datetime.now(timezone.utc).tzinfo)
    assert (start.month, start.day, end.year) == (1, 1, 2027)
    state.record_revenue("stripe", "cs_a", 1900, 85, 1815, True, "2026-03-03T10:00:00+00:00")
    state.record_revenue("stripe", "cs_b", 1900, 85, 1815, True, "2026-12-20T10:00:00+00:00")
    state.set("books_month", "2026-11")
    clock.now = datetime(2027, 1, 2, 9, tzinfo=timezone.utc)
    res = Bookkeeping().run("monthly_books", ctx(kit))
    assert res.metrics["periods"] == ["2026-12", "2026"]
    subjects = [m["Subject"] for m in FakeSMTP.sent[-2:]]
    assert subjects[0].startswith("📒 Books for 2026-12") and subjects[1] == "📒 Year-end books for 2026: $36.30 profit"
    assert kit.files.exists("exports/books/2026.csv")
    assert Bookkeeping().run("monthly_books", ctx(kit)).summary.startswith("books for 2026-12 done")


# ------------------------------------------------------------------ Phase 88: payout watch
def test_failed_payouts_and_idle_money_alert(kit, state, clock):
    assert PayoutWatch().run("watch_payouts", ctx(kit)).summary == "no Stripe balance read yet"
    state.set("stripe_finance", {"available_cents": 5000, "pending_cents": 0,
                                 "last_payout": {"amount_cents": 3000, "status": "failed", "arrival_date": "2026-08-01"}})
    res = PayoutWatch().run("watch_payouts", ctx(kit))
    assert res.metrics["notes"] == 2
    messages = " | ".join(e["message"] for e in state.recent_errors(5, kind="alert"))
    assert "payout to your bank failed" in messages and "ready in Stripe for 60 days" in messages
    PayoutWatch().run("watch_payouts", ctx(kit))
    assert len(state.recent_errors(5, kind="alert")) == 2  # each once (idle money: weekly)


# ------------------------------------------------------------------ Phase 89: goal ladder
def test_a_week_on_goal_suggests_a_higher_one(state, config):
    assert next_goal(1000) == 1500 and next_goal(1500) == 2500
    for _ in range(6):
        ladder(state, config, {"progress": 1.1})
    assert state.get("goal_suggestion") is None
    ladder(state, config, {"progress": 1.0})
    assert state.get("goal_suggestion")["cents"] == 1500
    assert any(i["title"] == "Raise your daily goal to $15" and i["how"] == "automonetize goal 15" for i in todo(state, config))
    ladder(state, config, {"progress": 0.4})
    assert state.get("goal_suggestion") is None and state.get("goal_streak") == 0


def test_cli_entry_points(kit, state, config, monkeypatch, tmp_path):
    import cli.growth
    from dashboard.cli import build_parser

    p = build_parser()
    assert p.parse_args(["expense", "list"]).args == ["list"] and p.parse_args(["goal", "15"]).dollars == "15"
    assert p.parse_args(["books", "2026"]).month == "2026"
    monkeypatch.setattr(cli.growth, "_setup", lambda: (config, state, kit.files))
    assert cli.growth.expense_main(["add", "9.99", "hosting", "--date", "2026-09-05"]) == 0
    assert cli.growth.expense_main(["add", "nope", "x"]) == 1
    assert cli.growth.books_main(["2026"]) == 0 and cli.growth.books_main(["26"]) == 1
