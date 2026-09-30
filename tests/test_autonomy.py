"""Set-and-forget operation: self-healing recovery, power assertions, tunnel config, setup-autonomous."""

from __future__ import annotations

import ctypes
import json
import os
import plistlib
import random
import sqlite3
import stat
import subprocess
import threading
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import tunnel
from agent.engine import Engine
from agent.power import (
    ASSERTION_TYPE, CaffeinateBackend, IOKitBackend, NullBackend, PowerManager, select_backend,
)
from agent.recovery import (
    Backoff, PlatformBackoff, Quarantine, is_locked, is_transient, retry_sqlite, self_diagnostic,
)
from agent.setup_autonomous import (
    SetupOptions, handshake, install_services, register_webhook_endpoint, run_setup, stripe_mode, validate_env,
    webhook_events,
)
from agent.state import StateStore
from strategies.base import Strategy, TaskResult
from tests.conftest import NOW, FakeTransport
from tools import build_toolkit
from tools.errors import HttpError, OperationalFailure
from tools.http_client import Response
from tools.storefront.webhook_listener import HANDSHAKE_TYPE, WebhookProcessor, sign_payload
from tools.syndication import Syndicator, build_article

ROOT = Path(__file__).resolve().parents[1]
TUNNEL_ID = "6ff42ae2-765d-4adf-8112-31c55c1551ef"


# =============================================================================== recovery
def test_backoff_grows_exponentially_with_jitter_and_caps():
    b = Backoff(base_seconds=60, cap_seconds=3600)
    rng = random.Random(1)
    for n, ceiling in ((1, 60), (2, 120), (3, 240), (10, 3600)):
        d = b.delay(n, rng)
        assert ceiling / 2 <= d <= ceiling


@pytest.mark.parametrize("exc,expected", [
    (HttpError(503, "u"), True), (HttpError(429, "u"), True), (HttpError(502, "u"), True),
    (HttpError(401, "u"), False), (HttpError(404, "u"), False),
    (ConnectionResetError(), True), (TimeoutError(), True),
    (OperationalFailure("x", 3, HttpError(500, "u")), True), (OperationalFailure("x", 3, HttpError(403, "u")), False),
    (ValueError("bug"), False),
])
def test_is_transient(exc, expected):
    assert is_transient(exc) is expected


def test_platform_backoff_is_persisted_honours_retry_after_and_clears(state, clock):
    pb = PlatformBackoff(state, Backoff(60, 3600), rng=random.Random(0))
    until = pb.failure("devto", HttpError(503, "u"))
    assert 30 <= (until - clock()).total_seconds() <= 60
    assert PlatformBackoff(state).blocked_until("devto") == until  # survives a restart (new object)
    second = pb.failure("devto", HttpError(503, "u"))
    assert (second - clock()).total_seconds() >= 60  # grows
    ra = pb.failure("hashnode", HttpError(429, "u", retry_after=7200))
    assert (ra - clock()).total_seconds() >= 7200
    clock.advance(hours=3)
    assert pb.blocked_until("devto") is None
    pb.success("devto")
    assert state.get("backoff:devto") is None


def test_permanent_errors_back_off_long(state, clock):
    pb = PlatformBackoff(state, Backoff(60, 24 * 3600), rng=random.Random(0))
    until = pb.failure("devto", HttpError(401, "u"))
    assert (until - clock()).total_seconds() >= 60 * 2 ** 6 / 2  # starts at the long end


def test_retry_sqlite_retries_locked_with_jitter_up_to_five_times():
    sleeps, calls = [], {"n": 0}

    def locked():
        calls["n"] += 1
        if calls["n"] < 4:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    assert retry_sqlite(locked, sleep=sleeps.append, rng=random.Random(3)) == "ok"
    assert calls["n"] == 4 and len(sleeps) == 3
    assert 0.025 <= sleeps[0] <= 0.05 and 0.05 <= sleeps[1] <= 0.1 and 0.1 <= sleeps[2] <= 0.2
    assert len(set(sleeps)) == 3

    calls["n"] = -100  # always locked
    sleeps.clear()
    with pytest.raises(sqlite3.OperationalError):
        retry_sqlite(locked, sleep=sleeps.append)
    assert len(sleeps) == 4  # 5 attempts, 4 waits

    def other():
        raise sqlite3.OperationalError("no such table: x")

    sleeps.clear()
    with pytest.raises(sqlite3.OperationalError):
        retry_sqlite(other, sleep=sleeps.append)
    assert sleeps == []  # not a lock: no retry
    assert is_locked(sqlite3.OperationalError("database is locked")) and not is_locked(ValueError("database is locked"))


def test_state_store_survives_a_real_lock_held_by_another_process(tmp_path):
    db = tmp_path / "s.db"
    sleeps = []
    store = StateStore(db, busy_timeout=0.01, lock_retry_sleep=sleeps.append)
    other = sqlite3.connect(db, isolation_level=None, timeout=0)
    other.execute("BEGIN IMMEDIATE")  # another writer (a CLI command) holds the lock

    released = {"done": False}

    def release_after_two(delay):
        if len(sleeps) == 2 and not released["done"]:
            other.execute("COMMIT")
            released["done"] = True

    store._retry_kwargs = {"sleep": lambda d: (sleeps.append(d), release_after_two(d))}
    store.set("k", 1)
    assert store.get("k") == 1 and released["done"] and len(sleeps) == 2
    other.close()
    store.close()


# ------------------------------------------------------------------ quarantine in the engine
class Failing(Strategy):
    name = "failing"
    tasks = ("aggregate_leads", "package_asset", "stage_outreach")

    def __init__(self):
        self.broken = True
        self.calls = 0

    def run(self, task, ctx):
        self.calls += 1
        if self.broken:
            raise RuntimeError("systemic")
        return TaskResult(True, "ok")


@pytest.fixture
def quarantine_engine(config, state, toolkit):
    power = PowerManager(NullBackend())
    online = {"up": True}
    strat = Failing()
    eng = Engine(config, state=state, strategies=[strat], toolkit=toolkit, sleep=lambda s: None,
                 online_check=lambda: online["up"], power=power)
    return eng, strat, online, power


def test_breaker_trip_quarantines_then_self_heals(quarantine_engine, config, state, clock):
    eng, strat, _, _ = quarantine_engine
    report = eng.run_cycle()
    assert "quarantined" in report.message and eng.breaker.tripped
    assert not config.stop_file.exists() and state.get("emergency_stop") is None  # not a manual stop
    q = state.get("quarantine")
    assert q["active"] and q["count"] == 1
    assert (clock() + timedelta(hours=2)).isoformat(timespec="seconds") == q["until"]
    assert any(e["kind"] == "alert" for e in state.recent_errors(10))
    assert "quarantine" in (config.data_dir / "ALERTS.log").read_text()

    calls = strat.calls
    clock.advance(hours=1)
    assert eng.run_cycle().status == "quarantined" and strat.calls == calls  # cooling down

    strat.broken = False  # whatever it was has passed
    clock.advance(hours=1, seconds=1)
    report = eng.run_cycle()
    assert report.status == "ran" and all(a["status"] == "ok" for a in report.actions)  # clean cycle
    assert not eng.breaker.tripped and eng.breaker.consecutive_errors == 0
    rec = state.get("quarantine")
    assert not rec["active"] and rec["last_diagnostic"]["passed"]
    assert state.get("breaker")["tripped"] is False


def test_repeat_trips_escalate_and_failed_diagnostic_extends(quarantine_engine, config, state, clock):
    eng, strat, online, _ = quarantine_engine
    eng.run_cycle()
    clock.advance(hours=2, seconds=1)
    for _ in range(3):  # released; the still-broken tasks trip it again once they're re-planned
        eng.run_cycle()
        if state.get("quarantine")["active"]:
            break
    q = state.get("quarantine")
    assert q["active"] and q["count"] == 2
    assert q["until"] == (clock() + timedelta(hours=4)).isoformat(timespec="seconds")

    # due, but offline: waits for the network without adding hours
    clock.advance(hours=4, seconds=1)
    online["up"] = False
    assert eng.run_cycle().status == "quarantined" and state.get("quarantine")["until"] == q["until"]

    # due, online, but the database fails its check: extended by quarantine_hours
    online["up"] = True
    real = eng.state.conn

    class Broken:
        def execute(self, sql, *a):
            if "quick_check" in sql:
                return SimpleNamespace(fetchone=lambda: ("corrupt page 7",))
            return real.execute(sql, *a)

    eng.state.conn = Broken()
    try:
        report = eng.run_cycle()
    finally:
        eng.state.conn = real
    assert report.status == "quarantined" and "db_integrity" in report.message
    assert state.get("quarantine")["until"] == (clock() + timedelta(hours=2)).isoformat(timespec="seconds")


def test_quarantine_is_capped(config, state, clock):
    config.quarantine_hours, config.quarantine_max_hours = 2, 6
    q = Quarantine(state, config)
    for _ in range(5):
        until = q.enter("x")
        q.release({})
    assert until - clock() == timedelta(hours=6)


def test_manual_stop_is_never_lifted_automatically(quarantine_engine, config, state, clock):
    eng, strat, _, _ = quarantine_engine
    eng.run_cycle()
    eng.emergency_stop("operator")
    strat.broken = False
    clock.advance(hours=3)
    assert eng.run_cycle().status == "stopped"
    assert state.get("quarantine")["active"]  # diagnostics don't even run while a human holds it
    eng.resume()
    assert not state.get("quarantine")["active"] and eng.run_cycle().status == "ran"


def test_self_diagnostic_checks(config, state):
    config.ensure_dirs()
    d = self_diagnostic(state, config, lambda: True)
    assert d["passed"] and d["db_integrity"] is True and d["db_write"] is True and d["disk_free_mb"] > 0
    assert not self_diagnostic(state, config, lambda: False)["passed"]


# ------------------------------------------------------------------ syndication backoff
class Flaky:
    def __init__(self, name, errors):
        self.name, self.errors, self.posts = name, list(errors), 0

    def configured(self):
        return True

    def last_published_at(self):
        return None

    def publish(self, article, live):
        if self.errors:
            raise self.errors.pop(0)
        self.posts += 1
        return f"https://{self.name}.example/{self.posts}"


def _article(clock, guid="g1"):
    a = build_article("python-remote", "Python Remote",
                      [{"company": f"C{i}", "stack": ["python"], "urgency_score": 50, "intent_signals": []} for i in range(12)],
                      clock(), lander_url="https://x.example/python-remote/")
    a.guid = guid
    return a


def test_syndication_5xx_backs_off_one_platform_and_retries_later(config, state, toolkit, clock):
    devto = Flaky("devto", [OperationalFailure("down", 3, HttpError(503, "u"))])
    hashnode = Flaky("hashnode", [])
    syn = Syndicator(config, state, toolkit.files, [devto, hashnode])
    res = syn.syndicate(_article(clock), clock())
    assert res["devto"].startswith("failed") and res["hashnode"].startswith("https://")  # the other one carries on
    assert state.get("backoff:devto")["transient"] is True

    clock.advance(minutes=1)
    assert syn.syndicate(_article(clock), clock())["devto"].startswith("backing off until")
    clock.advance(hours=1)
    assert syn.syndicate(_article(clock), clock())["devto"].startswith("https://")
    assert state.get("backoff:devto") is None


def test_hn_tracker_transient_failure_reschedules_without_failing_the_task(toolkit, transport, make_hypothesis, state, clock):
    from strategies.base import TaskContext
    from strategies.inbound_syndicator import InboundSyndicator

    transport.add("https://hn.algolia.com/api/v1/search_by_date", Response(503, "u", b"busy"))
    ctx = TaskContext(toolkit, make_hypothesis(), {}, attempt=1)
    res = InboundSyndicator().run("track_hn", ctx)
    assert res.ok and "deferred" in res.summary and state.get("backoff:hn_algolia")
    calls = len(transport.calls)
    res = InboundSyndicator().run("track_hn", ctx)
    assert "backing off" in res.summary and len(transport.calls) == calls  # no request while cooling down


# =============================================================================== power
class FakeBackend:
    name = "fake"

    def __init__(self):
        self.events = []

    def acquire(self, reason):
        self.events.append(("acquire", reason))

    def release(self):
        self.events.append(("release",))


def test_power_holds_are_reference_counted_across_threads():
    be = FakeBackend()
    pm = PowerManager(be)
    with pm.hold("engine cycle"):
        with pm.hold("webhook"):
            assert pm.active == 2
        assert pm.active == 1 and be.events == [("acquire", "engine cycle")]
    assert be.events == [("acquire", "engine cycle"), ("release",)] and pm.active == 0

    barrier = threading.Barrier(4)

    def worker():
        with pm.hold("webhook"):
            barrier.wait(5)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert be.events.count(("acquire", "webhook")) == 1 and pm.active == 0


def test_power_failure_never_breaks_the_work():
    class Broken(FakeBackend):
        def acquire(self, reason):
            raise OSError("IOKit said no")

    pm = PowerManager(Broken())
    with pm.hold("x"):
        pass
    assert "IOKit said no" in pm.last_error


def test_engine_cycle_and_webhook_hold_power(config, state, toolkit):
    be = FakeBackend()
    pm = PowerManager(be)
    seen = []

    class Probe(Strategy):
        name = "probe"
        tasks = ("aggregate_leads",)

        def run(self, task, ctx):
            seen.append(pm.reasons())
            return TaskResult(True, "ok")

    Engine(config, state=state, strategies=[Probe()], toolkit=toolkit, online_check=lambda: True, power=pm).run_cycle()
    assert seen == [["engine cycle"]] and pm.active == 0 and be.events[-1] == ("release",)

    import asyncio

    from aiohttp.test_utils import TestClient, TestServer

    from tools.storefront.webhook_listener import build_app

    config.stripe_webhook_secret = "whsec_test"
    held = []
    proc = WebhookProcessor(toolkit, use_sdk=False)
    orig = proc.handle
    proc.handle = lambda *a: (held.append(pm.reasons()), orig(*a))[1]

    async def go():
        async with TestClient(TestServer(build_app(proc, power=pm))) as client:
            body = json.dumps({"id": "evt_1", "type": HANDSHAKE_TYPE, "data": {"object": {"nonce": "n1"}}}).encode()
            r = await client.post("/webhook", data=body, headers={"Stripe-Signature": sign_payload(body, "whsec_test")})
            return r.status, await r.json()

    status, reply = asyncio.run(go())
    assert status == 200 and reply["status"] == "processed"
    assert held == [["webhook"]] and pm.active == 0


def test_backend_selection_is_a_noop_off_macos():
    assert isinstance(select_backend(True, system="Linux"), NullBackend)
    assert isinstance(select_backend(False, system="Darwin"), NullBackend)

    def no_frameworks(path):
        raise OSError("not here")

    be = select_backend(True, system="Darwin", loader=no_frameworks, which=lambda n: "/usr/bin/caffeinate")
    assert isinstance(be, CaffeinateBackend)
    assert isinstance(select_backend(True, system="Darwin", loader=no_frameworks, which=lambda n: None), NullBackend)


class FakeFn:
    def __init__(self, impl):
        self.impl, self.calls = impl, []

    def __call__(self, *args):
        self.calls.append(args)
        return self.impl(*args)


def test_iokit_backend_calls_the_assertion_api():
    strings = {}

    def create(alloc, text, enc):
        assert enc == 0x08000100
        ref = 1000 + len(strings)
        strings[ref] = text.decode()
        return ref

    def create_assertion(kind, level, name, out):
        assert strings[kind] == ASSERTION_TYPE and level == 255 and strings[name].startswith("AutoMonetize")
        ctypes.cast(out, ctypes.POINTER(ctypes.c_uint32)).contents.value = 77
        return 0

    iokit = SimpleNamespace(IOPMAssertionCreateWithName=FakeFn(create_assertion), IOPMAssertionRelease=FakeFn(lambda i: 0))
    cf = SimpleNamespace(CFStringCreateWithCString=FakeFn(create), CFRelease=FakeFn(lambda r: None))
    be = IOKitBackend(lambda path: iokit if "IOKit" in path else cf)
    be.acquire("engine cycle")
    be.acquire("again")  # already held: no second assertion
    assert len(iokit.IOPMAssertionCreateWithName.calls) == 1 and len(cf.CFRelease.calls) == 2
    be.release()
    assert iokit.IOPMAssertionRelease.calls == [(77,)]
    be.release()
    assert len(iokit.IOPMAssertionRelease.calls) == 1

    iokit.IOPMAssertionCreateWithName = FakeFn(lambda *a: 0xE00002C2)
    with pytest.raises(OSError):
        be.acquire("fails")


def test_caffeinate_backend_ties_itself_to_our_pid():
    procs = []

    class P:
        def __init__(self, cmd, **kw):
            self.cmd, self.alive = cmd, True
            procs.append(self)

        def poll(self):
            return None if self.alive else 0

        def terminate(self):
            self.alive = False

        def wait(self, timeout=None):
            return 0

    be = CaffeinateBackend("/usr/bin/caffeinate", popen=P)
    be.acquire("x")
    be.acquire("y")
    assert len(procs) == 1 and procs[0].cmd == ["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())]
    be.release()
    assert not procs[0].alive


def test_schedule_wake_uses_non_interactive_sudo_and_stops_when_denied():
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    pm = PowerManager(NullBackend(), schedule_wake=True, runner=runner, system="Darwin")
    assert pm.schedule_wake(NOW + timedelta(hours=1), now=NOW)
    assert calls[0][:5] == ["sudo", "-n", "/usr/bin/pmset", "schedule", "wake"]
    assert pm.schedule_wake(NOW + timedelta(hours=1), now=NOW) and len(calls) == 1  # same slot: not repeated
    assert not pm.schedule_wake(NOW + timedelta(seconds=60), now=NOW)  # too soon to bother

    denied = PowerManager(NullBackend(), schedule_wake=True, system="Darwin",
                          runner=lambda cmd, **kw: SimpleNamespace(returncode=1, stdout="", stderr="a password is required"))
    assert not denied.schedule_wake(NOW + timedelta(hours=1), now=NOW) and denied.wake_denied
    assert not PowerManager(NullBackend(), schedule_wake=True, system="Linux", runner=runner).schedule_wake(NOW + timedelta(hours=2), now=NOW)


def test_idle_wait_follows_the_wall_clock_across_sleep(config, state, toolkit):
    """A Mac asleep for 3 hours: the monotonic clock stood still, the wall clock didn't."""
    wall = {"t": 1_000_000.0}
    eng = Engine(config, state=state, strategies=[], toolkit=toolkit, online_check=lambda: True,
                 power=PowerManager(NullBackend()), wall_clock=lambda: wall["t"])
    waits = []

    def fake_wait(timeout):
        waits.append(timeout)
        wall["t"] += 3 * 3600 if len(waits) == 1 else timeout  # the machine sleeps during the first slice
        return False

    eng._stop_event.wait = fake_wait
    eng.idle_wait(3600, slice_seconds=30)
    assert waits == [30]  # woke up overdue: returns at once instead of waiting another hour


# =============================================================================== tunnel
def test_render_and_parse_config_round_trip_with_restricted_ingress():
    text = tunnel.render_config(TUNNEL_ID, "/Users/sam/.cloudflared/x.json", "Hooks.Example.com", "http://127.0.0.1:8443")
    cfg = tunnel.parse_config(text)
    assert cfg["tunnel"] == TUNNEL_ID and cfg["credentials-file"] == "/Users/sam/.cloudflared/x.json"
    s = tunnel.summarize_config(cfg)
    assert s["hostnames"] == ["hooks.example.com"]
    assert s["paths"] == ["^/webhook$", "^/healthz$", "^/lead\\-magnet/capture$", "^/lead\\-magnet/confirm$",
                          "^/lead\\-magnet/unsubscribe$"]
    assert s["services"] == ["http://127.0.0.1:8443"] and s["catch_all_404"]


def test_parse_config_handles_quotes_comments_and_rejects_nesting():
    cfg = tunnel.parse_config(
        "# comment\ntunnel: abc\ncredentials-file: \"/Users/Sam Dev/.cloudflared/abc.json\"\n"
        "ingress:\n  - hostname: a.example.com   # inline comment\n    service: http://localhost:8443\n"
        "  - service: http_status:404\n")
    assert cfg["credentials-file"] == "/Users/Sam Dev/.cloudflared/abc.json"
    assert cfg["ingress"][0] == {"hostname": "a.example.com", "service": "http://localhost:8443"}
    assert tunnel.summarize_config(cfg)["catch_all_404"]
    open_cfg = tunnel.parse_config("tunnel: x\ningress:\n  - hostname: a.example.com\n    service: http://localhost:1\n")
    assert not tunnel.summarize_config(open_cfg)["catch_all_404"]
    with pytest.raises(tunnel.TunnelConfigError):
        tunnel.parse_config("ingress:\n  - hostname: a\n    originRequest:\n      noTLSVerify: true\n")
    # a path with spaces survives the round trip
    rendered = tunnel.render_config(TUNNEL_ID, "/Users/Sam Dev/.cloudflared/x.json", "a.example.com")
    assert tunnel.parse_config(rendered)["credentials-file"] == "/Users/Sam Dev/.cloudflared/x.json"


@pytest.mark.parametrize("bad", ["", "localhost", "https://hooks.example.com", "hooks.example.com/webhook",
                                 "-bad.example.com", "10.0.0.1", "abc.trycloudflare.com", "a..example.com"])
def test_hostname_validation_rejects(bad):
    with pytest.raises(tunnel.TunnelConfigError):
        tunnel.validate_hostname(bad)


def test_hostname_and_url():
    assert tunnel.validate_hostname("Hooks.Example.COM.") == "hooks.example.com"
    assert tunnel.public_webhook_url("hooks.example.com") == "https://hooks.example.com/webhook"
    with pytest.raises(tunnel.TunnelConfigError):
        tunnel.render_config("not-a-uuid", "/x.json", "hooks.example.com")


def test_parse_cloudflared_create_and_list_output():
    out = (f"Tunnel credentials written to /Users/sam/.cloudflared/{TUNNEL_ID}.json. cloudflared chose this file "
           "based on where your origin certificate was found. Keep this file secret.\n\n"
           f"Created tunnel automonetize with id {TUNNEL_ID}\n")
    assert tunnel.parse_create_output(out) == (TUNNEL_ID, f"/Users/sam/.cloudflared/{TUNNEL_ID}.json")
    with pytest.raises(tunnel.TunnelConfigError):
        tunnel.parse_create_output("error: tunnel already exists")
    listing = json.dumps([
        {"id": "11111111-1111-1111-1111-111111111111", "name": "automonetize", "deleted_at": "2026-01-01T00:00:00Z"},
        {"id": TUNNEL_ID, "name": "automonetize", "deleted_at": "0001-01-01T00:00:00Z"},
        {"id": "22222222-2222-2222-2222-222222222222", "name": "other"},
    ])
    assert tunnel.find_tunnel_id(listing, "automonetize") == TUNNEL_ID
    assert tunnel.find_tunnel_id(listing, "missing") is None
    assert tunnel.find_tunnel_id("", "automonetize") is None


def test_env_file_update_preserves_content_and_is_private(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# secrets\nSTRIPE_SECRET_KEY=sk_test_abc\nexport PUBLIC_WEBHOOK_URL=http://old\nDRY_RUN=true\n")
    tunnel.update_env_file(env, {"PUBLIC_WEBHOOK_URL": "https://hooks.example.com/webhook", "NEW_KEY": "has space $x"})
    text = env.read_text()
    assert text.startswith("# secrets\nSTRIPE_SECRET_KEY=sk_test_abc\nexport PUBLIC_WEBHOOK_URL=https://hooks.example.com/webhook\n")
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    parsed = tunnel.read_env_file(env)
    assert parsed == {"STRIPE_SECRET_KEY": "sk_test_abc", "PUBLIC_WEBHOOK_URL": "https://hooks.example.com/webhook",
                      "DRY_RUN": "true", "NEW_KEY": "has space $x"}
    # bash reads it the same way launchd's run_agent.sh will
    out = subprocess.run(["bash", "-c", f"set -a; source {env}; printf %s \"$NEW_KEY\""], capture_output=True, text=True)
    assert out.stdout == "has space $x"


def test_tunnel_cli_used_by_setup_script(tmp_path, capsys):
    cfg = tmp_path / "a.yml"
    assert tunnel.main(["render-config", "--tunnel-id", TUNNEL_ID, "--credentials-file", "/c.json",
                        "--hostname", "hooks.example.com", "--out", str(cfg)]) == 0
    assert tunnel.main(["check-config", str(cfg)]) == 0
    env, record = tmp_path / ".env", tmp_path / "data" / "tunnel.json"
    assert tunnel.main(["write-env", "--env-file", str(env), "--hostname", "hooks.example.com", "--record", str(record),
                        "--tunnel-id", TUNNEL_ID]) == 0
    assert "PUBLIC_WEBHOOK_URL=https://hooks.example.com/webhook" in env.read_text()
    assert json.loads(record.read_text())["tunnel_id"] == TUNNEL_ID
    assert tunnel.main(["validate-hostname", "nope"]) == 2


def test_config_reads_public_url_from_plain_env():
    from agent.config import Config

    cfg = Config.load(env={"PUBLIC_WEBHOOK_URL": "https://hooks.example.com/webhook"})
    assert cfg.public_webhook_url == "https://hooks.example.com/webhook" and cfg.quarantine_hours == 2


# =============================================================================== launchd
def test_install_script_renders_agent_and_tunnel_jobs(tmp_path):
    env = {**os.environ, "HOME": "/Users/sam", "TUNNEL_NAME": "automonetize",
           "TUNNEL_CONFIG": "/Users/sam/.cloudflared/automonetize.yml", "CLOUDFLARED": "/opt/homebrew/bin/cloudflared"}
    subprocess.run(["bash", str(ROOT / "deploy" / "install_launchd.sh"), "--render-only", str(tmp_path)],
                   check=True, env=env, capture_output=True)
    agent = plistlib.loads((tmp_path / "com.automonetize.agent.plist").read_bytes())
    tun = plistlib.loads((tmp_path / "com.automonetize.tunnel.plist").read_bytes())
    for job in (agent, tun):
        assert job["RunAtLoad"] is True and job["KeepAlive"] is True
        assert job["StandardOutPath"].startswith("/Users/sam/Library/Logs/automonetize/")
        assert job["EnvironmentVariables"]["HOME"] == "/Users/sam"
    assert agent["WorkingDirectory"] == str(ROOT)
    assert tun["ProgramArguments"] == ["/opt/homebrew/bin/cloudflared", "tunnel", "--config",
                                       "/Users/sam/.cloudflared/automonetize.yml", "run", "automonetize"]

    daemon = tmp_path / "daemon"
    subprocess.run(["bash", str(ROOT / "deploy" / "install_launchd.sh"), "--system", "--render-only", str(daemon)],
                   check=True, env={**env, "SUDO_USER": ""}, capture_output=True)
    assert plistlib.loads((daemon / "com.automonetize.agent.plist").read_bytes())["UserName"]


def test_setup_tunnel_script_is_executable_and_valid():
    script = ROOT / "deploy" / "tunnel" / "setup_tunnel.sh"
    assert script.stat().st_mode & stat.S_IXUSR
    subprocess.run(["bash", "-n", str(script)], check=True)
    text = script.read_text()
    assert "brew install cloudflared" in text and "tunnel login" in text and "install_launchd.sh" in text
    res = subprocess.run(["bash", str(script), "https://not-a-host"], capture_output=True, text=True, cwd=ROOT)
    assert res.returncode != 0 and "bare hostname" in res.stderr


# =============================================================================== setup-autonomous
STRIPE = "https://api.stripe.com/v1"
PUBLIC = "https://hooks.example.com/webhook"


@pytest.fixture
def ready(config, state, breaker, tmp_path):
    env = tmp_path / ".env"
    env.write_text("STRIPE_SECRET_KEY=sk_live_0123456789abcdef\n")
    env.chmod(0o600)
    config.stripe_secret_key = "sk_live_0123456789abcdef"
    config.public_webhook_url = PUBLIC
    config.smtp_host, config.smtp_username, config.smtp_password = "smtp.example.com", "u", "p"
    config.dry_run = False
    t = FakeTransport()
    kit = build_toolkit(config, state, breaker, transport=t, sleep=lambda s: None)
    return kit, t, env


def test_validate_env_flags_problems(config, tmp_path):
    env = tmp_path / ".env"
    env.write_text("X=1\n")
    env.chmod(0o644)
    config.stripe_secret_key = "sk_test_0123456789abcdef"
    config.stripe_webhook_secret = "nope"
    config.public_webhook_url = "http://hooks.example.com/webhook"
    by = {c.name: c for c in validate_env(config, env)}
    assert by[".env permissions"].status == "warn"
    assert by["STRIPE_SECRET_KEY"].status == "warn" and "TEST" in by["STRIPE_SECRET_KEY"].detail
    assert by["STRIPE_WEBHOOK_SECRET"].status == "fail"
    assert by["PUBLIC_WEBHOOK_URL"].status == "fail"
    assert [c.status for n, c in by.items() if n.startswith("mailer")] == ["fail"]
    assert {c.name: c for c in validate_env(config, env, require_live=True)}["STRIPE_SECRET_KEY"].status == "fail"
    assert stripe_mode("sk_live_0123456789") == "live" and stripe_mode("pk_live_0123456789") is None


def test_register_creates_endpoint_and_saves_secret(ready, config):
    kit, t, env = ready
    t.add_json(f"{STRIPE}/webhook_endpoints?", {"data": []})
    t.add_json(f"{STRIPE}/webhook_endpoints", {"id": "we_1", "secret": "whsec_new_secret"})
    check = register_webhook_endpoint(config, kit.http, env)
    assert check.status == "ok" and "whsec_" not in check.detail  # never printed
    assert config.stripe_webhook_secret == "whsec_new_secret"
    assert tunnel.read_env_file(env)["STRIPE_WEBHOOK_SECRET"] == "whsec_new_secret"
    body = t.calls_to(f"{STRIPE}/webhook_endpoints", "POST")[0]["body"].decode()
    for event in webhook_events():
        assert f"={event}" in body.replace("%2E", ".")
    assert HANDSHAKE_TYPE not in body and "metadata%5Bautomonetize%5D=1" in body


def test_register_reuses_existing_endpoint(ready, config):
    kit, t, env = ready
    config.stripe_webhook_secret = "whsec_have"
    t.add_json(f"{STRIPE}/webhook_endpoints?", {"data": [{"id": "we_9", "url": PUBLIC, "enabled_events": ["*"]}]})
    assert register_webhook_endpoint(config, kit.http, env).status == "ok"
    assert not t.calls_to(f"{STRIPE}/webhook_endpoints", "POST")
    # someone else's endpoint for the URL without a secret on file: don't touch it
    config.stripe_webhook_secret = ""
    t.add_json(f"{STRIPE}/webhook_endpoints?", {"data": [{"id": "we_9", "url": PUBLIC, "enabled_events": ["*"]}]})
    assert register_webhook_endpoint(config, kit.http, env).status == "fail"
    assert not t.calls_to(f"{STRIPE}/webhook_endpoints/we_9", "DELETE")


class TunnelTransport(FakeTransport):
    """Delivers POSTs to the public URL into a real WebhookProcessor, as Cloudflare + cloudflared would."""

    def __init__(self, processor, down_first=0):
        super().__init__()
        self.processor, self.down = processor, down_first

    def __call__(self, method, url, headers, body, timeout):
        if url == PUBLIC:
            self.calls.append({"method": method, "url": url, "headers": dict(headers), "timeout": timeout, "body": body})
            if self.down > 0:
                self.down -= 1
                return Response(502, url, b"bad gateway")
            out = self.processor.handle(body, headers.get("Stripe-Signature"))
            return Response(out.status, url, json.dumps(out.body).encode())
        return super().__call__(method, url, headers, body, timeout)


def test_handshake_goes_through_the_public_url_and_is_recorded(config, state, breaker):
    config.stripe_webhook_secret, config.public_webhook_url = "whsec_x", PUBLIC
    holder = {}
    t = TunnelTransport(None, down_first=2)  # the tunnel is still connecting for the first two tries
    kit = build_toolkit(config, state, breaker, transport=t, sleep=lambda s: None)
    t.processor = holder["p"] = WebhookProcessor(kit, use_sdk=False)
    clock = {"t": 1_800_000_000.0}
    sleeps = []
    check = handshake(config, state, kit.http, now=lambda: clock["t"], sleep=lambda s: (sleeps.append(s), clock.__setitem__("t", clock["t"] + s)),
                      nonce="abc123")
    assert check.status == "ok" and len(sleeps) == 2
    assert state.get("handshake:abc123")["via"] == "public_url"


def test_handshake_reports_secret_mismatch(config, state, breaker):
    config.stripe_webhook_secret, config.public_webhook_url = "whsec_x", PUBLIC
    t = TunnelTransport(None)
    kit = build_toolkit(config, state, breaker, transport=t, sleep=lambda s: None)
    t.processor = WebhookProcessor(kit, secret="whsec_other", use_sdk=False)
    check = handshake(config, state, kit.http, sleep=lambda s: None)
    assert check.status == "fail" and "different STRIPE_WEBHOOK_SECRET" in check.detail


def test_install_services_off_macos_renders_only(config, tmp_path):
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="rendered x", stderr="")

    c = install_services(ROOT, config.data_dir, system="Linux", runner=runner)
    assert c.status == "skip" and "--render-only" in calls[0]
    c = install_services(ROOT, config.data_dir, system="Darwin",
                         runner=lambda cmd, **kw: SimpleNamespace(returncode=0, stdout="installed /x/agent.plist\n", stderr=""))
    assert c.status == "ok" and "installed" in c.detail


def test_run_setup_end_to_end(config, state, breaker, tmp_path):
    env = tmp_path / ".env"
    env.write_text("STRIPE_SECRET_KEY=sk_live_0123456789abcdef\n")
    env.chmod(0o600)
    config.stripe_secret_key, config.public_webhook_url = "sk_live_0123456789abcdef", PUBLIC
    config.email_backend, config.sendgrid_api_key, config.dry_run = "sendgrid", "SG.key", False
    config.schedule_wake = False
    t = TunnelTransport(None)
    kit = build_toolkit(config, state, breaker, transport=t, sleep=lambda s: None)
    t.processor = WebhookProcessor(kit, use_sdk=False)  # reads config.stripe_webhook_secret at construction...
    t.add_json(f"{STRIPE}/balance", {"livemode": True})
    t.add_json("https://api.sendgrid.com/v3/scopes", {"scopes": ["mail.send"]})
    t.add_json(f"{STRIPE}/webhook_endpoints?", {"data": []})
    t.add_json(f"{STRIPE}/webhook_endpoints", {"id": "we_1", "secret": "whsec_fresh"})
    t.add_json("http://127.0.0.1:8443/healthz", {"ok": True})

    def runner(cmd, **kw):
        # launchd restarts the agent, which picks up the new secret from .env
        t.processor.secret = tunnel.read_env_file(env)["STRIPE_WEBHOOK_SECRET"]
        return SimpleNamespace(returncode=0, stdout="installed agent\ninstalled tunnel\n", stderr="")

    checks = run_setup(config, kit, SetupOptions(env_file=env, workdir=ROOT, system="Darwin"), online_check=lambda: True,
                       runner=runner, sleep=lambda s: None)
    status = {(c.step, c.name): c.status for c in checks}
    assert not any(c.failed for c in checks), checks
    assert status[("stripe webhook", "endpoint")] == "ok" and status[("launchd", "services")] == "ok"
    assert status[("handshake", "public url")] == "ok"


def test_run_setup_stops_early_on_missing_basics(config, state, breaker, tmp_path):
    kit = build_toolkit(config, state, breaker, transport=FakeTransport(), sleep=lambda s: None)
    checks = run_setup(config, kit, SetupOptions(env_file=tmp_path / "missing.env", workdir=ROOT), online_check=lambda: True)
    assert any(c.failed for c in checks)
    assert all(c.status == "skip" for c in checks if c.step in ("stripe webhook", "launchd", "handshake"))


def test_supervise_starts_paused_instead_of_exiting(tmp_path, monkeypatch):
    """Under launchd KeepAlive an exit is a restart loop; a paused start keeps the webhook up."""
    from dashboard import cli

    data = tmp_path / "data"
    cfg_file = tmp_path / "a.toml"
    cfg_file.write_text(f'[automonetize]\ndata_dir = "{data}"\nnetwork_check_hosts = []\n')
    ran = {}

    class Sup:
        def __init__(self, config, engine, **kw):
            ran["stopped"] = engine.is_stopped()[0]

        def run(self):
            return "done"

    monkeypatch.setattr("agent.supervisor.Supervisor", Sup)
    assert cli.main(["-c", str(cfg_file), "stop", "--reason", "maintenance"]) == 0
    assert cli.main(["-c", str(cfg_file), "supervise", "--headless", "--no-webhook"]) == 0
    assert ran["stopped"] is True
