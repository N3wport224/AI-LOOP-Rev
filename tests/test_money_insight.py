"""Phases 125-129: forecast, price history, refund-rate guard, abandoned checkouts, niche trends."""

from datetime import timedelta

from strategies.money_insight import (abandoned, describe_abandoned, describe_forecast, describe_trends, forecast,
                                      niche_trends, price_history, refund_problem, refund_rate)
from strategies.owner_reports import OwnerReports
from strategies.owner_todo import todo
from tests import test_business_ops
from tests.test_business_ops import dataset

kit = test_business_ops.kit  # the same live-mode toolkit fixture


def earn(state, clock, ext, net, days_ago, aid=None):
    when = (clock() - timedelta(days=days_ago)).isoformat(timespec="seconds")
    state.record_revenue("stripe", ext, net, 0, net, True, occurred_at=when)
    if aid is not None:
        state.record_order("stripe", ext, f"{ext}@co.example", net, None, aid, None, status="delivered", occurred_at=when)


# ------------------------------------------------------------------ Phase 125: forecast
def test_forecast_projects_the_month_at_the_recent_pace(state, config, clock):
    # NOW is 30 Sep 12:00: half a day of September left
    earn(state, clock, "a", 14_000, 3)    # 14 days: $140 -> $10/day
    earn(state, clock, "b", 5_000, 20)    # earlier this month
    f = forecast(state, config)
    assert f["month"] == "September" and f["mtd_cents"] == 19_000 and f["per_day_cents"] == 1000
    assert f["projected_cents"] == 19_500 and f["goal_cents"] == 30_000 and not f["on_track"]
    assert describe_forecast(f).endswith("against a $300.00 goal: $105.00 short.")


# ------------------------------------------------------------------ Phase 126: price history
def test_price_changes_are_recorded(kit, state):
    _, aid = dataset(kit, state, "python-remote", price=1900)
    state.update_asset(aid, price_cents=1900)  # unchanged: nothing recorded
    state.update_asset(aid, price_cents=2400)
    state.update_asset(aid, status="published")
    rows = price_history(state)
    assert len(rows) == 1 and rows[0]["old_cents"] == 1900 and rows[0]["new_cents"] == 2400
    assert rows[0]["title"] == "python-remote Tech Stack Intel"


# ------------------------------------------------------------------ Phase 127: refunds
def test_a_high_refund_rate_goes_on_the_todo_list(kit, state, config, clock):
    _, aid = dataset(kit, state, "python-remote")
    for i in range(4):
        state.record_order("stripe", f"ok{i}", f"a{i}@co.example", 1900, None, aid, None, status="delivered")
    state.record_order("stripe", "r1", "r@co.example", 1900, None, aid, None, status="refunded")
    assert refund_rate(state) == {"orders": 5, "refunded": 1, "rate": 0.2}
    assert refund_problem(state, config)["refunded"] == 1
    assert any(i["title"] == "Look into refunds" for i in todo(state, config))
    config.refund_alert_rate = 0.25
    assert refund_problem(state, config) is None


# ------------------------------------------------------------------ Phases 128-129: Monday lines
def test_abandoned_checkouts_and_niche_trends(kit, state, config, clock):
    state.upsert_checkout_session("cs_1", "plink", None, "expired", "unpaid", "a@co.example", 1900)
    state.upsert_checkout_session("cs_2", "plink", None, "open", "unpaid", None, 2900)  # just opened: not abandoned yet
    state.upsert_checkout_session("cs_3", "plink", None, "complete", "paid", "b@co.example", 1900)
    assert abandoned(state) == {"checkouts": 1, "cents": 1900}
    assert "Abandoned checkouts (last 7 days): 1, worth $19.00" in describe_abandoned(abandoned(state))
    _, py = dataset(kit, state, "python-remote")
    _, ml = dataset(kit, state, "ml-ai")
    state.update_asset(py, niche="python-remote")
    state.update_asset(ml, niche="ml-ai")
    earn(state, clock, "p1", 3000, 2, py)
    earn(state, clock, "p0", 2000, 20, py)
    earn(state, clock, "m0", 4000, 20, ml)
    trends = {t["niche"]: t for t in niche_trends(state)}
    assert trends["python-remote"]["change_pct"] == 50 and trends["ml-ai"]["change_pct"] == -100
    assert describe_trends(niche_trends(state)).startswith("Last 14 days by niche vs the 14 before: python-remote $30.00 (+50%)")
    body = OwnerReports.digest_body(kit, clock().replace(day=28))  # a Monday
    assert "Abandoned checkouts" in body and "Last 14 days by niche" in body and "September so far" in body
