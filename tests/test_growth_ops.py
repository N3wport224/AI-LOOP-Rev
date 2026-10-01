"""Phases 20-23: share kit, refunds & disputes, buyer follow-up, monthly bookkeeping."""

import csv
import io

import pytest

from strategies.bookkeeping import Bookkeeping, build_books, previous_month
from strategies.buyer_followup import BuyerFollowup
from strategies.owner_reports import OwnerReports
from strategies.refunds import DISPUTE_FEE_CENTS, Refunds
from strategies.share_kit import ShareKit, as_text, build_kit
from strategies.support_desk import SupportDesk
from tests import test_business_ops
from tests.test_business_ops import STRIPE, ctx, dataset
from tests.test_distribution import FakeSMTP

kit = test_business_ops.kit  # the same live-mode toolkit fixture
T = 1790000000  # 2026-09-21


def sale(state, aid, order_id="cs_1", email="buyer@co.example", status="delivered", delivered_days_ago=4, clock=None):
    state.record_order("stripe", order_id, email, 1900, "plink_python-remote", aid, None, status=status)
    state.record_revenue("stripe", order_id, 1900, 85, 1815, True, product_ref="plink_python-remote")
    if status == "delivered" and clock is not None:
        when = (clock() - __import__("datetime").timedelta(days=delivered_days_ago)).isoformat(timespec="seconds")
        state._exec("UPDATE orders SET delivered_at = ? WHERE order_id = ?", (when, order_id))
    return state.get_order("stripe", order_id)


# ------------------------------------------------------------------ Phase 21: refunds & disputes
def stripe_feeds(transport, refunds=(), disputes=()):
    transport.add_json(f"{STRIPE}/refunds", {"data": list(refunds)})
    transport.add_json(f"{STRIPE}/disputes", {"data": list(disputes)})
    transport.add_json(f"{STRIPE}/checkout/sessions", {"data": [{"id": "cs_1"}]})


def net_total(state):
    return int(state._one("SELECT COALESCE(SUM(net_cents), 0) AS n FROM revenue")["n"])


def test_a_refund_is_a_negative_row_once_and_marks_the_order(kit, state, transport):
    _, aid = dataset(kit, state, "python-remote")
    order = sale(state, aid)
    stripe_feeds(transport, refunds=[{"id": "re_1", "amount": 1900, "status": "succeeded", "payment_intent": "pi_1", "created": T}])
    res = Refunds().run("sync_refunds", ctx(kit))
    assert res.metrics == {"refunds": 1, "disputes": 0}
    assert net_total(state) == 1815 - 1900
    assert state.get_order("stripe", "cs_1")["status"] == "refunded"
    assert state.orders_for_refs(["plink_python-remote"]) == 0  # no longer counts as a sale
    assert Refunds().run("sync_refunds", ctx(kit)).metrics["refunds"] == 0  # idempotent
    assert net_total(state) == 1815 - 1900 and order["id"]


def test_the_local_checkout_session_is_used_before_asking_stripe(kit, state, transport):
    _, aid = dataset(kit, state, "python-remote")
    sale(state, aid, order_id="cs_local")
    state.upsert_checkout_session("cs_local", "plink_python-remote", "pi_9", "complete", "paid", "buyer@co.example", 1900)
    stripe_feeds(transport, refunds=[{"id": "re_9", "amount": 500, "status": "succeeded", "payment_intent": "pi_9", "created": T}])
    Refunds().run("sync_refunds", ctx(kit))
    assert state.get_order("stripe", "cs_local")["status"] == "refunded"
    assert not transport.calls_to(f"{STRIPE}/checkout/sessions")


def test_a_dispute_costs_the_fee_alerts_and_a_win_comes_back(kit, state, transport):
    _, aid = dataset(kit, state, "python-remote")
    sale(state, aid)
    dispute = {"id": "dp_1", "amount": 1900, "reason": "fraudulent", "status": "needs_response", "payment_intent": "pi_1",
               "created": T, "evidence_details": {"due_by": T + 7 * 86400}}
    stripe_feeds(transport, disputes=[dispute])
    assert Refunds().run("sync_refunds", ctx(kit)).metrics["disputes"] == 1
    assert net_total(state) == 1815 - 1900 - DISPUTE_FEE_CENTS
    assert state.get_order("stripe", "cs_1")["status"] == "disputed"
    alert = state.recent_errors(1, kind="alert")[0]
    assert "fraudulent" in alert["message"] and "2026-09-28" in alert["message"]
    assert Refunds().run("sync_refunds", ctx(kit)).metrics["disputes"] == 0  # nothing new
    stripe_feeds(transport, disputes=[{**dispute, "status": "won"}])
    assert Refunds().run("sync_refunds", ctx(kit)).metrics["disputes"] == 1
    assert net_total(state) == 1815 - DISPUTE_FEE_CENTS  # the amount came back; Stripe keeps the fee
    assert state.count_errors("alert") == 1


def test_refunds_need_a_stripe_key(kit, config):
    config.stripe_secret_key = ""
    assert Refunds().run("sync_refunds", ctx(kit)).summary == "no Stripe key"


# ------------------------------------------------------------------ Phase 20: share kit
def test_share_kit_has_four_tracked_posts_per_product(kit, state):
    dataset(kit, state, "python-remote")
    kit.files.write_json("exports/intel/python-remote/tech_radar.json", [
        {"company": "A", "openings": 3, "urgency_score": 80, "intent_signals": ["series_b"]},
        {"company": "B", "openings": 2, "urgency_score": 10, "intent_signals": []}])
    data = build_kit(state, kit.files)
    assert [p["channel"] for p in data["posts"]] == ["linkedin", "x", "reddit", "dm"]
    for p in data["posts"]:
        assert "client_reference_id=" in p["link"] and p["channel"] in p["link"] and p["link"] in p["text"]
    x = data["posts"][1]["text"]
    assert len(x) <= 280 and "2 companies hiring right now" in x and "$19" in x
    assert "series b" in data["posts"][0]["text"]
    assert "self-promotion" in data["posts"][2]["text"]


def test_share_kit_uses_the_lander_and_never_invents_numbers(kit, state):
    _, aid = dataset(kit, state, "python-remote")
    state.update_asset(aid, lander_url="https://me.github.io/datasets/python-remote/")
    posts = build_kit(state, kit.files)["posts"]
    assert all("utm_source=" + p["channel"] in p["link"] and p["link"].startswith("https://me.github.io/") for p in posts)
    assert not any(ch.isdigit() for ch in posts[1]["text"].split("http")[0].replace("$19", ""))
    assert "Nothing to share" in as_text({"posts": []})


def test_share_kit_task_rebuilds_on_change_and_weekly(kit, state, clock):
    assert ShareKit().run("refresh_share_kit", ctx(kit)).metrics["posts"] == 0
    assert ShareKit().run("refresh_share_kit", ctx(kit)).summary == "share kit up to date"
    dataset(kit, state, "python-remote")
    assert ShareKit().run("refresh_share_kit", ctx(kit)).metrics["posts"] == 4
    assert ShareKit().run("refresh_share_kit", ctx(kit)).summary == "share kit up to date"
    clock.advance(days=8)
    assert "ready" in ShareKit().run("refresh_share_kit", ctx(kit)).summary


def test_the_monday_digest_carries_the_share_kit(kit, state, clock):
    from datetime import datetime, timezone

    dataset(kit, state, "python-remote")
    ShareKit().run("refresh_share_kit", ctx(kit))
    wednesday = OwnerReports.digest_body(kit, clock().astimezone(timezone.utc))
    assert "control panel → Share" in wednesday and "share kit" not in wednesday
    monday = OwnerReports.digest_body(kit, datetime(2026, 10, 5, 9, tzinfo=timezone.utc))
    assert "This week's share kit" in monday and "--- LinkedIn" in monday


# ------------------------------------------------------------------ Phase 22: buyer follow-up
def test_buyers_get_one_followup_after_three_days(kit, state, clock):
    _, aid = dataset(kit, state, "python-remote")
    sale(state, aid, "cs_old", "old@co.example", delivered_days_ago=12, clock=clock)
    sale(state, aid, "cs_new", "new@co.example", delivered_days_ago=1, clock=clock)
    sale(state, aid, "cs_due", "due@co.example", delivered_days_ago=4, clock=clock)
    sale(state, aid, "cs_ref", "ref@co.example", status="refunded")
    sale(state, aid, "cs_sup", "gone@co.example", delivered_days_ago=5, clock=clock)
    state.suppress("gone@co.example", "test")
    before = len(FakeSMTP.sent)
    assert BuyerFollowup().run("follow_up_buyers", ctx(kit)).metrics["sent"] == 1
    msg = FakeSMTP.sent[-1]
    assert len(FakeSMTP.sent) == before + 1 and msg["To"] == "due@co.example"
    body = msg.get_content() if not msg.is_multipart() else msg.get_body(("plain",)).get_content()
    assert "python-remote Tech Stack Intel" in body and "unsubscribe" in body and "1 Main St" in body
    assert "mailto:" in msg["List-Unsubscribe"]
    assert BuyerFollowup().run("follow_up_buyers", ctx(kit)).metrics["sent"] == 0  # once per order
    clock.advance(days=3)  # the 1-day-old order is now due
    assert BuyerFollowup().run("follow_up_buyers", ctx(kit)).metrics["sent"] == 1


def test_followup_waits_for_a_postal_address_and_can_be_switched_off(kit, state, clock, config):
    _, aid = dataset(kit, state, "python-remote")
    sale(state, aid, delivered_days_ago=4, clock=clock)
    config.sender_postal_address = ""
    assert "postal" in BuyerFollowup().run("follow_up_buyers", ctx(kit)).summary
    config.buyer_followup = False
    assert BuyerFollowup().run("follow_up_buyers", ctx(kit)).summary == "buyer follow-up off"


def test_an_unsubscribe_reply_suppresses_the_buyer(kit, state):
    _, aid = dataset(kit, state, "python-remote")
    sale(state, aid)
    inbox = [{"sender": "buyer@co.example", "subject": "Re: Quick check", "message_id": "<u1>",
              "body": "Please unsubscribe me.\n> Don't want emails like this? Reply \"unsubscribe\"."}]
    res = SupportDesk(scan=lambda *a: inbox).run("answer_support", ctx(kit))
    assert res.metrics["unsubscribed"] == 1 and res.metrics["alerted"] == 0
    assert state.is_suppressed("buyer@co.example")


# ------------------------------------------------------------------ Phase 23: bookkeeping
def test_books_list_last_months_rows_with_totals(kit, state, config):
    config.subscription_timezone = "UTC"
    state.record_revenue("stripe", "cs_a", 1900, 85, 1815, True, "2026-09-03T10:00:00+00:00", product_ref="plink_x", note="")
    state.record_revenue("stripe", "refund:re_1", -1900, 0, -1900, True, "2026-09-04T10:00:00+00:00", note="refund of cs_a")
    state.record_revenue("stripe", "cs_b", 1000, 59, 941, True, "2026-10-01T00:30:00+00:00")  # next month
    state.record_revenue("manual", "unverified", 500, 0, 500, False, "2026-09-05T10:00:00+00:00")
    text, totals = build_books(state, config, "2026-09")
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0][:3] == ["date", "source", "reference"] and len(rows) == 4
    assert rows[1][2] == "cs_a" and rows[2][3] == "refund of cs_a" and rows[2][6] == "-19.00"
    assert rows[-1][0] == "TOTAL" and rows[-1][6] == "-0.85" and totals["net_cents"] == -85


def test_monthly_books_are_saved_and_emailed_once(kit, state, config, clock):
    from datetime import datetime, timezone

    config.subscription_timezone, config.owner_email = "UTC", "owner@me.example"
    state.record_revenue("stripe", "cs_a", 1900, 85, 1815, True, "2026-09-03T10:00:00+00:00")
    assert previous_month(clock()) == "2026-08"
    clock.now = datetime(2026, 10, 1, 6, tzinfo=timezone.utc)
    res = Bookkeeping().run("monthly_books", ctx(kit))
    assert res.metrics["rows"] == 1 and kit.files.exists("exports/books/2026-09.csv")
    msg = FakeSMTP.sent[-1]
    assert msg["To"] == "owner@me.example" and "2026-09" in msg["Subject"] and "$18.15" in msg["Subject"]
    assert [p.get_filename() for p in msg.iter_attachments()] == ["automonetize-2026-09.csv"]
    assert Bookkeeping().run("monthly_books", ctx(kit)).summary == "books for 2026-09 done"


def test_books_wait_for_the_first_sale(kit, state):
    assert "no revenue yet" in Bookkeeping().run("monthly_books", ctx(kit)).summary
    assert "done" in Bookkeeping().run("monthly_books", ctx(kit)).summary


@pytest.mark.parametrize("task", ["sync_refunds", "refresh_share_kit", "follow_up_buyers", "monthly_books"])
def test_new_tasks_are_planned_and_handled(config, state, toolkit, task):
    from agent.engine import PLAN, Engine

    engine = Engine(config, state=state, sleep=lambda s: None)
    assert task in dict(PLAN) and task in engine.handlers
    assert engine.breaker.max_actions_per_cycle >= len(PLAN) + 10


def test_cli_and_gui_entry_points(kit, state, monkeypatch, capsys):
    from dashboard.cli import build_parser

    p = build_parser()
    assert p.parse_args(["share"]).func.__name__ == "cmd_share"
    assert p.parse_args(["books", "2026-09"]).month == "2026-09"
    from gui.routes.share import routes

    assert [r.path for r in routes()][:2] == ["/api/share", "/api/testimonials"]


def test_stripe_errors_are_logged_not_raised(kit, state, transport):
    transport.add_json(f"{STRIPE}/refunds", {"error": {"message": "permission"}}, status=403)
    res = Refunds().run("sync_refunds", ctx(kit))
    assert res.ok and "couldn't read" in res.summary and state.get("refunds_synced_at") is None
