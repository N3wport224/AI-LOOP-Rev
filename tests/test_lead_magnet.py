"""Free lead magnet: capture, sample delivery, double opt-in, weekly Tech Pulse, public endpoints."""

import asyncio
import csv
import io
import json
from datetime import timedelta

import pytest

from agent.power import NullBackend, PowerManager
from strategies.base import TaskContext
from strategies.lead_magnet import (
    CaptureLimiter, LeadEndpoints, LeadMagnet, capture, confirm, normalize_email, pulse_signals, send_sample, unsubscribe,
)
from tests.conftest import NOW
from tests.test_distribution import FakeSMTP, go_live
from tools import build_toolkit

NICHE = "python-remote"


def radar_records(n=12):
    out = []
    for i in range(n):
        score = 90 - i * 7 if i < 10 else 0
        out.append({
            "company": f"Co{i:02d}", "domain": f"co{i}.example", "stack": ["Python", "AWS", "PostgreSQL"],
            "openings": 1 + i % 3, "intent_score": max(score, 0), "urgency_score": 50,
            "intent_level": "High" if score >= 60 else "Medium" if score >= 35 else ("Low" if score > 0 else ""),
            "intent_tag": f"Urgency: {'High' if score >= 60 else 'Medium'} (Cloud Migration)" if score > 0 else "",
            "commercial_signals": ["Cloud Migration"] if score > 0 else [], "migration_path": "On-prem → AWS" if i % 2 else "",
            "latest_posted_at": (NOW - timedelta(days=1 if i % 3 == 0 else 20)).isoformat(),
            "careers_url": f"https://co{i}.example/careers", "contact_email": f"hr@co{i}.example",
            "intent_evidence": ["migrating our secret thing"],
        })
    return out


@pytest.fixture
def kit(config, state, breaker, transport, make_hypothesis):
    FakeSMTP.sent, FakeSMTP.fail = [], False
    config.public_webhook_url = "https://hooks.example.com/webhook"
    config.pages_base_url = "https://me.github.io/datasets"
    k = build_toolkit(config, state, breaker, transport=transport, sleep=lambda s: None, smtp_factory=FakeSMTP)
    k.files.write_json(f"exports/intel/{NICHE}/tech_radar.json", radar_records())
    hyp = make_hypothesis()
    ds = state.add_asset(hyp["id"], "lead_directory", "Python Remote Tech Stack Intel", "assets/x.zip", 1, 12, 1400,
                         product_ref="plink_ds")
    state.update_asset(ds, niche=NICHE, checkout_url="https://buy.stripe.com/ds", status="published", provider="stripe")
    sub = state.add_asset(hyp["id"], "subscription", "Weekly", "assets/x.zip", 1, 12, 1000, product_ref="plink_sub")
    state.update_asset(sub, niche=NICHE, checkout_url="https://buy.stripe.com/sub", status="published", provider="stripe")
    k.hyp = hyp
    return k


def live(config):
    go_live(config)
    config.sender_email, config.sender_name = "sam@example.com", "Sam"


# ------------------------------------------------------------------ capture
def test_capture_stores_a_free_subscriber_kept_out_of_paid_metrics(kit, state):
    res = capture(kit, "  Ada@Example.COM ", NICHE, source="python-remote", ref="am--devto--w40")
    assert res.status == "accepted" and res.created and res.send_sample
    sub = state.subscriber(res.subscriber_id)
    assert (sub["tier"], sub["subscription_status"], sub["email"], sub["niche"]) == ("free", "pending", "ada@example.com", NICHE)
    assert (sub["channel"], sub["campaign"]) == ("devto", "w40") and len(sub["token"]) >= 24
    assert state.list_subscribers() == [] and len(state.list_subscribers(tier="free")) == 1
    from dashboard.snapshot import collect_snapshot

    assert collect_snapshot(state, kit.config)["distribution"]["recurring"] == {"active": 0, "past_due": 0, "canceled": 0,
                                                                                 "mrr_cents": 0}
    again = capture(kit, "ada@example.com", NICHE)
    assert again.status == "accepted" and not again.created and again.subscriber_id == res.subscriber_id


@pytest.mark.parametrize("bad", ["", "nope", "a@b", "a@b.c", "a b@example.com", "x@example.com\nBcc: y@evil.example",
                                 "a@example.com,b@example.com", "<a@example.com>", "a" * 250 + "@example.com"])
def test_invalid_addresses_are_rejected(kit, state, bad):
    assert normalize_email(bad) is None
    assert capture(kit, bad, NICHE).status == "invalid"
    assert state.list_subscribers(tier="free") == []


def test_abuse_brakes(kit, state, config):
    assert capture(kit, "bot@example.com", NICHE, honeypot="http://spam.example").status == "spam"
    assert state.list_subscribers(tier="free") == []
    limiter = CaptureLimiter(per_ip_hour=2)
    assert [capture(kit, f"u{i}@example.com", NICHE, ip="1.2.3.4", limiter=limiter).status for i in range(3)] == \
        ["accepted", "accepted", "rate_limited"]
    assert capture(kit, "other@example.com", NICHE, ip="5.6.7.8", limiter=limiter).status == "accepted"
    config.lead_magnet_max_per_hour = 3
    assert capture(kit, "fourth@example.com", NICHE).status == "rate_limited"  # global hourly cap
    state.suppress("gone@example.com", "unsubscribed")
    config.lead_magnet_max_per_hour = 60
    res = capture(kit, "gone@example.com", NICHE)
    assert res.status == "suppressed" and res.public_ok and res.subscriber_id is None  # looks like success, stores nothing


def test_unknown_niche_falls_back_to_the_active_one(kit, state):
    res = capture(kit, "a@example.com", "<script>")
    assert state.subscriber(res.subscriber_id)["niche"] == NICHE


# ------------------------------------------------------------------ sample
def test_sample_email_live(kit, state, config):
    live(config)
    sid = capture(kit, "ada@example.com", NICHE).subscriber_id
    assert send_sample(kit, sid) == "delivered"
    msg = FakeSMTP.sent[-1]
    assert msg["To"] == "ada@example.com" and "10 companies" in msg["Subject"]
    unsub = f"https://hooks.example.com/lead-magnet/unsubscribe?t={state.subscriber(sid)['token']}"
    assert msg["List-Unsubscribe"].startswith(f"<{unsub}>") and msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    body = msg.get_body(("plain",)).get_content()
    assert "https://hooks.example.com/lead-magnet/confirm?t=" in body and config.sender_postal_address in body
    assert "prefilled_email=ada%40example.com" in body and "client_reference_id=am--leadmagnet--sample" in body
    files = {p.get_filename(): p.get_content() for p in msg.iter_attachments()}
    rows = list(csv.DictReader(io.StringIO(files[f"{NICHE}-free-sample.csv"])))
    assert len(rows) == 10 and rows[0]["company"] == "Co00" and "contact_email" not in rows[0]
    assert "intent_evidence" not in rows[0] and rows[0]["intent_tag"].startswith("Urgency: High")
    sheet = files[f"{NICHE}-intent-cheatsheet.md"]
    assert sheet.startswith("# Python Remote Hiring-Intent Cheatsheet") and "Reading an intent tag" in sheet
    assert send_sample(kit, sid) == "delivered" and len(FakeSMTP.sent) == 1  # never twice
    assert state.subscription_delivery(sid, "sample")["delta_count"] == 10


def test_sample_dry_run_and_failure_retry(kit, state, config):
    sid = capture(kit, "ada@example.com", NICHE).subscriber_id
    assert send_sample(kit, sid) == "dry_run" and FakeSMTP.sent == []
    live(config)
    FakeSMTP.fail = True
    assert send_sample(kit, sid) == "failed"
    FakeSMTP.fail = False
    res = LeadMagnet().run("nurture_leads", TaskContext(kit, kit.hyp, {}))
    assert res.metrics["samples"] == 1 and len(FakeSMTP.sent) == 1


def test_confirm_and_unsubscribe(kit, state):
    sub = state.subscriber(capture(kit, "ada@example.com", NICHE).subscriber_id)
    assert confirm(kit, "wrong") is None
    ok = confirm(kit, sub["token"])
    assert ok["subscription_status"] == "active" and ok["confirmed_at"]
    gone = unsubscribe(kit, sub["token"])
    assert gone["subscription_status"] == "unsubscribed" and state.is_suppressed("ada@example.com")
    assert confirm(kit, sub["token"]) is None  # can't be re-confirmed by an old link
    assert send_sample(kit, sub["id"]) == "skipped"


def test_single_opt_in_mode(kit, state, config):
    config.lead_magnet_double_opt_in = False
    sid = capture(kit, "ada@example.com", NICHE).subscriber_id
    assert state.subscriber(sid)["subscription_status"] == "active"


# ------------------------------------------------------------------ weekly pulse
def test_pulse_signals_prefer_fresh_high_intent():
    recs = radar_records()
    picked = pulse_signals(recs, NOW - timedelta(days=7), 3)
    assert [p["company"] for p in picked] == ["Co00", "Co03", "Co06"] and all(p["fresh"] for p in picked)
    quiet = pulse_signals(recs, NOW + timedelta(days=1), 3)  # nothing new since tomorrow
    assert [p["company"] for p in quiet] == ["Co00", "Co01", "Co02"] and not any(p["fresh"] for p in quiet)
    assert set(picked[0]) == {"company", "intent_tag", "migration_path", "stack", "openings", "fresh", "careers_url",
                              "intent_score", "urgency_score", "company_id"}


def run_nurture(kit):
    return LeadMagnet().run("nurture_leads", TaskContext(kit, kit.hyp, {}))


def test_weekly_pulse_goes_to_confirmed_leads_once_a_week(kit, state, config, clock):
    live(config)
    confirmed = capture(kit, "ada@example.com", NICHE).subscriber_id
    confirm(kit, state.subscriber(confirmed)["token"])
    capture(kit, "pending@example.com", NICHE)  # never confirmed: gets the sample only
    payer = capture(kit, "payer@example.com", NICHE).subscriber_id
    confirm(kit, state.subscriber(payer)["token"])
    state.upsert_subscriber("stripe", "sub_1", email="payer@example.com", niche=NICHE, price_cents=1000)
    res = run_nurture(kit)
    assert res.metrics["due"] and res.metrics["period"] == "2026-W40"
    assert (res.metrics["sent"], res.metrics["skipped"]) == (1, 1)
    pulses = [m for m in FakeSMTP.sent if "Tech Pulse" in m["Subject"]]
    assert [m["To"] for m in pulses] == ["ada@example.com"]
    msg = pulses[0]
    html = msg.get_body(("html",)).get_content()
    assert html.count("<li") == 3 and "Co00" in html
    assert "https://buy.stripe.com/sub?client_reference_id=am--leadmagnet--pulse_2026_w40" in html.replace("&amp;", "&")
    assert "prefilled_email=ada%40example.com" in html.replace("&amp;", "&") and "$10.00/mo" in html
    assert config.sender_postal_address in html and "Unsubscribe with one click" in html
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert "Co00" in msg.get_body(("plain",)).get_content()
    before = len(FakeSMTP.sent)
    assert run_nurture(kit).metrics["sent"] == 0 and len(FakeSMTP.sent) == before  # once per week
    clock.advance(days=7)
    assert run_nurture(kit).metrics["sent"] == 1


def test_pulse_waits_for_monday_9am_and_for_a_postal_address(kit, state, config, clock):
    live(config)
    sid = capture(kit, "ada@example.com", NICHE).subscriber_id
    confirm(kit, state.subscriber(sid)["token"])
    clock.now = NOW.replace(day=5, month=10, hour=8)  # Monday 08:00
    assert run_nurture(kit).metrics["due"] is False
    clock.now = clock.now.replace(hour=9, minute=1)
    config.sender_postal_address = ""
    res = run_nurture(kit)
    assert res.metrics["blocked"] and not [m for m in FakeSMTP.sent if "Tech Pulse" in m["Subject"]]


def test_engine_plans_the_nurture_task():
    from agent.engine import PLAN
    from strategies import default_strategies

    assert ("nurture_leads", 52) in PLAN
    assert any(s.name == "lead_magnet" for s in default_strategies())


# ------------------------------------------------------------------ public endpoints
def test_get_links_never_act_only_post_does(kit, state):
    ep = LeadEndpoints(kit)
    sub = state.subscriber(capture(kit, "ada@example.com", NICHE).subscriber_id)
    page = ep.action_page("confirm", sub["token"])
    assert page.status == 200 and 'method="post"' in page.body and "ada@example.com" in page.body
    assert state.subscriber(sub["id"])["subscription_status"] == "pending"  # a scanner's prefetch changes nothing
    assert ep.confirm(sub["token"]).status == 200
    assert state.subscriber(sub["id"])["subscription_status"] == "active"
    assert ep.action_page("unsubscribe", "bogus").status == 404


def test_http_capture_confirm_and_unsubscribe(kit, state, config):
    from aiohttp.test_utils import TestClient, TestServer

    from tools.storefront.webhook_listener import WebhookProcessor, build_app, pending_key

    live(config)
    config.lead_magnet_max_per_ip_hour = 2
    app = build_app(WebhookProcessor(kit, use_sdk=False), power=PowerManager(NullBackend()))

    async def go():
        async with TestClient(TestServer(app)) as client:
            r = await client.post("/lead-magnet/capture", data={"email": "ada@example.com", "niche": NICHE, "website": ""},
                                  headers={"CF-Connecting-IP": "9.9.9.9"})
            assert r.status == 200 and "Check your inbox" in await r.text()
            assert r.headers["X-Robots-Tag"] == "noindex"
            for _ in range(100):  # the sample goes out after the response
                if not app[pending_key()]:
                    break
                await asyncio.sleep(0.02)
            r = await client.post("/lead-magnet/capture", data={"email": "bad"}, headers={"CF-Connecting-IP": "9.9.9.9"})
            assert r.status == 400
            r = await client.post("/lead-magnet/capture", data={"email": "b@example.com"}, headers={"CF-Connecting-IP": "9.9.9.9"})
            assert r.status == 429  # third request from the same visitor this hour
            r = await client.post("/lead-magnet/capture", json={"email": "c@example.com", "niche": NICHE},
                                  headers={"CF-Connecting-IP": "8.8.8.8"})
            assert r.status == 200 and json.loads(await r.text()) == {"ok": True, "error": None}
            token = state.list_subscribers(tier="free")[0]["token"]
            r = await client.get(f"/lead-magnet/confirm?t={token}")
            assert r.status == 200 and state.list_subscribers(tier="free")[0]["subscription_status"] == "pending"
            r = await client.post("/lead-magnet/confirm", data={"t": token})
            assert r.status == 200 and state.list_subscribers(tier="free")[0]["subscription_status"] == "active"
            r = await client.post(f"/lead-magnet/unsubscribe?t={token}", data={"List-Unsubscribe": "One-Click"})
            assert r.status == 200 and state.is_suppressed("ada@example.com")
            # the listener still refuses unsigned Stripe events: lead routes don't weaken /webhook
            assert (await client.post("/webhook", data=b"{}")).status in (400, 503)

    asyncio.run(go())
    assert [m["To"] for m in FakeSMTP.sent] == ["ada@example.com", "c@example.com"]
