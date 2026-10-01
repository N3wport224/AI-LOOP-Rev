"""Phases 12-15: doctor/autostart, owner reports, catch-up catalog, outreach approval in the panel."""

import subprocess

import pytest

from cli.doctor import Doctor
from strategies.base import Strategy, TaskContext, TaskResult
from strategies.owner_reports import OwnerReports
from strategies.satellite_orchestrator import SATELLITE, SatelliteOrchestrator
from tests import test_gui
from tests.test_distribution import FakeSMTP, go_live
from tools import build_toolkit
from tools.catalog import live_products

gui = test_gui.gui  # shared fixture


def published(state, hid, title, url, kind="lead_directory", status="published", price=1900):
    aid = state.add_asset(hid, kind, title, "x.zip", 1, 10, price)
    state.update_asset(aid, provider="stripe", status=status, checkout_url=url)
    return aid


# ------------------------------------------------------------------ Phase 14: catalog + catch-up
def test_live_products_lists_each_buyable_link_once(state, make_hypothesis):
    hid = make_hypothesis()["id"]
    published(state, hid, "Python v1", "https://buy.stripe.com/abc")
    published(state, hid, "Python v2", "https://buy.stripe.com/abc")          # same link reused by a new version
    published(state, hid, "Test link", "https://buy.stripe.com/test_xyz")     # test mode
    published(state, hid, "Staged", "https://buy.stripe.com/def", status="staged")
    published(state, hid, "Dossier", "https://hooks.example.com/v1/dossiers", kind="dossier")
    published(state, hid, "API", "https://buy.stripe.com/api", kind="api_access", price=2900)
    products = live_products(state)
    assert [(p["title"], p["url"]) for p in products] == [("Python v2", "https://buy.stripe.com/abc"),
                                                           ("API", "https://buy.stripe.com/api")]


def test_unlisted_satellite_datasets_are_published_without_waiting(toolkit, state, make_hypothesis, monkeypatch):
    sat = make_hypothesis()
    state.set_hypothesis_status(sat["id"], SATELLITE, "satellite niche")
    aid = published(state, sat["id"], "ML dataset", "", status="staged")

    class Publisher(Strategy):
        tasks = ("publish_listing",)

        def run(self, task, ctx):
            state.update_asset(aid, status="published", checkout_url="https://buy.stripe.com/ml")
            return TaskResult(True, "live", {"published": True})

    monkeypatch.setattr(SatelliteOrchestrator, "handlers", staticmethod(lambda: {"publish_listing": Publisher()}))
    orch = SatelliteOrchestrator()
    assert orch.catch_up(toolkit) == [sat["params"]["niche"]]
    assert orch.catch_up(toolkit) == []  # already on sale: nothing more to do
    assert [p["title"] for p in live_products(state)] == ["ML dataset"]


# ------------------------------------------------------------------ Phase 13: owner reports
@pytest.fixture
def live_kit(config, state, breaker, transport):
    FakeSMTP.sent, FakeSMTP.fail = [], False
    go_live(config)
    config.sender_email, config.sender_name = "owner@example.com", "Owner"
    config.subscription_timezone, config.owner_digest_hour = "UTC", 8
    return build_toolkit(config, state, breaker, transport=transport, sleep=lambda s: None, smtp_factory=FakeSMTP)


def report(kit, hyp):
    return OwnerReports().run("report_owner", TaskContext(kit, hyp, {}))


def test_sale_and_alert_emails_once_each(live_kit, state, make_hypothesis, clock):
    hyp = make_hypothesis()
    clock.now = clock.now.replace(hour=7)  # before the digest hour
    state.record_order("stripe", "cs_old", "old@buyer.example", 1400, "plink", None, hyp["id"])
    assert report(live_kit, hyp).metrics["sent"] == 0 and FakeSMTP.sent == []  # history isn't re-announced
    aid = published(state, hyp["id"], "Python Remote Tech Stack Intel", "https://buy.stripe.com/abc")
    state.record_order("stripe", "cs_new", "new@buyer.example", 1900, "plink", aid, hyp["id"])
    state.log_error("engine", "QUARANTINE until later: network", kind="alert")
    res = report(live_kit, hyp)
    assert res.metrics["kinds"] == ["sales", "alerts"]
    sale, alert = FakeSMTP.sent
    assert sale["To"] == "owner@example.com" and sale["Subject"] == "💰 New sale: $19.00"
    assert "Python Remote Tech Stack Intel" in sale.get_body(("plain",)).get_content()
    assert "needs attention" in alert["Subject"] and "QUARANTINE" in alert.get_body(("plain",)).get_content()
    assert report(live_kit, hyp).metrics["sent"] == 0  # nothing new


def test_daily_digest_after_the_hour_once_a_day(live_kit, state, make_hypothesis, clock, config):
    hyp = make_hypothesis()
    published(state, hyp["id"], "Python Remote Tech Stack Intel", "https://buy.stripe.com/abc")
    state.stage_outreach(hyp["id"], "k1", "email", "hr@co.example", "Subject", "Body", 0.9)
    clock.now = clock.now.replace(hour=7)
    report(live_kit, hyp)
    assert not [m for m in FakeSMTP.sent if "daily report" in m["Subject"]]
    clock.now = clock.now.replace(hour=9)
    report(live_kit, hyp)
    report(live_kit, hyp)
    digests = [m for m in FakeSMTP.sent if "daily report" in m["Subject"]]
    assert len(digests) == 1
    body = digests[0].get_body(("plain",)).get_content()
    assert "https://buy.stripe.com/abc" in body and "waiting for your OK: 1" in body and "connect-marketing" in body
    clock.advance(days=1)
    report(live_kit, hyp)
    assert len([m for m in FakeSMTP.sent if "daily report" in m["Subject"]]) == 2


def test_failed_send_is_retried_and_reports_can_be_off(live_kit, state, make_hypothesis, clock, config):
    hyp = make_hypothesis()
    clock.now = clock.now.replace(hour=7)
    report(live_kit, hyp)
    state.record_order("stripe", "cs_1", "b@x.example", 1900, "plink", None, hyp["id"])
    FakeSMTP.fail = True
    assert report(live_kit, hyp).metrics["sent"] == 0
    FakeSMTP.fail = False
    assert report(live_kit, hyp).metrics["kinds"] == ["sales"]  # not lost
    config.owner_reports = False
    assert "off" in report(live_kit, hyp).summary


# ------------------------------------------------------------------ Phase 12: doctor / autostart
class Ctl:
    def __init__(self, pid=None, launchd=False):
        self.pid, self.launchd, self.calls = pid, launchd, []

    def supervisor_pid(self):
        return self.pid

    def launchd_managed(self):
        return self.launchd

    def start(self):
        self.calls.append("start")
        self.pid = 4242
        return {"ok": True, "message": "Started a background supervisor."}

    def restart(self):
        self.calls.append("restart")
        return {"ok": True, "message": "Restarted."}


def runner(outputs):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        key = " ".join(cmd[:2]) if cmd[0] != "git" else " ".join(cmd[:3])
        code, out = outputs.get(key, (0, ""))
        if callable(out):
            out = out()
        return subprocess.CompletedProcess(cmd, code, out, "")
    run.calls = calls
    return run


def test_doctor_reports_and_fixes_what_it_safely_can(config, state, tmp_path, monkeypatch):
    run = runner({"pmset -g": (0, " sleep                10\n displaysleep 10\n"), "git rev-list --count": (0, "3\n"),
                  "git status --porcelain": (0, "")})
    ctl = Ctl()
    doc = Doctor(config, state, ctl, run=run, system="Darwin", sleep=lambda s: None)
    findings = {f.name: f for f in doc.checks()}
    assert findings["Agent running"].status == "fail"
    assert findings["Starts after a reboot"].status == "warn" and findings["Starts after a reboot"].fix == "automonetize autostart"
    assert findings["Real payments"].status == "warn" and "go-live" in findings["Real payments"].fix
    assert findings["On sale"].status == "warn" and findings["Marketing"].status == "warn"
    assert findings["Mac stays awake"].status == "warn" and "pmset" in findings["Mac stays awake"].fix
    assert findings["Up to date"].status == "warn" and "3 update" in findings["Up to date"].detail
    assert "started" in findings["Agent running"].auto().lower() and ctl.pid == 4242
    assert "updated and restarted" in findings["Up to date"].auto() and ["git", "pull", "--ff-only"] in run.calls


def test_doctor_never_pulls_over_local_changes(config, state):
    run = runner({"git status --porcelain": (0, " M strategies/x.py\n")})
    doc = Doctor(config, state, Ctl(pid=1), run=run, system="Linux")
    assert "local changes" in doc.update_code() and ["git", "pull", "--ff-only"] not in run.calls


def test_autostart_hands_the_process_over_to_launchd(config, state, monkeypatch):
    ctl = Ctl(pid=999)
    killed = []

    def kill(pid, sig):
        killed.append(pid)
        ctl.pid = None

    monkeypatch.setattr("cli.doctor.os.kill", kill)

    def installed():
        ctl.launchd, ctl.pid = True, 1001
        return "installed"

    run = runner({})
    real = run

    def run_with_install(cmd, **kw):
        if cmd[0].endswith("install_launchd.sh"):
            installed()
        return real(cmd, **kw)

    doc = Doctor(config, state, ctl, run=run_with_install, system="Darwin", sleep=lambda s: None)
    msg = doc.enable_autostart()
    assert killed == [999] and msg.startswith("installed")
    assert any(c[0].endswith("install_launchd.sh") and c[1:] == ["--service", "agent"] for c in run.calls)
    assert "macOS-only" in Doctor(config, state, Ctl(), run=run, system="Linux").enable_autostart()


# ------------------------------------------------------------------ Phase 15: outreach in the panel
def test_outreach_review_in_the_control_panel(gui, state, make_hypothesis):
    hyp = make_hypothesis()
    low = state.stage_outreach(hyp["id"], "k1", "email", "a@co.example", "Low", "Body A", 0.4)
    high = state.stage_outreach(hyp["id"], "k2", "email", "b@co.example", "High", "Body B", 0.9)

    async def scenario(client):
        csrf = await test_gui.login(client)
        data = await (await client.get("/api/outreach")).json()
        assert [d["subject"] for d in data["drafts"]] == ["High", "Low"]  # best first
        assert data["send_problems"]  # no postal address etc. in the test config: the panel says so
        r = await client.post("/api/outreach", json={"action": "approve", "ids": [high]})
        assert r.status == 403  # CSRF token required
        r = await client.post("/api/outreach", json={"action": "approve", "ids": [high]}, headers={"X-CSRF-Token": csrf})
        assert (await r.json())["changed"] == 1
        r = await client.post("/api/outreach", json={"action": "approve", "ids": [high]}, headers={"X-CSRF-Token": csrf})
        assert (await r.json())["changed"] == 0  # already decided: never re-approved or flipped
        for bad in ({"action": "send", "ids": [low]}, {"action": "reject", "ids": []}, {"action": "reject", "ids": ["1"]},
                    {"action": "reject", "ids": [True]}):
            assert (await client.post("/api/outreach", json=bad, headers={"X-CSRF-Token": csrf})).status == 400
        r = await client.post("/api/outreach", json={"action": "reject", "ids": [low]}, headers={"X-CSRF-Token": csrf})
        assert (await r.json())["changed"] == 1
        assert (await (await client.get("/api/outreach")).json())["drafts"] == []
        assert (await client.get("/api/outreach?status=bogus")).status == 400

    test_gui.run(gui, scenario)
    assert {r["id"]: r["status"] for r in state.list_outreach(limit=10)} == {low: "rejected", high: "approved"}


def test_engine_plans_the_new_tasks(config, state, toolkit):
    from agent.engine import PLAN, Engine

    assert ("report_owner", 98) in PLAN
    assert "report_owner" in Engine(config, state=state, toolkit=toolkit, sleep=lambda s: None).handlers
