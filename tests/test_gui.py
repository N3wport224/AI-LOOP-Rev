"""Local control GUI: access control, credentials manager, supervision controls, status and logs."""

import asyncio
import json
import os
import signal
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agent.setup_autonomous import Check
from gui.auth import Auth, load_or_create_token
from gui.context import GuiContext
from gui.control import ServiceController, engine_state
from gui.routes.control import redact
from gui.server import build_app, check_loopback

TOKEN = "t" * 43
ENV = """# Stripe (keep this comment)
STRIPE_SECRET_KEY=sk_test_0123456789abcdefWXYZ
export SMTP_PASSWORD='old pass'
UNRELATED=keep-me
"""


class FakeController:
    def __init__(self):
        self.calls = []
        self.pid = None
        self.config = None

    def supervisor_pid(self):
        return self.pid

    def status(self):
        return {"running": bool(self.pid), "pid": self.pid, "since": None, "workers": [], "mode": "process" if self.pid else "none",
                "engine": "running" if self.pid else "not running", "engine_flag": "running", "reason": ""}

    def act(self, action):
        self.calls.append(action)
        return {"ok": True, "message": f"did {action}"}


@pytest.fixture
def gui(tmp_path, config, state):
    env_file = tmp_path / ".env"
    env_file.write_text(ENV)
    env_file.chmod(0o600)
    toml = tmp_path / "a.toml"
    toml.write_text(f'[automonetize]\ndata_dir = "{config.data_dir}"\nnetwork_check_hosts = []\n')
    preflights = []

    def preflight(cfg):
        preflights.append(cfg)
        return [Check("validate", "STRIPE_SECRET_KEY", "warn", "TEST mode key"), Check("preflight", "stripe api", "ok", "authenticated")]

    logs = tmp_path / "agent.log"
    logs.write_text("boot\nusing key sk_live_ABCDEFGHIJKLMNOP and whsec_abcdefghijk\nsmtp password=hunter2 ok\n")
    ctl = FakeController()
    g = GuiContext(config=config, config_path=str(toml), env_file=env_file, workdir=tmp_path, state=state, controller=ctl,
                   auth=Auth(TOKEN), port=0, preflight=preflight, webhook_probe=lambda c: {"healthy": True, "events": {"processed": 3}},
                   log_paths=[logs])
    g.preflights = preflights
    return g


def run(gui, scenario):
    async def go():
        async with TestClient(TestServer(build_app(gui))) as client:
            gui.port = client.port
            await scenario(client)

    asyncio.run(go())


async def login(client):
    r = await client.post("/login", data={"token": TOKEN}, allow_redirects=False)
    assert r.status == 302
    r = await client.get("/api/session")
    return (await r.json())["csrf"]


# ------------------------------------------------------------------ access control
def test_token_file_is_private_and_stable(tmp_path):
    token = load_or_create_token(tmp_path)
    path = tmp_path / ".gui_token"
    assert len(token) >= 40 and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert load_or_create_token(tmp_path) == token


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "hooks.example.com", "::"])
def test_refuses_to_bind_off_loopback(host):
    with pytest.raises(ValueError):
        check_loopback(host)


def test_everything_needs_a_session(gui):
    async def s(client):
        r = await client.get("/", allow_redirects=False)
        assert r.status == 302 and r.headers["Location"] == "/login"
        assert (await client.get("/api/status")).status == 401
        assert (await client.get("/api/settings")).status == 401
        assert (await client.post("/api/control", json={"action": "kill", "confirm": True})).status in (401, 403)
        r = await client.get("/login")
        assert r.status == 200 and "script-src 'self'" in r.headers["Content-Security-Policy"]
        assert r.headers["X-Frame-Options"] == "DENY"
    run(gui, s)
    assert gui.controller.calls == []


def test_dns_rebinding_and_cross_origin_are_refused(gui):
    async def s(client):
        csrf = await login(client)
        r = await client.get("/api/status", headers={"Host": "evil.example:8080"})
        assert r.status == 421
        r = await client.post("/api/control", json={"action": "pause"},
                              headers={"X-CSRF-Token": csrf, "Origin": "https://evil.example"})
        assert r.status == 403
        r = await client.post("/api/control", json={"action": "pause"})  # no CSRF header
        assert r.status == 403
        r = await client.post("/api/control", json={"action": "pause"}, headers={"X-CSRF-Token": "forged"})
        assert r.status == 403
        r = await client.post("/login", data={"token": TOKEN}, headers={"Origin": "null"}, allow_redirects=False)
        assert r.status == 403
    run(gui, s)
    assert gui.controller.calls == []


def test_login_brute_force_is_throttled_and_launch_codes_are_single_use(gui):
    async def s(client):
        for _ in range(10):
            assert (await client.post("/login", data={"token": "guess"})).status == 401
        assert (await client.post("/login", data={"token": TOKEN})).status == 429
        code = gui.auth.issue_launch_code()
        r = await client.get(f"/auth/launch?code={code}", allow_redirects=False)
        assert r.status == 302 and "am_gui" in r.cookies and "samesite=strict" in r.headers["Set-Cookie"].lower()
        assert "httponly" in r.headers["Set-Cookie"].lower()
        assert (await client.get("/api/session")).status == 200
        client.session.cookie_jar.clear()
        assert (await client.get(f"/auth/launch?code={code}")).status == 401  # already used
    run(gui, s)


def test_static_assets_have_no_inline_script():
    root = Path(__file__).resolve().parents[1] / "gui" / "static"
    index = (root / "index.html").read_text()
    assert "<script>" not in index and " onclick=" not in index and 'src="/static/app.js"' in index
    assert ".innerHTML" not in (root / "app.js").read_text()


# ------------------------------------------------------------------ settings
def test_settings_never_send_secrets_to_the_browser(gui):
    async def s(client):
        await login(client)
        r = await client.get("/api/settings")
        text = await r.text()
        data = json.loads(text)
        assert "sk_test_0123456789abcdefWXYZ" not in text and "old pass" not in text
        by = {f["key"]: f for f in data["fields"]}
        assert by["STRIPE_SECRET_KEY"]["set"] and by["STRIPE_SECRET_KEY"]["hint"] == "•••• WXYZ"
        assert by["STRIPE_SECRET_KEY"]["value"] == "" and by["STRIPE_MODE"]["value"] == "test"
        assert by["SMTP_PASSWORD"]["secret"] and by["SMTP_PASSWORD"]["set"]
        assert {g["id"] for g in data["groups"]} == {"stripe", "mailer", "syndication", "tunnel", "compliance", "evolution"}
        for key in ("STRIPE_WEBHOOK_SECRET", "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SENDGRID_API_KEY", "POSTMARK_SERVER_TOKEN",
                    "DEVTO_API_KEY", "HASHNODE_TOKEN", "GITHUB_TOKEN", "TUNNEL_HOSTNAME", "CAN_SPAM_POSTAL_ADDRESS",
                    "CAN_SPAM_UNSUBSCRIBE_EMAIL"):
            assert key in by
    run(gui, s)


@pytest.mark.parametrize("values,field", [
    ({"STRIPE_SECRET_KEY": "sk_live_0123456789abcdef"}, "STRIPE_SECRET_KEY"),       # live key while mode is test
    ({"STRIPE_MODE": "live"}, "STRIPE_SECRET_KEY"),                                 # existing key is test-mode
    ({"STRIPE_SECRET_KEY": "pk_test_0123456789abcdef"}, "STRIPE_SECRET_KEY"),       # publishable key
    ({"STRIPE_WEBHOOK_SECRET": "abc"}, "STRIPE_WEBHOOK_SECRET"),
    ({"SMTP_HOST": "smtp.example.com\nDRY_RUN=false"}, "SMTP_HOST"),                # .env injection
    ({"SMTP_USER": "me\x00"}, "SMTP_USER"),
    ({"SMTP_PORT": "99999"}, "SMTP_PORT"),
    ({"SENDGRID_API_KEY": "nope-not-sg"}, "SENDGRID_API_KEY"),
    ({"AUTOMONETIZE_SENDER_EMAIL": "not-an-email"}, "AUTOMONETIZE_SENDER_EMAIL"),
    ({"TUNNEL_HOSTNAME": "https://hooks.example.com"}, "TUNNEL_HOSTNAME"),
    ({"TUNNEL_HOSTNAME": "abc.trycloudflare.com"}, "TUNNEL_HOSTNAME"),
    ({"CAN_SPAM_POSTAL_ADDRESS": "nowhere"}, "CAN_SPAM_POSTAL_ADDRESS"),
    ({"AUTOMONETIZE_PAGES_BASE_URL": "http://me.github.io"}, "AUTOMONETIZE_PAGES_BASE_URL"),
    ({"DRY_RUN": "maybe"}, "DRY_RUN"),
    ({"PATH": "/tmp"}, "PATH"),                                                      # not a setting
])
def test_invalid_settings_are_rejected_and_nothing_is_written(gui, values, field):
    before = gui.env_file.read_text()

    async def s(client):
        csrf = await login(client)
        r = await client.post("/api/settings", json={"values": values}, headers={"X-CSRF-Token": csrf})
        body = await r.json()
        assert r.status == 400 and field in body["errors"], body
    run(gui, s)
    assert gui.env_file.read_text() == before and gui.preflights == []


def test_saving_updates_env_in_place_and_runs_preflight(gui):
    async def s(client):
        csrf = await login(client)
        r = await client.post("/api/settings", headers={"X-CSRF-Token": csrf}, json={"values": {
            "STRIPE_MODE": "test", "STRIPE_SECRET_KEY": "", "SMTP_PASSWORD": "",          # empty secrets: keep
            "SMTP_HOST": "smtp.fastmail.com", "SMTP_PORT": "587", "SMTP_USER": "sam@example.com",
            "TUNNEL_HOSTNAME": "hooks.example.com", "CAN_SPAM_POSTAL_ADDRESS": "1 Main St, Springfield, IL 62701, USA",
            "HASHNODE_TOKEN": "hn-token-with space", "DRY_RUN": "true",
        }})
        body = await r.json()
        assert r.status == 200 and body["ok"], body
        assert "SMTP_HOST" in body["saved"] and "PUBLIC_WEBHOOK_URL" in body["saved"] and "STRIPE_SECRET_KEY" not in body["saved"]
        assert [c["status"] for c in body["checks"]] == ["warn", "ok"] and body["restart_required"] is False
    run(gui, s)
    text = gui.env_file.read_text()
    assert text.startswith("# Stripe (keep this comment)\nSTRIPE_SECRET_KEY=sk_test_0123456789abcdefWXYZ\n")
    assert "export SMTP_PASSWORD='old pass'" in text and "UNRELATED=keep-me" in text
    assert "PUBLIC_WEBHOOK_URL=https://hooks.example.com/webhook" in text and "SMTP_USER=sam@example.com" in text
    assert "CAN_SPAM_POSTAL_ADDRESS='1 Main St, Springfield, IL 62701, USA'" in text
    assert stat.S_IMODE(gui.env_file.stat().st_mode) == 0o600
    cfg = gui.config  # reloaded from the file: the aliases reach the agent's config
    assert (cfg.smtp_host, cfg.smtp_port, cfg.smtp_username) == ("smtp.fastmail.com", 587, "sam@example.com")
    assert cfg.sender_postal_address.startswith("1 Main St") and cfg.hashnode_token == "hn-token-with space"
    assert cfg.public_webhook_url == "https://hooks.example.com/webhook" and cfg.smtp_password == "old pass"
    assert len(gui.preflights) == 1


def test_clearing_a_secret_and_override_warnings(gui):
    gui.env_file.write_text(gui.env_file.read_text() + "AUTOMONETIZE_SMTP_HOST=override.example.com\n")
    gui.controller.pid = 4242

    async def s(client):
        csrf = await login(client)
        r = await client.post("/api/settings", headers={"X-CSRF-Token": csrf},
                              json={"values": {"SMTP_HOST": "smtp.example.com"}, "clear": ["SMTP_PASSWORD"], "preflight": False})
        body = await r.json()
        assert body["ok"] and body["checks"] == [] and body["restart_required"] is True
        assert any("SMTP_HOST" in w and "override.example.com" in w for w in body["warnings"])
    run(gui, s)
    assert "SMTP_PASSWORD=\n" in gui.env_file.read_text() or "SMTP_PASSWORD=''" in gui.env_file.read_text()
    assert gui.config.smtp_password == ""


def test_live_mode_accepts_a_matching_live_key(gui):
    async def s(client):
        csrf = await login(client)
        r = await client.post("/api/settings", headers={"X-CSRF-Token": csrf}, json={
            "values": {"STRIPE_MODE": "live", "STRIPE_SECRET_KEY": "rk_live_0123456789abcdef"}, "preflight": False})
        assert r.status == 200
    run(gui, s)
    assert "STRIPE_SECRET_KEY=rk_live_0123456789abcdef" in gui.env_file.read_text()
    assert "STRIPE_MODE=live" in gui.env_file.read_text()


# ------------------------------------------------------------------ control, status, logs
def test_control_actions_are_dispatched(gui):
    async def s(client):
        csrf = await login(client)
        h = {"X-CSRF-Token": csrf}
        for action in ("start", "pause", "resume", "restart"):
            r = await client.post("/api/control", json={"action": action}, headers=h)
            assert r.status == 200 and (await r.json())["message"] == f"did {action}"
        assert (await client.post("/api/control", json={"action": "kill"}, headers=h)).status == 400  # needs confirm
        assert (await client.post("/api/control", json={"action": "kill", "confirm": True}, headers=h)).status == 200
        assert (await client.post("/api/control", json={"action": "rm -rf"}, headers=h)).status == 400
    run(gui, s)
    assert gui.controller.calls == ["start", "pause", "resume", "restart", "kill"]


def test_status_reports_everything_the_panel_shows(gui, state):
    gui.controller.pid = 999
    state.record_revenue("stripe", "o1", 900, 56, 844, True)
    state.upsert_subscriber("stripe", "sub_1", email="p@example.com", price_cents=1000)
    state.capture_free_subscriber("lead@example.com", "python-remote", "tok1234567890")

    async def s(client):
        await login(client)
        data = await (await client.get("/api/status")).json()
        assert data["supervisor"]["running"] and data["webhook"]["healthy"] and data["webhook"]["events"] == {"processed": 3}
        assert data["revenue"] == {"net_cents": 844, "target_cents": 1000, "gross_cents": data["revenue"]["gross_cents"],
                                   "orders": data["revenue"]["orders"]}
        assert data["mrr_cents"] == 1000 and data["subscribers"] == 1 and data["leads"] == {"pending": 1}
        assert data["engine"]["state"] == "running" and "flag" in data["engine"] and data["dry_run"] is True
    run(gui, s)


def test_logs_are_redacted(gui, state):
    state.log_action(1, None, "debug", "ok", "token=abc123secret and Bearer abcdefghijklmnop")

    async def s(client):
        await login(client)
        data = await (await client.get("/api/logs?lines=50")).json()
        text = str(data)
        assert "sk_live_ABCDEFGHIJKLMNOP" not in text and "whsec_abcdefghijk" not in text and "hunter2" not in text
        assert "abc123secret" not in text and "abcdefghijklmnop" not in text
        assert "boot" in text and "[redacted]" in text
    run(gui, s)
    assert redact("password=x y") == "password=[redacted] y"


# ------------------------------------------------------------------ the service controller
class Proc:
    def __init__(self, rc=0, out=""):
        self.returncode, self.stdout, self.stderr = rc, out, ""


@pytest.fixture
def controller(config, state, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("STRIPE_SECRET_KEY=sk_test_x\n")
    calls = {"run": [], "spawn": [], "kill": []}
    alive = set()

    def killer(pid, sig):
        calls["kill"].append((pid, sig))
        if pid not in alive:
            raise ProcessLookupError
        if sig == signal.SIGTERM:
            alive.discard(pid)

    c = ServiceController(config, state, tmp_path, env_file, runner=lambda cmd, **k: (calls["run"].append(cmd), Proc())[1],
                          spawner=lambda cmd, **k: calls["spawn"].append((cmd, k)), killer=killer, system="Linux",
                          home=tmp_path / "home", sleep=lambda s: None)
    c.calls, c.alive = calls, alive
    return c


def test_start_spawns_a_supervisor_with_the_env_file(controller, state, config):
    config.stop_file.parent.mkdir(parents=True, exist_ok=True)
    config.stop_file.write_text("killed")
    state.set("emergency_stop", {"reason": "kill switch"})
    state.set("paused", {"reason": "x"})
    res = controller.act("start")
    assert res["ok"] and not config.stop_file.exists() and state.get("emergency_stop") is None and state.get("paused") is None
    (cmd, kw), = controller.calls["spawn"]
    assert cmd[-3:] == ["dashboard.cli", "supervise", "--headless"] and kw["start_new_session"] is True
    assert kw["env"]["STRIPE_SECRET_KEY"] == "sk_test_x" and kw["cwd"] == str(controller.workdir)
    assert any(a["name"] == "gui:start" for a in state.recent_actions(5))
    # already running: flags cleared, nothing spawned
    controller.alive.add(4321)
    state.set("supervisor", {"pid": 4321})
    controller.act("start")
    assert len(controller.calls["spawn"]) == 1


def test_pause_resume_and_the_engine_honours_them(controller, state, config, toolkit):
    from agent.engine import Engine
    from agent.power import NullBackend, PowerManager

    controller.act("pause")
    assert engine_state(state, config) == ("paused", "paused from the control panel")
    eng = Engine(config, state=state, strategies=[], toolkit=toolkit, online_check=lambda: True, power=PowerManager(NullBackend()))
    assert eng.run_cycle().status == "paused"
    controller.act("resume")
    assert engine_state(state, config)[0] == "running" and eng.run_cycle().status in ("ran", "idle")


def test_kill_switch_stops_the_engine_and_the_process(controller, state, config):
    controller.alive.add(777)
    state.set("supervisor", {"pid": 777})
    res = controller.act("kill")
    assert res["ok"] and config.stop_file.exists() and state.get("emergency_stop")
    assert (777, signal.SIGTERM) in controller.calls["kill"]
    assert engine_state(state, config)[0] == "stopped"
    assert controller.calls["spawn"] == []


def test_launchd_mode_uses_launchctl(controller, state, config, tmp_path):
    controller.system = "Darwin"
    plist = tmp_path / "home" / "Library" / "LaunchAgents" / "com.automonetize.agent.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("<plist/>")
    controller.act("start")
    domain = f"gui/{os.getuid()}"
    assert ["launchctl", "bootstrap", domain, str(plist)] in controller.calls["run"]
    assert ["launchctl", "kickstart", f"{domain}/com.automonetize.agent"] in controller.calls["run"]
    controller.act("kill")
    assert ["launchctl", "bootout", f"{domain}/com.automonetize.agent"] in controller.calls["run"]
    controller.act("restart")
    assert ["launchctl", "kickstart", "-k", f"{domain}/com.automonetize.agent"] in controller.calls["run"]
    assert controller.calls["spawn"] == [] and controller.status()["mode"] == "launchd"


def test_restart_waits_for_exit_then_relaunches_keeping_flags(controller, state):
    controller.alive.add(55)
    state.set("supervisor", {"pid": 55})
    state.set("paused", {"reason": "operator"})
    res = controller.act("restart")
    assert res["ok"] and (55, signal.SIGTERM) in controller.calls["kill"] and len(controller.calls["spawn"]) == 1
    assert state.get("paused")  # a restart reloads settings; it doesn't undo a pause


def test_cli_gui_refuses_public_hosts(capsys):
    from dashboard import cli

    assert cli.main(["gui", "--host", "0.0.0.0", "--no-browser"], console=SimpleNamespace(print=print)) == 2
    assert "refusing" in capsys.readouterr().out


def test_status_reports_api_volume_subscribers_and_bandit(gui, state, config):
    from api.auth import ApiKeys
    from tools.copy_bandit import record_event, tune

    keys = ApiKeys(state, config)
    _, row = keys.issue(11, "dev@example.com")
    _, past_due = keys.issue(12, "late@example.com")
    keys.set_status(past_due["id"], "degraded")
    for _ in range(3):
        keys.consume(row, "signals", state.clock())
    keys.consume(row, "companies", state.clock())
    record_event(state, "cta", "free_sample", "view", state.clock(), 50)
    record_event(state, "cta", "free_sample", "signup", state.clock(), 5)
    record_event(state, "cta", "instant_feed", "view", state.clock(), 50)  # the control, converting worse
    record_event(state, "cta", "instant_feed", "signup", state.clock(), 1)
    tune(state, config, state.clock())

    async def s(client):
        await login(client)
        g = (await (await client.get("/api/status")).json())["growth"]
        assert g["api"]["requests_today"] == 4 and g["api"]["by_endpoint"] == {"signals": 3, "companies": 1}
        assert (g["api"]["subscribers"], g["api"]["active_keys"], g["api"]["degraded_keys"]) == (2, 1, 1)
        free = next(r for r in g["copy"]["arms"] if r["variant"] == "free_sample")
        assert (free["views"], free["signups"], free["conversion_rate"]) == (50, 5, 0.1)
        assert g["copy"]["winners"]["cta"] == "free_sample" and free["allocation"] == 0.8
        assert set(g["sources"]) == {"candidate", "trial", "active", "suspended", "rejected"}
    run(gui, s)
    index = (Path(__file__).resolve().parents[1] / "gui" / "static" / "index.html").read_text()
    assert 'id="k-api"' in index and 'id="copy-arms"' in index and 'id="k-api-subs"' in index


def test_dashboard_renders_the_growth_panel(config, state):
    from rich.console import Console

    from api.auth import ApiKeys
    from dashboard.render import render_dashboard
    from dashboard.snapshot import collect_snapshot

    keys = ApiKeys(state, config)
    _, row = keys.issue(1, "d@example.com")
    keys.consume(row, "signals", state.clock())
    state.set("niche_allocation", {"shares": {"python-remote": 0.7, "devops-sre": 0.3}, "revenue_cents": {"python-remote": 2900}})
    console = Console(record=True, width=160)
    console.print(render_dashboard(collect_snapshot(state, config)))
    out = console.export_text()
    assert "Developer API & Growth Engine" in out and "1 requests today" in out and "1 subscriber" in out
    assert "instant_feed (control)" in out and "python-remote 70% ($29.00)" in out and "Discovered sources" in out
