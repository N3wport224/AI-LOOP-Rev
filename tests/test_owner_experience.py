"""Phases 110-114: HTML digest, daily push, quiet --days, task timings, milestones."""

from cli import growth
from strategies import milestones
from strategies.owner_reports import OwnerReports
from tests import test_business_ops
from tests.test_business_ops import ctx
from tests.test_distribution import FakeSMTP
from tools import contact_policy as contact
from tools.http_client import Response
from tools.notify import new_topic
from tools.report_html import text_to_html
from tools.timings import describe, table, timings

kit = test_business_ops.kit  # the same live-mode toolkit fixture


def html_of(msg):
    part = msg.get_body(("html",))
    return part.get_content() if part is not None else ""


# ------------------------------------------------------------------ Phase 110: HTML digest
def test_text_becomes_safe_tidy_html():
    out = text_to_html("Intro line\n\nOn sale (1):\n- Python <Intel>  $19.00  https://me.github.io/py/.\n\nRun `am`.", "Report")
    assert "<h3>On sale (1):</h3><ul><li>Python &lt;Intel&gt;" in out
    assert '<a href="https://me.github.io/py/">https://me.github.io/py/</a>.' in out
    assert "<code>am</code>" in out and "<script" not in out and "<img" not in out


def test_digest_has_an_html_part_and_a_phone_line(kit, state, config, clock, transport):
    config.owner_email = "owner@me.example"
    config.ntfy_topic = new_topic()
    transport.add(f"https://ntfy.sh/{config.ntfy_topic}", Response(200, "x", b"{}", {}))
    assert OwnerReports().digest(kit, "owner@me.example")
    msg = FakeSMTP.sent[-1]
    assert "daily report" in msg["Subject"] and "<h2>AutoMonetize daily report" in html_of(msg)
    push = [c for c in transport.calls_to(f"https://ntfy.sh/{config.ntfy_topic}", "POST")
            if c["headers"].get("Title") == "AutoMonetize daily"]
    assert len(push) == 1 and push[0]["body"].decode().startswith("Yesterday $0.00 · 7 days $0.00 · ")
    config.digest_push = False
    state.set("owner_digest_date", None)
    OwnerReports().digest(kit, "owner@me.example")
    assert len([c for c in transport.calls_to(f"https://ntfy.sh/{config.ntfy_topic}", "POST")
                if c["headers"].get("Title") == "AutoMonetize daily"]) == 1


# ------------------------------------------------------------------ Phase 112: quiet for N days
def test_quiet_for_a_few_days_ends_by_itself(state, clock, monkeypatch, config, capsys):
    monkeypatch.setattr(growth, "_setup", lambda: (config, state, None))
    assert growth.quiet_main(["on", "--days", "7"]) == 0
    assert "until 2026-10-07 12:00 UTC" in capsys.readouterr().out
    assert contact.paused(state)
    clock.advance(days=8)
    assert contact.paused(state) is None
    assert growth.quiet_main(["on", "--days", "0"]) == 2 and growth.quiet_main(["on", "--days"]) == 2


# ------------------------------------------------------------------ Phase 113: timings
def test_timings_rank_tasks_by_total_time(state, clock, monkeypatch, config, capsys):
    for d in (2.0, 4.0):
        state.log_action(1, None, "build_intel", "ok", "", d)
    state.log_action(1, None, "sync_revenue", "ok", "", 0.5)
    state.log_action(1, None, "package_asset", "skipped", "on battery", 0.0)
    rows = timings(state)
    assert [r["task"] for r in rows] == ["build_intel", "sync_revenue"]
    assert rows[0] == {"task": "build_intel", "runs": 2, "avg_s": 3.0, "max_s": 4.0, "total_s": 6.0}
    assert describe(rows).startswith("Slowest tasks this week: build_intel 3.0s avg (max 4s)")
    assert "build_intel" in table(rows)
    monkeypatch.setattr(growth, "_setup", lambda: (config, state, None))
    growth.timings_main([])
    assert "build_intel" in capsys.readouterr().out
    clock.advance(days=8)
    assert timings(state) == []


# ------------------------------------------------------------------ Phase 114: milestones
def test_milestones_are_announced_once(kit, state, config, clock):
    config.owner_email, config.daily_target_cents = "owner@me.example", 5000
    OwnerReports().run("report_owner", ctx(kit))  # baseline: nothing yet
    assert state.get(milestones.KEY) == []
    state.record_revenue("stripe", "cs_1", 1900, 85, 1815, True)
    before = len(FakeSMTP.sent)
    OwnerReports().run("report_owner", ctx(kit))
    subjects = [m["Subject"] for m in FakeSMTP.sent[before:]]
    assert "🎉 Your first sale!" in subjects and not any("Goal" in s for s in subjects)
    state.record_revenue("stripe", "cs_2", 9000, 300, 8700, True)
    before = len(FakeSMTP.sent)
    OwnerReports().run("report_owner", ctx(kit))
    subjects = [m["Subject"] for m in FakeSMTP.sent[before:]]
    assert any(s.startswith("🎯 Goal reached") for s in subjects) and "🏁 $100.00 earned in total" in subjects
    assert "🎉 Your first sale!" not in subjects
    before = len(FakeSMTP.sent)
    OwnerReports().run("report_owner", ctx(kit))
    assert not any(m["Subject"][0] in "🎉🎯🏁" for m in FakeSMTP.sent[before:])


def test_an_existing_install_does_not_celebrate_history(kit, state, config):
    state.record_revenue("stripe", "cs_1", 1900, 85, 1815, True)
    assert milestones.announce(kit, "owner@me.example") is False
    assert "first_sale" in state.get(milestones.KEY)
