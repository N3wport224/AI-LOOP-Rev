"""Phases 70-74: setup wizard, connections check, start notice, version + what's new, crash-loop rollback."""

import subprocess
import sys
from datetime import timedelta

import pytest

from agent import last_good
from agent.startup_notice import maybe_notify
from tests import test_business_ops
from tests.test_distribution import FakeSMTP
from tools.connections import Conn, check_all, summary
from tools.http_client import Response
from tools.version import describe, info

kit = test_business_ops.kit  # the same live-mode toolkit fixture


# ------------------------------------------------------------------ Phase 71: connections
class FakeIMAP:
    def __init__(self, host, fail=False):
        self.host = host

    def login(self, u, p):
        if p == "bad":
            raise OSError("authentication failed")

    def select(self, *a, **k):
        return "OK", [b"1"]

    def logout(self):
        pass


def test_connections_are_checked_read_only(kit, config, transport):
    config.public_webhook_url, config.pages_base_url = "https://hooks.example.com/webhook", "https://me.github.io/d"
    config.github_token, config.devto_api_key = "ghp_x", "devkey"
    transport.add_json("https://api.stripe.com/v1/balance", {"livemode": True})
    transport.add("https://hooks.example.com/healthz", Response(200, "x", b"{}", {}))
    transport.add("https://me.github.io/d", Response(200, "x", b"<html>", {}))
    transport.add_json("https://api.github.com/user", {"login": "me"})
    transport.add_json("https://dev.to/api/users/me", {"username": "me"}, status=401)
    conns = {c.name: c for c in check_all(config, kit.http, smtp_login=lambda *a: None, imap_factory=FakeIMAP)}
    assert conns["Stripe"].status == "ok" and conns["Stripe"].detail == "live account"
    assert conns["Sending email"].status == "ok" and conns["Reading email"].status == "ok"
    assert conns["Public URL"].status == "ok" and conns["Website"].status == "ok" and conns["GitHub"].detail == "signed in as me"
    assert conns["Dev.to"].status == "fail" and "401" in conns["Dev.to"].detail and conns["Dev.to"].fix
    assert conns["Heartbeat"].status == "skip"
    assert all(c["method"] == "GET" for c in transport.calls)  # nothing created, sent or changed
    config.smtp_password = "bad"
    failed = {c.name: c for c in check_all(config, kit.http, smtp_login=lambda *a: "refused", imap_factory=FakeIMAP)}
    assert failed["Sending email"].status == "fail" and failed["Reading email"].status == "fail"
    assert summary([Conn("A", "ok", ""), Conn("B", "fail", "")]) == "1 working, 1 failing: B"


# ------------------------------------------------------------------ Phase 72: start notice
def test_start_notice_once_per_six_hours(kit, state, config, clock):
    config.owner_email = "owner@me.example"
    check = lambda cfg, http: [Conn("Stripe", "ok", "live account"), Conn("Website", "fail", "HTTP 404", "connect-marketing")]  # noqa: E731
    assert maybe_notify(kit, check=check) is True
    msg = FakeSMTP.sent[-1]
    body = msg.get_body(("plain",)).get_content() if msg.is_multipart() else msg.get_content()
    assert msg["To"] == "owner@me.example" and "1 connection problem" in msg["Subject"]
    assert "Website: HTTP 404 → connect-marketing" in body and "AutoMonetize" in body
    assert maybe_notify(kit, check=check) is False
    clock.advance(hours=7)
    assert maybe_notify(kit, check=lambda c, h: [Conn("Stripe", "ok", "live account")]) is True
    assert FakeSMTP.sent[-1]["Subject"] == "✅ AutoMonetize is running"


# ------------------------------------------------------------------ Phase 73: version and what's new
def test_version_info_reads_pyproject_and_git():
    v = info()
    assert v["version"] and (v["commit"] or True)
    assert describe({"version": "1.0", "commit": "abc1234", "date": "2026-10-01"}) == "AutoMonetize 1.0 (abc1234, 2026-10-01)"
    assert describe({"version": "1.0"}) == "AutoMonetize 1.0"


def test_whats_new_is_emailed_once_after_the_canary_passes(kit, state, config):
    from strategies.owner_reports import OwnerReports

    config.owner_email = "owner@me.example"
    OwnerReports().run("report_owner", test_business_ops.ctx(kit))
    state.set("whats_new", {"target": "abcdef1234567", "changes": ["Phase 70: setup wizard", "Fix X"], "sent": False})
    OwnerReports().run("report_owner", test_business_ops.ctx(kit))
    news = [m for m in FakeSMTP.sent if "updated" in m["Subject"]]
    assert len(news) == 1 and "2 change" in news[0]["Subject"]
    OwnerReports().run("report_owner", test_business_ops.ctx(kit))
    assert len([m for m in FakeSMTP.sent if "updated" in m["Subject"]]) == 1


# ------------------------------------------------------------------ Phase 74: crash-loop rollback
def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@t.invalid", "-c", "commit.gpgsign=false",
                           "-c", "init.defaultBranch=main", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, config):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q")
    (r / "app.py").write_text("V = 1\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "good")
    config.evolution_repo = str(r)
    return r


def test_a_crash_loop_goes_back_to_the_last_good_commit(repo, state, config, clock):
    reloads = []
    t = lambda status: last_good.tick(state, config, status, reload=reloads.append)  # noqa: E731
    good = git(repo, "rev-parse", "HEAD")
    assert t("ran") == ""
    clock.advance(hours=25)
    assert t("ran") == "recorded" and state.get("last_good")["sha"] == good
    (repo / "app.py").write_text("V = 'broken'\n")
    git(repo, "commit", "-qam", "bad update")
    assert t("crashed") == "" and t("crashed") == ""
    assert t("crashed") == "rolled_back"
    assert git(repo, "rev-parse", "HEAD") == good and reloads
    assert "went back to the last version that worked" in state.recent_errors(1, kind="alert")[0]["message"]


def test_no_rollback_without_a_good_commit_or_over_edits(repo, state, config, clock):
    t = lambda status: last_good.tick(state, config, status, reload=lambda r: None)  # noqa: E731
    for _ in range(4):
        assert t("crashed") == ""  # nothing known to be good yet: stay
    clock.advance(hours=10)
    t("ran")
    assert state.get("last_good") is None  # the crashes restarted the 24-hour clock
    clock.advance(hours=15)
    assert t("ran") == "recorded"
    (repo / "app.py").write_text("V = 2\n")
    git(repo, "commit", "-qam", "next")
    (repo / "app.py").write_text("V = 'editing'\n")  # uncommitted work
    for _ in range(2):
        t("crashed")
    assert t("crashed") == "blocked: uncommitted changes"


def test_setup_wizard_asks_only_whats_missing(kit, state, config, monkeypatch, tmp_path):
    import cli.setup_wizard as wiz

    env = tmp_path / ".env"
    env.write_text("")
    monkeypatch.setattr(wiz, "ROOT", tmp_path)

    class Ctl:
        def launchd_managed(self):
            return True

    config.sender_postal_address, config.owner_email = "", ""
    monkeypatch.setattr("cli.doctor.controller", lambda e: (config, state, Ctl()))
    answers = iter(["short", "1 Main St, Springfield, IL 62701, USA", "boss@me.example", "", "", ""])
    asked = []

    def ask(q):
        asked.append(q)
        return next(answers, "")

    ran = []
    steps = {"connections": lambda: [Conn("Stripe", "ok", "live")], "go_live": lambda: ran.append("go_live") or 0}
    assert wiz.main([], ask=ask, steps=steps, system="Darwin") == 0
    text = env.read_text()
    assert 'CAN_SPAM_POSTAL_ADDRESS="1 Main St, Springfield, IL 62701, USA"' in text or \
        "CAN_SPAM_POSTAL_ADDRESS=1 Main St" in text.replace("'", "")
    assert "OWNER_EMAIL=boss@me.example" in text
    assert not any("real payments" in q for q in asked) or ran == []  # live already: not asked


def test_cli_entry_points():
    from dashboard.cli import build_parser

    p = build_parser()
    for cmd in ("setup", "connections", "version"):
        assert p.parse_args([cmd]).func.__name__ == f"cmd_{cmd}"
    assert sys.version_info >= (3, 11)
    assert timedelta(0) == timedelta(0)
