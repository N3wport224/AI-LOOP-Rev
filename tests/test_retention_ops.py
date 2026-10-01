"""Phases 28-31: heartbeat, release gate, refresh offers, launch codes."""

from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from agent.heartbeat import Heartbeat
from strategies.base import TaskContext
from strategies.digital_asset_packager import DigitalAssetPackager
from strategies.launch_promos import LaunchPromos, active_code
from strategies.refresh_offers import RefreshOffers
from strategies.release_announcer import ReleaseAnnouncer
from strategies.release_gate import gate, held, problems
from strategies.share_kit import build_kit
from tests import test_business_ops
from tests.test_business_ops import STRIPE, ctx, dataset
from tests.test_distribution import FakeSMTP
from tools.http_client import Response
from tools.promo import code_text, promo_link

kit = test_business_ops.kit  # the same live-mode toolkit fixture


def body_of(msg):
    return msg.get_body(("plain",)).get_content() if msg.is_multipart() else msg.get_content()


def form(call):
    return parse_qs((call["body"] or b"").decode())


def stripe_promos(transport, niche):
    transport.add_json(f"{STRIPE}/payment_links/plink_{niche}/line_items", {"data": [{"price": {"product": f"prod_{niche}"}}]})
    transport.add_json(f"{STRIPE}/coupons", {"id": "co_1"})
    transport.add_json(f"{STRIPE}/promotion_codes", {"id": "promo_1"})


# ------------------------------------------------------------------ Phase 28: heartbeat
def test_heartbeat_pings_and_never_fails_the_cycle(kit, state, transport, config):
    assert "no heartbeat URL" in Heartbeat().run("send_heartbeat", ctx(kit)).summary
    config.heartbeat_url = "https://hc-ping.com/abc"
    transport.add("https://hc-ping.com/abc", Response(200, "x", b"OK", {}))
    assert Heartbeat().run("send_heartbeat", ctx(kit)).metrics == {"ok": True}
    assert state.get("last_heartbeat_at") and transport.calls_to("https://hc-ping.com/abc")
    config.heartbeat_url = "https://hc-ping.com/gone"  # 404
    res = Heartbeat().run("send_heartbeat", ctx(kit))
    assert res.ok and res.metrics == {"ok": False}
    config.heartbeat_url = "http://insecure.example"
    assert "https" in Heartbeat().run("send_heartbeat", ctx(kit)).summary


def test_doctor_suggests_a_heartbeat(kit, state, config):
    from cli.doctor import Doctor
    from tests.test_autopilot import Ctl, runner

    findings = {f.name: f for f in Doctor(config, state, Ctl(pid=1), run=runner({}), system="Linux").checks(deep=False)}
    assert findings["Heartbeat"].status == "warn" and "automonetize heartbeat" in findings["Heartbeat"].fix
    config.heartbeat_url = "https://hc-ping.com/abc"
    state.set("last_heartbeat_at", state.now())
    findings = {f.name: f for f in Doctor(config, state, Ctl(pid=1), run=runner({}), system="Linux").checks(deep=False)}
    assert findings["Heartbeat"].status == "ok"


def test_heartbeat_command_tests_and_saves_the_url(kit, config, tmp_path, monkeypatch, transport):
    import cli.go_live
    import cli.growth

    env = tmp_path / ".env"
    env.write_text("DRY_RUN=true\n")
    monkeypatch.setattr(cli.growth, "ROOT", tmp_path)
    monkeypatch.setattr(cli.growth, "_setup", lambda: (config, kit.state, kit.files))
    restarted = []
    monkeypatch.setattr(cli.go_live, "restart", lambda env: restarted.append(env) or True)
    assert cli.growth.heartbeat_main(["not-a-url"]) == 1
    assert cli.growth.heartbeat_main(["https://hc-ping.com/missing"], transport=transport) == 1  # 404
    transport.add("https://hc-ping.com/abc", Response(200, "x", b"OK", {}))
    assert cli.growth.heartbeat_main(["https://hc-ping.com/abc"], transport=transport) == 0
    assert "HEALTHCHECK_URL=https://hc-ping.com/abc" in env.read_text() and restarted


# ------------------------------------------------------------------ Phase 29: release gate
def rows(n, blank=0):
    return [{"company": "" if i < blank else f"Co{i}", "title": "Engineer"} for i in range(n)]


def test_gate_rules():
    assert problems(100, rows(60), 0.5) == ([], True)
    assert problems(100, rows(40), 0.5) == (["rows dropped from 100 to 40"], True)
    reasons, releasable = problems(0, rows(10, blank=5), 0.5)
    assert reasons == ["50% of rows have no company or job title"] and not releasable


def test_a_shrunken_build_is_held_then_released_if_it_persists(state, config, clock):
    live = {"lead_count": 100, "checkout_url": "https://buy.stripe.com/x"}
    assert gate(state, config, "py", live, rows(90)) is None
    assert gate(state, config, "py", live, rows(30)) == "rows dropped from 100 to 30"
    assert len(state.recent_errors(5, kind="alert")) == 1 and "py" in held(state)
    clock.advance(days=1)
    assert gate(state, config, "py", live, rows(35))  # still held, no second alert
    assert len(state.recent_errors(5, kind="alert")) == 1
    clock.advance(days=3)
    assert gate(state, config, "py", live, rows(35)) is None  # persisted 3+ days: it's real
    assert held(state) == {}


def test_blank_rows_are_never_released_by_time_and_nothing_on_sale_is_not_protected(state, config, clock):
    live = {"lead_count": 100, "checkout_url": "https://buy.stripe.com/x"}
    assert gate(state, config, "py", live, rows(100, blank=50))
    clock.advance(days=30)
    assert gate(state, config, "py", live, rows(100, blank=50))
    assert gate(state, config, "py", live, rows(100)) is None  # fixed
    assert gate(state, config, "new", {"lead_count": 100}, rows(10)) is None  # not on sale yet


def test_the_packager_keeps_the_old_version_on_sale(kit, state, config):
    config.min_leads_for_asset = 5
    hid = state.create_hypothesis("lead_directory:py:g1", "lead_directory", "py data", {"niche": "py"})
    for i in range(40):
        state.upsert_lead(f"k{i}", "py", {"company": f"Co{i}", "title": "Python Engineer"})
    hyp = {"id": hid, "params": {"niche": "py"}, "iterations": 0}
    assert DigitalAssetPackager().run("package_asset", TaskContext(kit, hyp, {})).metrics["built"]
    v1 = state.latest_asset(hid, "lead_directory")
    state.update_asset(v1["id"], status="published", checkout_url="https://buy.stripe.com/py")
    state._exec("DELETE FROM leads WHERE niche = 'py' AND dedupe_key NOT IN ('k1', 'k2', 'k3', 'k4', 'k5', 'k6')")
    res = DigitalAssetPackager().run("package_asset", TaskContext(kit, hyp, {}))
    assert res.metrics["built"] is False and "held" in res.summary
    assert state.latest_asset(hid, "lead_directory")["id"] == v1["id"]


# ------------------------------------------------------------------ Phase 31: launch codes
def test_new_niches_get_a_launch_code_used_in_emails_and_posts(kit, state, transport, clock):
    _, py = dataset(kit, state, "python-remote")
    state.record_order("stripe", "cs_a", "a@co.example", 1900, None, py, None, status="delivered")
    assert LaunchPromos().run("create_launch_promos", ctx(kit)).metrics["created"] == 0  # first run: old news
    dataset(kit, state, "ml-ai")
    stripe_promos(transport, "ml-ai")
    assert LaunchPromos().run("create_launch_promos", ctx(kit)).metrics["created"] == 1
    code = active_code(state, "ml-ai")
    assert code["code"] == "LAUNCHMLAI" and code["percent_off"] == 20
    coupon = form(transport.calls_to(f"{STRIPE}/coupons", "POST")[-1])
    assert coupon["percent_off"] == ["20"] and coupon["applies_to[products][0]"] == ["prod_ml-ai"]
    assert form(transport.calls_to(f"{STRIPE}/payment_links/plink_ml-ai", "POST")[-1])["allow_promotion_codes"] == ["true"]
    assert LaunchPromos().run("create_launch_promos", ctx(kit)).metrics["created"] == 0  # once per niche

    ReleaseAnnouncer().run("announce_releases", ctx(kit))
    body = body_of(FakeSMTP.sent[-1])
    assert "20% off with code LAUNCHMLAI" in body and "prefilled_promo_code=LAUNCHMLAI" in body
    posts = [p for p in build_kit(state, kit.files)["posts"] if p["product"].startswith("ml-ai")]
    assert posts and all("LAUNCHMLAI" in p["text"] and "prefilled_promo_code" in p["link"] for p in posts)

    clock.advance(days=8)
    assert active_code(state, "ml-ai") is None
    assert not any("LAUNCHMLAI" in p["text"] for p in build_kit(state, kit.files)["posts"])


def test_launch_codes_need_live_mode(kit, state, config):
    config.dry_run = True
    dataset(kit, state, "python-remote")
    LaunchPromos().run("create_launch_promos", ctx(kit))
    dataset(kit, state, "ml-ai")
    assert "live mode only" in LaunchPromos().run("create_launch_promos", ctx(kit)).summary


def test_code_helpers():
    assert code_text("LAUNCH", "ml-ai") == "LAUNCHMLAI"
    assert len(code_text("UPDATE", "py", random_suffix=True)) == len("UPDATEPY") + 6
    assert parse_qs(urlsplit(promo_link("https://buy.stripe.com/x?client_reference_id=r", "C1")).query) == \
        {"client_reference_id": ["r"], "prefilled_promo_code": ["C1"]}


# ------------------------------------------------------------------ Phase 30: refresh offers
def grown(kit, state, clock, old_rows=50, new_rows=120, days_ago=40):
    hid, old = dataset(kit, state, "python-remote", rows=old_rows)
    when = (clock() - timedelta(days=days_ago)).isoformat(timespec="seconds")
    state.record_order("stripe", "cs_old", "a@co.example", 1900, "plink_python-remote", old, hid, occurred_at=when, status="delivered")
    new = state.add_asset(hid, "lead_directory", "python-remote Tech Stack Intel", "assets/python-remote/python-remote-v1.zip", 2,
                          new_rows, 1900, product_ref="plink_python-remote")
    state.update_asset(new, provider="stripe", status="published", checkout_url="https://buy.stripe.com/python-remote")
    return hid, old, new


def test_buyers_get_one_single_use_discount_when_the_data_grew(kit, state, transport, clock):
    grown(kit, state, clock)
    stripe_promos(transport, "python-remote")
    assert RefreshOffers().run("offer_refresh", ctx(kit)).metrics["sent"] == 1
    msg = FakeSMTP.sent[-1]
    body = body_of(msg)
    assert msg["To"] == "a@co.example" and "70 new rows" in msg["Subject"]
    assert "has 50 rows; today's has 120" in body and "$9.50 instead of $19" in body and "prefilled_promo_code=UPDATEPYTH" in body
    assert "unsubscribe" in body and "1 Main St" in body
    promo = form(transport.calls_to(f"{STRIPE}/promotion_codes", "POST")[-1])
    assert promo["max_redemptions"] == ["1"] and promo["code"][0].startswith("UPDATEPYTH")
    assert RefreshOffers().run("offer_refresh", ctx(kit)).metrics["sent"] == 0  # once per order


@pytest.mark.parametrize("setup,why", [
    (lambda s, c: None, "too_recent"),
    (lambda s, c: None, "too_little_growth"),
    (lambda s, c: s.suppress("a@co.example", "t"), "suppressed"),
    (lambda s, c: s.record_order("stripe", "cs_new", "a@co.example", 1900, "plink_python-remote", None, None), "bought_again"),
    (lambda s, c: setattr(c, "refresh_offers", False), "off"),
])
def test_no_offer_when_it_would_not_be_welcome(kit, state, transport, clock, config, setup, why):
    days = 10 if why == "too_recent" else 40
    new_rows = 60 if why == "too_little_growth" else 120
    hid, old, new = grown(kit, state, clock, new_rows=new_rows, days_ago=days)
    if why == "bought_again":
        state.record_order("stripe", "cs_new", "a@co.example", 1900, "plink_python-remote", new, hid, status="delivered")
    else:
        setup(state, config)
    stripe_promos(transport, "python-remote")
    assert RefreshOffers().run("offer_refresh", ctx(kit)).metrics["sent"] == 0


def test_dry_run_previews_without_creating_stripe_codes(kit, state, transport, clock, config):
    grown(kit, state, clock)
    config.dry_run = True
    assert RefreshOffers().run("offer_refresh", ctx(kit)).metrics["sent"] == 1
    assert not transport.calls_to(f"{STRIPE}/coupons")


@pytest.mark.parametrize("task", ["send_heartbeat", "create_launch_promos", "offer_refresh"])
def test_new_tasks_are_planned_and_handled(config, state, toolkit, task):
    from agent.engine import PLAN, Engine

    engine = Engine(config, state=state, sleep=lambda s: None)
    assert task in dict(PLAN) and task in engine.handlers
    assert engine.breaker.max_actions_per_cycle >= len(PLAN) + 10
