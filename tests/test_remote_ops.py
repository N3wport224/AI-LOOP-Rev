"""Phases 60-64: phone notifications, email commands, quiet mode, report preferences, Health tab."""

from datetime import datetime, timezone

import pytest

from agent.owner_commands import SUBJECT_RE, code, execute, poll
from strategies.owner_reports import OwnerReports
from tests import test_business_ops, test_gui
from tests.test_business_ops import ctx, dataset
from tests.test_distribution import FakeSMTP
from tools import contact_policy as contact
from tools.http_client import Response
from tools.notify import enabled, new_topic, push

kit = test_business_ops.kit  # the same live-mode toolkit fixture
gui = test_gui.gui            # the control-panel fixture


def body_of(msg):
    return msg.get_body(("plain",)).get_content() if msg.is_multipart() else msg.get_content()


# ------------------------------------------------------------------ Phase 60: phone notifications
def test_push_goes_to_the_topic_and_never_raises(kit, config, transport):
    assert not enabled(config) and push(kit.http, config, "t", "m") is False
    config.ntfy_topic = new_topic()
    assert enabled(config) and len(config.ntfy_topic) >= 20
    transport.add(f"https://ntfy.sh/{config.ntfy_topic}", Response(200, "x", b"{}", {}))
    assert push(kit.http, config, "💰 New sale: $19", "Py Intel", tags="moneybag") is True
    call = transport.calls_to(f"https://ntfy.sh/{config.ntfy_topic}", "POST")[-1]
    assert call["body"] == b"Py Intel" and call["headers"]["Title"] == "New sale: $19"
    config.ntfy_topic = "am-unreachable-topic-0000"
    assert push(kit.http, config, "t", "m") is False  # 404 from the fake server: swallowed


def test_sales_and_alerts_buzz_the_phone(kit, state, config, transport):
    config.ntfy_topic, config.owner_email = "am-testtopic123456", "owner@me.example"
    transport.add("https://ntfy.sh/am-testtopic123456", Response(200, "x", b"{}", {}))
    OwnerReports().run("report_owner", ctx(kit))  # first run: start from now
    _, aid = dataset(kit, state, "python-remote")
    state.record_order("stripe", "cs_1", "buyer@co.example", 1900, None, aid, None, status="delivered")
    state.log_error("x", "something broke", kind="alert")
    OwnerReports().run("report_owner", ctx(kit))
    bodies = [c["body"] for c in transport.calls_to("https://ntfy.sh/am-testtopic123456", "POST")]
    assert b"python-remote Tech Stack Intel" in bodies and b"something broke" in bodies
    assert all(b"buyer@co.example" not in b for b in bodies)  # no customer addresses leave the Mac


def test_daily_sale_alerts_dont_buzz_the_phone_per_sale(kit, state, config, transport):
    # Audit after Phase 400: "daily (digest only)" and "off" mean no per-sale notification, the phone included.
    config.ntfy_topic, config.owner_email, config.sale_alerts = "am-testtopic123456", "owner@me.example", "daily"
    transport.add("https://ntfy.sh/am-testtopic123456", Response(200, "x", b"{}", {}))
    OwnerReports().run("report_owner", ctx(kit))
    _, aid = dataset(kit, state, "python-remote")
    state.record_order("stripe", "cs_1", "buyer@co.example", 1900, None, aid, None, status="delivered")
    OwnerReports().run("report_owner", ctx(kit))
    bodies = [c["body"] for c in transport.calls_to("https://ntfy.sh/am-testtopic123456", "POST")]
    assert not any(b"python-remote Tech Stack Intel" in b for b in bodies)


# ------------------------------------------------------------------ Phase 63: preferences
def test_sale_emails_and_digest_follow_preferences(kit, state, config, clock):
    config.owner_email = "owner@me.example"
    OwnerReports().run("report_owner", ctx(kit))
    config.sale_alerts = "daily"
    _, aid = dataset(kit, state, "python-remote")
    state.record_order("stripe", "cs_1", "buyer@co.example", 1900, None, aid, None, status="delivered")
    before = len(FakeSMTP.sent)
    OwnerReports().run("report_owner", ctx(kit))
    assert not any("New sale" in m["Subject"] for m in FakeSMTP.sent[before:])
    assert state.get("owner_last_order_id") == state.get_order("stripe", "cs_1")["id"]  # not re-sent later either
    config.owner_digest = "weekly"
    reports = OwnerReports()
    assert reports.digest(kit, "owner@me.example") is False  # NOW is a Wednesday
    clock.now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)  # a Monday
    assert reports.digest(kit, "owner@me.example") is True


# ------------------------------------------------------------------ Phase 62: quiet mode
def test_quiet_mode_holds_marketing_but_not_purchases(kit, state, config):
    contact.set_quiet(state, True, "test")
    assert "quiet" in contact.blocked(state, config, "a@co.example", "followup")
    from strategies.distribution_engine import DistributionEngine, fulfil_order

    assert DistributionEngine().run("dispatch_outreach", ctx(kit)).metrics.get("paused")
    _, aid = dataset(kit, state, "python-remote")
    state.record_order("stripe", "cs_1", "buyer@co.example", 1900, "plink_python-remote", aid, None)
    assert fulfil_order(kit, state.get_order("stripe", "cs_1")) == "delivered"
    contact.set_quiet(state, False)
    assert contact.blocked(state, config, "a@co.example", "followup") is None


# ------------------------------------------------------------------ Phase 61: email commands
@pytest.mark.parametrize("subject,cmd", [("AM STATUS 7F3A9C", "status"), ("Re: am pause 7f3a9c", "pause"),
                                         ("AM status", None), ("Hello", None)])
def test_command_subjects(subject, cmd):
    m = SUBJECT_RE.match(subject)
    assert (m.group(1).lower() if m else None) == cmd


def test_commands_from_the_owner_with_the_code_run_and_reply(kit, state, config):
    config.owner_email = "owner@me.example"
    secret = code(state)
    inbox = [
        {"sender": "owner@me.example", "subject": f"AM PAUSE {secret}", "message_id": "<1>", "body": ""},
        {"sender": "owner@me.example", "subject": "AM RESUME 000000", "message_id": "<2>", "body": ""},  # wrong code
        {"sender": "owner@me.example", "subject": f"AM QUIET {secret}", "message_id": "<3>", "body": ""},
    ]
    seen = []

    def scan(host, user, pw, wanted, since, unseen_only=True):
        seen.append((wanted("owner@me.example"), wanted("evil@x.example"), unseen_only))
        return inbox

    assert poll(kit, scan=scan) == 2
    assert seen == [(True, False, False)]
    assert state.get("paused") and contact.quiet(state)
    replies = [m for m in FakeSMTP.sent if m["To"] == "owner@me.example"]
    assert [m["Subject"] for m in replies[-2:]] == ["AM PAUSE: done", "AM QUIET: done"]
    assert poll(kit, scan=scan) == 0  # every 5 minutes, and each message once
    state.set("owner_commands_polled_at", None)
    inbox.append({"sender": "owner@me.example", "subject": f"AM RESUME {secret}", "message_id": "<4>", "body": ""})
    assert poll(kit, scan=scan) == 1 and not state.get("paused")


def test_status_and_todo_replies(kit, state):
    assert "Today: $" in execute(kit, "status") and "Engine: running" in execute(kit, "status")
    assert "Connect marketing" in execute(kit, "todo")
    assert "STATUS, TODO, REPORT" in execute(kit, "help")


def test_the_digest_explains_email_commands(kit, state, config, clock):
    config.owner_email = "owner@me.example"
    text = OwnerReports.digest_body(kit, clock())
    assert f"AM <COMMAND> {code(state)}" in text


def test_commands_run_even_while_paused(config, state, toolkit, monkeypatch):
    from agent.engine import Engine

    calls = []
    monkeypatch.setattr("agent.owner_commands.poll", lambda tools, engine=None, scan=None: calls.append(engine) or 0)
    engine = Engine(config, state=state, toolkit=toolkit, sleep=lambda s: None)
    engine.pause("test")
    assert engine.run_cycle().status == "paused" and calls == [engine]


# ------------------------------------------------------------------ Phase 64: Health tab
def test_health_tab_lists_findings_and_toggles_quiet(gui, state):
    async def scenario(client):
        csrf = await test_gui.login(client)
        data = await (await client.get("/api/health")).json()
        assert data["findings"] and data["findings"][0]["status"] in ("fail", "warn") and data["quiet"] is False
        assert {"name", "status", "detail", "fix", "can_fix"} <= set(data["findings"][0])
        assert (await client.post("/api/quiet", json={"on": True})).status == 403  # CSRF
        r = await client.post("/api/quiet", json={"on": True}, headers={"X-CSRF-Token": csrf})
        assert (await r.json())["ok"] and (await (await client.get("/api/health")).json())["quiet"] is True
        r = await client.post("/api/health/fix", json={"name": "Nothing like this"}, headers={"X-CSRF-Token": csrf})
        assert r.status == 409

    test_gui.run(gui, scenario)
    assert contact.quiet(state)


def test_cli_entry_points():
    from dashboard.cli import build_parser

    p = build_parser()
    assert p.parse_args(["quiet", "on"]).mode == "on" and p.parse_args(["commands", "--new"]).new
    assert p.parse_args(["phone"]).func.__name__ == "cmd_phone"
