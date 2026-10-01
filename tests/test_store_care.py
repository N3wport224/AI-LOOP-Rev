"""Phases 24-27: storefront health, new-release emails, goal pacing, stale-data guard."""

import io
import zipfile
from datetime import timedelta

import pytest

from strategies.freshness_guard import FreshnessGuard, is_stale, promotable
from strategies.goal_pacing import GoalPacing, compute_pace, describe, next_step
from strategies.owner_reports import OwnerReports
from strategies.release_announcer import ReleaseAnnouncer
from strategies.share_kit import build_kit
from strategies.storefront_health import StorefrontHealth
from tests import test_business_ops
from tests.test_business_ops import STRIPE, ctx, dataset
from tests.test_distribution import FakeSMTP

kit = test_business_ops.kit  # the same live-mode toolkit fixture


def alerts(state, source):
    return [e for e in state.recent_errors(50, kind="alert") if e["source"] == source]


def fresh_leads(state, niche, clock, days_ago=0):
    real = clock.now
    clock.now = real - timedelta(days=days_ago)
    state.upsert_lead(f"{niche}-lead-{days_ago}", niche, {"company": "Acme"})
    clock.now = real


def buyer(state, aid, email, order_id):
    state.record_order("stripe", order_id, email, 1900, None, aid, None, status="delivered")


# ------------------------------------------------------------------ Phase 24: storefront health
def test_storefront_checks_link_page_and_file_and_alerts_once(kit, state, transport, clock):
    _, aid = dataset(kit, state, "python-remote")
    state.update_asset(aid, lander_url="https://me.github.io/d/python-remote/")
    transport.add_json(f"{STRIPE}/payment_links/plink_python-remote", {"active": True})
    transport.add("https://me.github.io/", __import__("tools.http_client", fromlist=["Response"]).Response(200, "x", b"ok", {}))
    res = StorefrontHealth().run("check_storefront", ctx(kit))
    assert res.metrics == {"ok": 1, "bad": 0} and not alerts(state, "storefront_health")
    assert "storefront checked" in StorefrontHealth().run("check_storefront", ctx(kit)).summary  # every 6 hours

    transport.add_json(f"{STRIPE}/payment_links/plink_python-remote", {"active": False})
    kit.files.resolve(state.get_asset(aid)["path"]).unlink()
    clock.advance(hours=7)
    assert StorefrontHealth().run("check_storefront", ctx(kit)).metrics["bad"] == 1
    [alert] = alerts(state, "storefront_health")
    assert "switched off" in alert["message"] and "missing" in alert["message"]
    clock.advance(hours=7)
    StorefrontHealth().run("check_storefront", ctx(kit))
    assert len(alerts(state, "storefront_health")) == 1  # the same problem isn't repeated

    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as zf:
        zf.writestr("leads.csv", "company\nAcme\n")
    kit.files.write_bytes(state.get_asset(aid)["path"], data.getvalue())  # the file is back
    transport.add_json(f"{STRIPE}/payment_links/plink_python-remote", {"active": True})
    clock.advance(hours=7)
    assert StorefrontHealth().run("check_storefront", ctx(kit)).metrics["bad"] == 0


def test_a_broken_page_is_reported_in_doctor_and_digest(kit, state, transport, clock):
    from cli.doctor import Doctor
    from tests.test_autopilot import Ctl, runner

    _, aid = dataset(kit, state, "python-remote")
    state.update_asset(aid, lander_url="https://me.github.io/gone/")
    transport.add_json(f"{STRIPE}/payment_links/plink_python-remote", {"active": True})
    StorefrontHealth().run("check_storefront", ctx(kit))
    assert "product page doesn't load (404)" in alerts(state, "storefront_health")[0]["message"]
    findings = {f.name: f for f in Doctor(kit.config, state, Ctl(pid=1), run=runner({}), system="Linux").checks(deep=False)}
    assert findings["Checkout & downloads"].status == "warn" and "404" in findings["Checkout & downloads"].detail
    assert "Can't be bought right now" in OwnerReports.digest_body(kit, clock())


# ------------------------------------------------------------------ Phase 27: stale-data guard
def test_stale_datasets_alert_once_and_are_not_promoted(kit, state, clock):
    _, aid = dataset(kit, state, "python-remote")
    fresh_leads(state, "python-remote", clock, days_ago=10)
    assert FreshnessGuard().run("guard_freshness", ctx(kit)).metrics["stale"] == 1
    assert is_stale(state, aid) and promotable(state) == []
    assert build_kit(state, kit.files)["posts"] == []
    assert "10 days" in alerts(state, "freshness_guard")[0]["message"]
    FreshnessGuard().run("guard_freshness", ctx(kit))
    assert len(alerts(state, "freshness_guard")) == 1
    assert "Not being promoted" in OwnerReports.digest_body(kit, clock())

    fresh_leads(state, "python-remote", clock)  # postings flow again
    assert FreshnessGuard().run("guard_freshness", ctx(kit)).metrics["stale"] == 0
    assert not is_stale(state, aid) and len(build_kit(state, kit.files)["posts"]) == 4


def test_fresh_and_never_scraped_niches_are_left_alone(kit, state, clock):
    dataset(kit, state, "python-remote")
    fresh_leads(state, "python-remote", clock, days_ago=2)
    dataset(kit, state, "ml-ai")  # no lead history at all: nothing to judge yet
    assert FreshnessGuard().run("guard_freshness", ctx(kit)).metrics["stale"] == 0


# ------------------------------------------------------------------ Phase 25: new-release emails
def test_past_buyers_hear_about_a_new_niche_once(kit, state, clock):
    _, py = dataset(kit, state, "python-remote")
    buyer(state, py, "a@co.example", "cs_a")
    buyer(state, py, "gone@co.example", "cs_g")
    state.suppress("gone@co.example", "test")
    before = len(FakeSMTP.sent)
    assert ReleaseAnnouncer().run("announce_releases", ctx(kit)).metrics["sent"] == 0  # first run: old news
    assert len(FakeSMTP.sent) == before

    _, ml = dataset(kit, state, "ml-ai")
    buyer(state, ml, "ml@co.example", "cs_m")  # already owns the new niche
    assert ReleaseAnnouncer().run("announce_releases", ctx(kit)).metrics["sent"] == 1
    msg = FakeSMTP.sent[-1]
    assert msg["To"] == "a@co.example" and "ml-ai Tech Stack Intel" in msg["Subject"]
    body = msg.get_body(("plain",)).get_content() if msg.is_multipart() else msg.get_content()
    assert "client_reference_id=" in body and "unsubscribe" in body and "1 Main St" in body
    assert "mailto:" in msg["List-Unsubscribe"]
    assert ReleaseAnnouncer().run("announce_releases", ctx(kit)).metrics["sent"] == 0  # once

    _, go = dataset(kit, state, "golang")
    assert ReleaseAnnouncer().run("announce_releases", ctx(kit)).metrics["sent"] == 1  # ml@ only; a@ had one this fortnight
    assert FakeSMTP.sent[-1]["To"] == "ml@co.example"
    clock.advance(days=15)  # past the 14-day window: golang is no longer "new"
    assert ReleaseAnnouncer().run("announce_releases", ctx(kit)).summary == "no new releases to announce"


def test_announcements_wait_for_a_postal_address_and_can_be_off(kit, state, config):
    _, py = dataset(kit, state, "python-remote")
    buyer(state, py, "a@co.example", "cs_a")
    ReleaseAnnouncer().run("announce_releases", ctx(kit))
    dataset(kit, state, "ml-ai")
    config.sender_postal_address = ""
    assert "postal" in ReleaseAnnouncer().run("announce_releases", ctx(kit)).summary
    config.release_announcements = False
    assert ReleaseAnnouncer().run("announce_releases", ctx(kit)).summary == "release announcements off"


# ------------------------------------------------------------------ Phase 26: goal pacing
@pytest.mark.parametrize("args,expect", [
    ((0, [], 0, 1000, 0, 0, ""), "go-live"),
    ((1, ["X: link off"], 0, 1000, 0, 0, ""), "Fix the storefront"),
    ((1, [], 1200, 1000, 9, 9, "devto"), "on goal"),
    ((1, [], 0, 1000, 0, 0, ""), "connect-marketing"),
    ((1, [], 200, 1000, 10, 1, ""), "10 people opened checkout but only 1 paid"),
    ((1, [], 300, 1000, 4, 3, "linkedin"), "from linkedin"),
])
def test_next_step_rules(config, args, expect):
    assert expect in next_step(config, *args)


def test_pace_is_computed_daily_and_shown(kit, state, config, clock):
    config.github_pages_repo, config.pages_base_url = "me/me.github.io", "https://me.github.io/d"
    dataset(kit, state, "python-remote")
    state.record_revenue("stripe", "cs_1", 1900, 85, 1815, True)
    pace = compute_pace(state, config)
    assert pace["net_per_day_cents"] == round(1815 / 7) and pace["projected_month_cents"] == pace["net_per_day_cents"] * 30
    assert "share kit" in pace["next_step"]  # nobody reached a checkout session yet
    assert GoalPacing().run("pace_goal", ctx(kit)).ok and state.get("goal_pace")["date"] == "2026-09-30"
    assert GoalPacing().run("pace_goal", ctx(kit)).summary == "pace up to date"
    text = describe(state.get("goal_pace"))
    assert text.startswith("Pace (7 days): $2.59/day of $10.00 (26% of goal)") and "Next step:" in text
    assert "Pace (7 days)" in OwnerReports.digest_body(kit, clock())


@pytest.mark.parametrize("task", ["check_storefront", "announce_releases", "pace_goal", "guard_freshness"])
def test_new_tasks_are_planned_and_handled(config, state, toolkit, task):
    from agent.engine import PLAN, Engine

    engine = Engine(config, state=state, sleep=lambda s: None)
    assert task in dict(PLAN) and task in engine.handlers
    assert engine.breaker.max_actions_per_cycle >= len(PLAN) + 10


def test_cli_and_gui_expose_the_pace(kit, state):
    from dashboard.cli import build_parser

    assert build_parser().parse_args(["pace"]).func.__name__ == "cmd_pace"
    html = (__import__("pathlib").Path(__file__).parents[1] / "gui/static/index.html").read_text()
    assert 'id="k-pace"' in html
