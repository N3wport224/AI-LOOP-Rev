"""Phases 40-44: contact policy, bounce guard, offer tuner, privacy requests, security self-audit."""

import json
import os
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from agent.security_audit import SecurityAudit, audit
from strategies.bounce_guard import BounceGuard, failed_recipients
from strategies.offer_tuner import OfferTuner, offer_pct, report
from strategies.owner_todo import todo
from strategies.refresh_offers import RefreshOffers
from strategies.support_desk import SupportDesk
from tests import test_business_ops
from tests.test_business_ops import ctx, dataset
from tests.test_distribution import FakeSMTP
from tools import contact_policy as contact
from tools.privacy import export, forget, placeholder

kit = test_business_ops.kit  # the same live-mode toolkit fixture


# ------------------------------------------------------------------ Phase 40: contact policy
def test_one_promo_per_person_per_fortnight_across_offer_types(state, config, clock):
    assert contact.blocked(state, config, "a@co.example", "release") is None
    contact.record(state, "a@co.example", "release", "py")
    assert "had an offer" in contact.blocked(state, config, "A@co.example", "refresh")
    assert contact.blocked(state, config, "a@co.example", "followup") is None  # a follow-up isn't an offer
    state.set("release_last_sent", {"b@co.example": state.now()})  # dates written before the log existed count too
    assert contact.blocked(state, config, "b@co.example", "winback")
    clock.advance(days=15)
    assert contact.blocked(state, config, "a@co.example", "sale") is None


def test_suppression_pause_and_daily_cap(state, config, clock):
    state.suppress("gone@co.example", "t")
    assert contact.blocked(state, config, "gone@co.example", "followup") == "suppressed"
    config.promo_daily_cap = 2
    for i in range(2):
        contact.record(state, f"p{i}@co.example", "followup")
    assert contact.blocked(state, config, "new@co.example", "followup") == "daily marketing cap reached"
    clock.advance(days=1)
    state.set(contact.PAUSE_KEY, {"until": (clock() + timedelta(days=1)).isoformat(timespec="seconds"), "reason": "bounces"})
    assert "paused" in contact.blocked(state, config, "new@co.example", "release")


def test_offers_respect_the_shared_policy(kit, state, clock):
    hid, old = dataset(kit, state, "python-remote", rows=50)
    when = (clock() - timedelta(days=40)).isoformat(timespec="seconds")
    state.record_order("stripe", "cs_old", "a@co.example", 1900, "plink_python-remote", old, hid, occurred_at=when, status="delivered")
    new = state.add_asset(hid, "lead_directory", "python-remote Tech Stack Intel", "assets/python-remote/python-remote-v1.zip", 2, 120,
                          1900, product_ref="plink_python-remote")
    state.update_asset(new, provider="stripe", status="published", checkout_url="https://buy.stripe.com/python-remote")
    kit.config.dry_run = True
    contact.record(state, "a@co.example", "sample", "1")  # they just had a different offer
    assert RefreshOffers().run("offer_refresh", ctx(kit)).metrics["sent"] == 0
    clock.advance(days=15)
    assert RefreshOffers().run("offer_refresh", ctx(kit)).metrics["sent"] == 1
    assert contact.sends(state, "refresh", "2000-01-01") == 1


# ------------------------------------------------------------------ Phase 41: bounce guard
GMAIL_BOUNCE = """Address not found

Your message wasn't delivered to ghost@nowhere.example because the address couldn't be found, or is unable to receive mail.

The response from the remote server was:
550 5.1.1 The email account that you tried to reach does not exist."""
DSN = "Reporting-MTA: dns; mx.example\n\nFinal-Recipient: rfc822; old@corp.example\nAction: failed\nStatus: 5.1.1\n"
FULL = "Delivery incomplete\n\nThere was a temporary problem delivering your message to busy@co.example. 4.2.2 Mailbox full."


def test_hard_bounces_are_recognised_and_temporary_ones_ignored():
    own = {"me@gmail.com"}
    assert failed_recipients(GMAIL_BOUNCE, own) == ["ghost@nowhere.example"]
    assert failed_recipients(DSN, own) == ["old@corp.example"]
    assert failed_recipients(FULL, own) == []


def test_bounces_are_suppressed_and_a_high_rate_pauses_marketing(kit, state, config):
    for i in range(20):
        contact.record(state, f"p{i}@co.example", "followup")
    inbox = [{"sender": "mailer-daemon@googlemail.com", "subject": "Delivery Status Notification (Failure)",
              "message_id": f"<b{i}>", "body": GMAIL_BOUNCE.replace("ghost", f"ghost{i}")} for i in range(2)]
    seen = []

    def scan(host, user, pw, wanted, since):
        seen.append((wanted("mailer-daemon@googlemail.com"), wanted("customer@co.example")))
        return inbox

    res = BounceGuard(scan=scan).run("process_bounces", ctx(kit))
    assert seen == [(True, False)]  # only bounce notices are read
    assert res.metrics["bounced"] == 2 and state.is_suppressed("ghost0@nowhere.example")
    assert contact.paused(state) and "Marketing email paused" in state.recent_errors(1, kind="alert")[0]["message"]
    assert BounceGuard(scan=scan).run("process_bounces", ctx(kit)).metrics["bounced"] == 0  # handled once
    from strategies.distribution_engine import DistributionEngine

    assert DistributionEngine().run("dispatch_outreach", ctx(kit)).metrics.get("paused")  # approved sales emails wait too


# ------------------------------------------------------------------ Phase 42: offer tuner
def test_discounts_rise_without_sales_and_fall_when_they_sell(state, config, toolkit, clock):
    from strategies.base import TaskContext

    t = TaskContext(toolkit, {"id": 1, "params": {}, "iterations": 0}, {})
    for i in range(30):
        contact.record(state, f"r{i}@co.example", "refresh")
        contact.record(state, f"w{i}@co.example", "winback")
    for i in range(6):  # 6 of 30 win-back emails converted (20%)
        state.upsert_subscriber("stripe", f"sub_{i}", email=f"w{i}@co.example", niche="py", price_cents=1000, status="active",
                                channel="winback")
    res = OfferTuner().run("tune_offers", t)
    assert res.metrics["changes"] == 2
    assert offer_pct(state, config, "refresh") == 60 and offer_pct(state, config, "winback") == 45
    assert OfferTuner().run("tune_offers", t).summary.startswith("offers tuned")  # weekly
    clock.advance(days=8)
    assert OfferTuner().run("tune_offers", t).summary == "offers: no change"  # counting restarts after a change
    rows = {r["kind"]: r for r in report(state, config)}
    assert rows["refresh"]["sent"] == 0 and rows["sample"]["pct"] == config.sample_offer_pct
    config.offer_tuning = False
    assert offer_pct(state, config, "refresh") == config.refresh_discount_pct


def test_tuning_respects_bounds(state, config, toolkit, clock):
    from strategies.base import TaskContext

    t = TaskContext(toolkit, {"id": 1, "params": {}, "iterations": 0}, {})
    state.set("offer_tuning", {"refresh": {"pct": 60, "since": "2000-01-01T00:00:00+00:00"}})
    for i in range(40):
        contact.record(state, f"r{i}@co.example", "refresh")
    OfferTuner().run("tune_offers", t)
    assert offer_pct(state, config, "refresh") == 60


# ------------------------------------------------------------------ Phase 43: privacy requests
def test_privacy_requests_are_detected_and_passed_to_you(kit, state):
    _, aid = dataset(kit, state, "python-remote")
    state.record_order("stripe", "cs_1", "p@co.example", 1900, None, aid, None, status="delivered")
    inbox = [{"sender": "p@co.example", "subject": "GDPR", "message_id": "<p1>", "body": "Please delete all my data."}]
    res = SupportDesk(scan=lambda *a: inbox).run("answer_support", ctx(kit))
    assert res.metrics["alerted"] == 1
    alert = state.recent_errors(1, kind="alert")[0]["message"]
    assert "automonetize privacy forget p@co.example" in alert
    assert any(i["title"] == "Answer 1 privacy request(s)" for i in todo(state, kit.config))


def test_export_and_forget(kit, state, config):
    _, aid = dataset(kit, state, "python-remote")
    email = "p@co.example"
    state.record_order("stripe", "cs_1", email, 1900, None, aid, None, status="delivered")
    state.record_revenue("stripe", "cs_1", 1900, 85, 1815, True)
    state.capture_free_subscriber(email, "python-remote", "tok", status="active")
    state.stage_outreach(1, "k", "email", email, "Hi", "Body", 0.9)
    contact.record(state, email, "followup")
    state.log_error("support_desk", f"customer {email} wrote to you", kind="alert")
    state.set("testimonials", [{"id": 1, "email": email, "niche": "py", "text": "x", "status": "approved", "at": state.now()}])
    state.set("referral_tokens", {"abc": email})
    state.set("privacy_requests", {email: {"at": state.now(), "subject": "GDPR"}})
    (config.data_dir / "dispatched_audit.log").write_text(json.dumps({"to": email, "subject": "Your download"}) + "\n" +
                                                         json.dumps({"to": "other@co.example", "subject": "x"}) + "\n")
    data = export(state, config, email)
    assert len(data["orders"]) == 1 and data["emails_sent"][0]["subject"] == "Your download" and data["referral_link"]

    done = forget(state, config, email)
    assert done["orders anonymised"] == 1 and done["free signups deleted"] == 1 and done["sales emails deleted"] == 1
    order = state.get_order("stripe", "cs_1")
    assert order["email"] == placeholder(email) and order["gross_cents"] == 1900  # the sale is kept, anonymised
    assert state.revenue_for_day()["net"] == 1815
    assert email not in state.recent_errors(1, kind="alert")[0]["message"]
    assert state.get("testimonials") == [] and state.get("referral_tokens") == {} and state.get("privacy_requests") == {}
    assert email not in (config.data_dir / "dispatched_audit.log").read_text()
    assert state.is_suppressed(email)  # never emailed again
    left = export(state, config, email)
    assert not any(left[k] for k in ("orders", "subscriptions", "sales_emails", "emails_sent", "contact_log", "testimonials"))


def test_privacy_command_asks_before_erasing(kit, state, config, monkeypatch):
    import cli.growth

    monkeypatch.setattr(cli.growth, "_setup", lambda: (config, state, kit.files))
    assert cli.growth.privacy_main(["forget", "nope"]) == 1
    assert cli.growth.privacy_main(["forget", "p@co.example"], confirm=lambda prompt: "no") == 1
    assert not state.is_suppressed("p@co.example")
    assert cli.growth.privacy_main(["export", "p@co.example"]) == 0
    assert cli.growth.privacy_main(["forget", "p@co.example"], confirm=lambda prompt: "FORGET") == 0
    assert state.is_suppressed("p@co.example")


# ------------------------------------------------------------------ Phase 44: security self-audit
def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@t.invalid", "-c", "commit.gpgsign=false", *args],
                          cwd=cwd, capture_output=True, text=True, check=True)


def test_audit_fixes_permissions_and_gitignore(config, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    env = root / ".env"
    env.write_text("STRIPE_SECRET_KEY=x\n")
    os.chmod(env, 0o644)
    config.ensure_dirs()
    os.chmod(config.data_dir, 0o755)
    found = {f["name"]: f for f in audit(config, root)}
    assert found[".env permissions"]["status"] == "fixed" and oct(env.stat().st_mode)[-3:] == "600"
    assert found["data permissions"]["status"] == "fixed" and oct(config.data_dir.stat().st_mode)[-3:] == "700"
    assert found[".gitignore"]["status"] == "fixed" and ".env" in (root / ".gitignore").read_text()
    assert audit(config, root) == []  # nothing left


def test_audit_reports_what_only_you_can_fix(config, tmp_path, state, toolkit):
    from strategies.base import TaskContext

    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    (root / ".env").write_text("X=1\n")
    git(root, "add", "-f", ".env")
    git(root, "commit", "-qm", "oops")
    (root / "automonetize.toml").write_text('stripe_secret_key = "sk_live_abc"\n')
    config.gui_host, config.stripe_secret_key = "0.0.0.0", "sk_live_abc"
    found = {f["name"]: f for f in audit(config, root, fix=False)}
    assert found[".env in git"]["status"] == "fail" and found["control panel exposure"]["status"] == "fail"
    assert found["secrets in automonetize.toml"]["status"] == "warn"
    assert "restricted key" in found["Stripe key type"]["fix"] and found["webhook secret"]["status"] == "warn"
    t = TaskContext(toolkit, {"id": 1, "params": {}, "iterations": 0}, {})
    assert SecurityAudit(root=root).run("audit_security", t).metrics["open"] >= 4
    assert len(state.recent_errors(10, kind="alert")) == 2  # the two failures, once
    assert SecurityAudit(root=root).run("audit_security", t).summary.startswith("security checked")
    titles = [i["title"] for i in todo(state, config)]
    assert "Security: .env in git" in titles and "Security: Stripe key type" in titles


@pytest.mark.parametrize("task", ["process_bounces", "tune_offers", "audit_security"])
def test_new_tasks_are_planned_and_handled(config, state, toolkit, task):
    from agent.engine import PLAN, Engine

    engine = Engine(config, state=state, sleep=lambda s: None)
    assert task in dict(PLAN) and task in engine.handlers
    assert engine.breaker.max_actions_per_cycle >= len(PLAN) + 10


def test_cli_entry_points():
    from dashboard.cli import build_parser

    p = build_parser()
    assert p.parse_args(["privacy", "forget", "a@b.c", "--yes"]).yes
    assert p.parse_args(["offers"]).func.__name__ == "cmd_offers"
    assert FakeSMTP is not None
