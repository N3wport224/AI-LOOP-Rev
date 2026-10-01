"""Phases 140-141 and 144: email lint, time/fraction setting checks, `automonetize audit`."""

import pytest

from agent.config_check import problems
from cli import growth
from tests import test_business_ops
from tools.dispatcher import Email
from tools.email_lint import EmailLintError, check, is_marketing
from tools.email_lint import problems as lint
from tools.self_audit import run_audit

kit = test_business_ops.kit  # the same live-mode toolkit fixture


def mail(subject="Your update", body="Hi", headers=None):
    return Email(to="a@co.example", subject=subject, body=body, kind="delivery", headers=headers or {})


# ------------------------------------------------------------------ Phase 140: email lint
def test_marketing_email_needs_unsubscribe_address_and_header(config, state):
    config.sender_postal_address = "1 Main St\nSpringfield"
    good = mail(body="New version.\n\n1 Main St\nSpringfield\nReply \"unsubscribe\" to stop.",
                headers={"List-Unsubscribe": "<mailto:u@me.example>"})
    assert lint(good, config, "release:1") == []
    bad = mail(subject="{title} is out", body="New version of None.")
    assert lint(bad, config, "release:2") == ["leftover placeholder: {title}", "no unsubscribe instruction in the body",
                                              "no postal address in the body", "no List-Unsubscribe header"]
    with pytest.raises(EmailLintError):
        check(bad, config, state, "release:2")
    with pytest.raises(EmailLintError):
        check(bad, config, state, "release:2")
    assert len([e for e in state.recent_errors(10) if e["source"] == "email_lint"]) == 1  # one alert per email


def test_transactional_email_is_only_checked_for_its_subject(config):
    assert lint(mail(body="Support: \"None of the links work\""), config, "order:1") == []
    assert lint(mail(subject=""), config, "order:1") == ["no subject"]
    assert not is_marketing("referral:3") and is_marketing("winback:9")


def test_the_dispatcher_refuses_a_failing_marketing_email(kit, config):
    with pytest.raises(EmailLintError):
        kit.dispatcher.send_transactional(mail(), audit_key="sale:1")


# ------------------------------------------------------------------ Phase 141: settings
def test_times_and_fractions_are_checked(config):
    config.subscription_timezone, config.owner_digest_hour = "Mars/Base", 24
    config.subscription_delivery_weekday, config.refund_alert_rate = 7, 10
    found = " | ".join(problems(config))
    for text in ("isn't a known time zone", "owner_digest_hour = 24", "subscription_delivery_weekday = 7",
                 "refund_alert_rate = 10: use a fraction"):
        assert text in found


# ------------------------------------------------------------------ Phase 144: audit
def test_audit_collects_every_check(config, state, monkeypatch, capsys, tmp_path):
    state.set("site_audit", {"broken_links": ["a → b"], "seo": [], "a11y": []})
    state.set("backup_verified", {"ok": True, "detail": "12 tables readable"})
    findings = run_audit(config, state, root=tmp_path, run=lambda *a, **k: type("R", (), {"returncode": 1, "stdout": ""})())
    areas = {f["area"]: f for f in findings}
    assert areas["website"]["status"] == "fail" and areas["backup restore test"]["status"] == "ok"
    assert areas["libraries"]["status"] == "ok" and findings[0]["status"] == "fail"
    monkeypatch.setattr(growth, "_setup", lambda: (config, state, None))
    monkeypatch.setattr("tools.self_audit.ROOT", tmp_path)
    assert growth.audit_main([]) == 1
    assert "website: 1 broken link(s)" in capsys.readouterr().out
