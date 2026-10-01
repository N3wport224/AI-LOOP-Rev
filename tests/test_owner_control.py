"""Phases 135-139: explain, email REPORT and QUIET N, mute alerts, what-changed, report --now."""

from agent.engine import PLAN
from agent.owner_commands import SUBJECT_RE, execute
from cli import growth
from strategies.owner_reports import OwnerReports
from tests import test_business_ops
from tests.test_business_ops import ctx, dataset
from tests.test_distribution import FakeSMTP
from tools import contact_policy as contact
from tools.alert_mute import active, is_muted, mute, sources, unmute
from tools.owner_views import explain, handler_for, what_changed

kit = test_business_ops.kit  # the same live-mode toolkit fixture


# ------------------------------------------------------------------ Phase 135: explain
def test_every_task_can_be_explained(state):
    for task, _ in PLAN:
        assert handler_for(task) is not None, task
    state.log_action(1, None, "build_site", "ok", "site: 12 files", 3.5)
    info = explain(state, "build_site")
    assert info["source"] == "strategies.inbound_syndicator" and info["purpose"].startswith("Organic inbound distribution")
    assert info["runs"][0]["detail"] == "site: 12 files" and info["of"] == len(PLAN)
    assert explain(state, "nope") is None and explain(state, "ops_checks")["purpose"].startswith("Ops checks")


# ------------------------------------------------------------------ Phase 136: email commands
def test_email_report_and_quiet_for_days(kit, state, clock):
    m = SUBJECT_RE.match("AM QUIET 7 ABC123")
    assert (m.group(1), m.group(2), m.group(3)) == ("QUIET", "7", "ABC123")
    m = SUBJECT_RE.match("Re: am report abc123")
    assert (m.group(1), m.group(2), m.group(3)) == ("report", None, "abc123")
    assert "for 7 day(s)" in execute(kit, "quiet", days=7)
    assert contact.quiet(state)["until"].startswith("2026-10-07")
    assert "Yesterday:" in execute(kit, "report")


# ------------------------------------------------------------------ Phase 137: mute
def test_muted_sources_are_logged_but_not_emailed(kit, state, config):
    config.owner_email = "owner@me.example"
    OwnerReports().run("report_owner", ctx(kit))  # baseline
    mute(state, "site_audit", days=3)
    state.log_error("site_audit", "2 broken links", kind="alert")
    before = len(FakeSMTP.sent)
    OwnerReports().run("report_owner", ctx(kit))
    assert not any("needs attention" in m["Subject"] for m in FakeSMTP.sent[before:])
    state.log_error("site_audit", "3 broken links", kind="alert")
    state.log_error("backup", "backup failed", kind="alert")
    OwnerReports().run("report_owner", ctx(kit))
    alert = [m for m in FakeSMTP.sent[before:] if "needs attention (1)" in m["Subject"]]
    body = alert[0].get_content() if alert and not alert[0].is_multipart() else alert[0].get_body(("plain",)).get_content()
    assert "backup failed" in body and "broken links" not in body
    assert is_muted(state, "site_audit") and sources(state) == ["backup", "site_audit"]
    assert unmute(state, "site_audit") and not active(state)


# ------------------------------------------------------------------ Phase 138: what changed
def test_what_changed_collects_everything(kit, state, config, clock):
    from tools.settings_log import record

    record(state, config)
    config.dry_run = not config.dry_run
    record(state, config)
    _, aid = dataset(kit, state, "python-remote", price=1900)
    state.update_asset(aid, price_cents=2400)
    mute(state, "ops_checks", days=2)
    text = "\n".join(what for _, what in what_changed(state))
    assert "settings: dry_run" in text and "price: python-remote Tech Stack Intel $19.00 → $24.00" in text
    assert "new version: python-remote Tech Stack Intel v1" in text and "alerts muted until" in text


# ------------------------------------------------------------------ Phase 139: report now
def test_report_now_prints_or_sends(kit, state, config, monkeypatch, capsys):
    monkeypatch.setattr(growth, "_setup", lambda: (config, state, None))
    assert growth.report_now_main(["--print"]) == 0
    assert "Yesterday:" in capsys.readouterr().out
    config.owner_email, config.dry_run = "owner@me.example", True
    assert growth.report_now_main([]) == 0
    assert "Report sent to owner@me.example (dry run" in capsys.readouterr().out
    log = (config.data_dir / "dispatched_audit.log").read_text()
    assert "AutoMonetize report (on request)" in log
