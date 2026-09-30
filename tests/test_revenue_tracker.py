import pytest

from tests.conftest import FakeTransport
from tools.circuit_breaker import RateLimiter
from tools.http_client import HttpClient, Response
from tools.revenue_tracker import RevenueTracker


def gumroad_tracker(state, transport):
    http = HttpClient(RateLimiter(6000, burst=100), transport=transport, respect_robots=False, backoff_seconds=0, sleep=lambda s: None)
    return RevenueTracker(state, 1000, http=http, gumroad_token="tok", fee_pct=10, fee_fixed_cents=50)


def test_fee_estimate():
    t = RevenueTracker(state=None, fee_pct=10, fee_fixed_cents=50)
    assert t.compute_net(900) == (140, 760)
    assert t.compute_net(30) == (30, 0)  # fee never exceeds gross
    assert t.compute_net(0) == (0, 0)


def test_manual_entries_verified_flag(state):
    t = RevenueTracker(state, 1000)
    t.record_manual(1200, verified=False, note="unconfirmed")
    s = t.daily_summary()
    assert s["net_cents"] == 0 and s["unverified_cents"] == 1200 and not s["target_met"]
    t.record_manual(1000, verified=True, external_id="bank-1")
    assert not t.record_manual(1000, verified=True, external_id="bank-1")
    s = t.daily_summary()
    assert s["net_cents"] == 1000 and s["target_met"] and s["progress"] == pytest.approx(1.0)


def test_gumroad_sync_records_verified_and_attributes(state, make_hypothesis):
    hyp = make_hypothesis()
    aid = state.add_asset(hyp["id"], "lead_directory", "T", "p.zip", 1, 10, 900)
    state.link_product(aid, "prodA")
    t = FakeTransport()
    t.add(
        "https://api.gumroad.com/v2/sales",
        [
            Response(200, "u", b'{"success": true, "next_page_key": "p2", "sales": ['
                     b'{"id": "s1", "price": 900, "product_id": "prodA", "created_at": "2026-09-30T08:00:00Z"},'
                     b'{"id": "s2", "price": 900, "product_id": "prodA", "refunded": true, "created_at": "2026-09-30T09:00:00Z"}]}'),
            Response(200, "u", b'{"success": true, "sales": ['
                     b'{"id": "s3", "price": 1500, "product_id": "other", "created_at": "2026-09-30T10:00:00Z"}]}'),
        ],
    )
    tracker = gumroad_tracker(state, t)
    rep = tracker.sync_gumroad()
    assert (rep.fetched, rep.new, rep.skipped) == (3, 2, 1)
    assert rep.net_cents_added == 760 + 1300
    assert "page_key=p2" in t.calls[1]["url"]
    assert state.revenue_for_hypothesis(hyp["id"]) == 760
    assert tracker.daily_summary()["net_cents"] == 2060
    assert tracker.daily_summary()["target_met"]

    # Idempotent re-sync
    t.add("https://api.gumroad.com/v2/sales", Response(200, "u", b'{"success": true, "sales": [{"id": "s1", "price": 900}]}'))
    assert tracker.sync_gumroad().new == 0


def test_gumroad_failure_payload_raises(state):
    t = FakeTransport()
    t.add_json("https://api.gumroad.com/v2/sales", {"success": False, "message": "bad token"})
    with pytest.raises(RuntimeError):
        gumroad_tracker(state, t).sync_gumroad()


def test_sync_noop_without_token(state):
    assert RevenueTracker(state).sync_gumroad().fetched == 0


def test_history_has_one_row_per_day(state):
    rows = RevenueTracker(state).history(7)
    assert len(rows) == 7 and rows[-1]["date"] == "2026-09-30"
