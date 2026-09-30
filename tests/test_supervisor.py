import os
import plistlib
import signal
import stat
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from agent.connectivity import is_online
from agent.engine import Engine, ProcessLock
from agent.supervisor import Supervisor
from strategies.base import Strategy, TaskResult

ROOT = Path(__file__).resolve().parents[1]


class Noop(Strategy):
    name = "noop"
    tasks = ("aggregate_leads",)

    def run(self, task, ctx):
        return TaskResult(True, "ok")


@pytest.fixture
def engine(config, state, toolkit):
    return Engine(config, state=state, strategies=[Noop()], toolkit=toolkit, sleep=lambda s: None)


def fast(config):
    config.supervisor_max_restarts = 2
    config.shutdown_timeout_seconds = 5


# ------------------------------------------------------------------ lifecycle
def test_runs_engine_to_completion_and_checkpoints(config, state, engine):
    fast(config)
    sup = Supervisor(config, engine=engine, webhook=False, interval=0, max_cycles=3, poll_seconds=0.01)
    assert sup.run() == "engine finished"
    assert state.get("iteration") == 3
    cp = state.get("last_shutdown")
    assert cp["reason"] == "engine finished" and cp["iteration"] == 3 and cp["unfinished"] == []
    assert cp["workers"]["engine"] == {"starts": 1, "crashes": 0, "disabled": False}
    assert state.get("supervisor") is None
    # WAL flushed into the main file and the process lock released
    wal = Path(str(config.db_path) + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0
    other = ProcessLock(config.data_dir / "agent.lock")
    assert other.acquire()
    other.release()


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_signal_triggers_graceful_shutdown(config, state, engine, sig):
    fast(config)
    before = signal.getsignal(sig)
    sup = Supervisor(config, engine=engine, webhook=False, interval=3600, poll_seconds=0.01)
    threading.Timer(0.3, lambda: os.kill(os.getpid(), sig)).start()
    started = time.monotonic()
    reason = sup.run()
    assert reason == sig.name
    assert time.monotonic() - started < 5, "interval wait must be interruptible"
    assert state.get("last_shutdown")["reason"] == sig.name
    assert state.get("iteration") >= 1
    assert signal.getsignal(sig) is before, "previous handler restored"


def test_second_instance_refused(config, engine):
    fast(config)
    lock = ProcessLock(config.data_dir / "agent.lock")
    assert lock.acquire()
    try:
        with pytest.raises(RuntimeError):
            Supervisor(config, engine=engine, webhook=False).start()
    finally:
        lock.release()


def test_crashing_worker_is_restarted_with_backoff(config, state, engine):
    fast(config)
    sup = Supervisor(config, engine=engine, webhook=False, interval=0, max_cycles=1, backoff_base=0.01, poll_seconds=0.01)
    real = sup.workers[0].target
    calls = {"n": 0}

    def flaky(stop):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise ConnectionError("network blip")
        return real(stop)

    sup.workers[0].target = flaky
    assert sup.run() == "engine finished"
    assert calls["n"] == 3 and sup.workers[0].starts == 3 and len(sup.workers[0].crashes) == 2
    assert sum("worker crashed" in e["message"] for e in state.recent_errors(10)) == 2


def test_critical_worker_crash_loop_stops_supervisor(config, state, engine):
    fast(config)
    sup = Supervisor(config, engine=engine, webhook=False, backoff_base=0.01, poll_seconds=0.01)
    sup.workers[0].target = lambda stop: (_ for _ in ()).throw(RuntimeError("bad config"))
    reason = sup.run()
    assert "engine crashed 3 times" in reason and "bad config" in reason


def test_noncritical_webhook_crash_loop_is_disabled_engine_continues(config, state, engine):
    fast(config)
    config.stripe_webhook_secret = "whsec_x"
    sup = Supervisor(config, engine=engine, webhook=True, interval=0.05, backoff_base=0.01, poll_seconds=0.01)
    webhook = next(w for w in sup.workers if w.name == "webhook")
    webhook.target = lambda stop: (_ for _ in ()).throw(OSError("port in use"))
    threading.Timer(1.0, sup.request_stop, args=("test done",)).start()
    assert sup.run() == "test done"
    assert webhook.disabled and state.get("iteration") >= 2
    assert any("disabled after" in e["message"] for e in state.recent_errors(20))


def test_supervised_webhook_serves_requests(config, state, engine):
    from tools.storefront.webhook_listener import sign_payload

    fast(config)
    config.stripe_webhook_secret, config.webhook_port = "whsec_x", 0
    sup = Supervisor(config, engine=engine, webhook=True, interval=3600, poll_seconds=0.01)
    t = threading.Thread(target=sup.run, daemon=True)
    t.start()
    assert sup.webhook_server.started.wait(5)
    payload = b'{"id": "evt_s", "type": "customer.created", "data": {"object": {}}}'
    req = urllib.request.Request(f"http://127.0.0.1:{sup.webhook_server.bound_port}/webhook", data=payload, method="POST",
                                 headers={"Stripe-Signature": sign_payload(payload, "whsec_x")})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=5) as r:
        assert r.status == 200
    sup.request_stop("done")
    t.join(10)
    assert not t.is_alive() and state.get("last_shutdown")["reason"] == "done"


# ------------------------------------------------------------------ network drops
def test_offline_cycles_wait_without_tripping_breaker(config, state, toolkit):
    online = {"up": False}
    eng = Engine(config, state=state, strategies=[Noop()], toolkit=toolkit, sleep=lambda s: None, online_check=lambda: online["up"])
    for _ in range(config.max_consecutive_errors + 3):
        assert eng.run_cycle().status == "offline"
    assert not eng.breaker.tripped and eng.breaker.consecutive_errors == 0
    assert state.get("offline_since")
    online["up"] = True
    assert eng.run_cycle().status == "ran"
    assert state.get("offline_since") is None
    assert any(a["name"] == "network" and "back online" in a["detail"] for a in state.recent_actions(5))


def test_is_online_probe(monkeypatch):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    tried = []

    class Sock:
        def close(self):
            pass

    def connect(addr, timeout):
        tried.append(addr)
        if addr[0] == "up.example":
            return Sock()
        raise OSError("unreachable")

    assert is_online(["down.example:443", "up.example:443"], connect=connect)
    assert tried == [("down.example", 443), ("up.example", 443)]
    assert not is_online(["down.example"], connect=connect)
    assert is_online([], connect=connect)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.local:3128")
    tried.clear()
    is_online(["api.stripe.com:443"], connect=connect)
    assert tried == [("proxy.local", 3128)]


# ------------------------------------------------------------------ deploy templates
def test_launchd_plist_template_renders_valid_plist():
    raw = (ROOT / "deploy" / "com.automonetize.agent.plist").read_text()
    rendered = raw.replace("__WORKDIR__", "/Users/sam/AutoMonetize").replace("__HOME__", "/Users/sam")
    plist = plistlib.loads(rendered.encode())
    assert plist["Label"] == "com.automonetize.agent"
    assert plist["KeepAlive"] is True and plist["RunAtLoad"] is True and plist["ThrottleInterval"] >= 10
    assert plist["StandardOutPath"] == "/Users/sam/Library/Logs/automonetize.stdout.log"
    assert plist["StandardErrorPath"].endswith("Library/Logs/automonetize.stderr.log")
    assert plist["ProgramArguments"] == ["/bin/bash", "/Users/sam/AutoMonetize/deploy/run_agent.sh"]
    assert plist["EnvironmentVariables"]["AUTOMONETIZE_ENV_FILE"] == "/Users/sam/AutoMonetize/.env"
    assert "__" not in rendered


def test_deploy_scripts_are_executable_and_run_supervisor():
    for name in ("run_agent.sh", "install_launchd.sh"):
        p = ROOT / "deploy" / name
        assert p.stat().st_mode & stat.S_IXUSR
    assert "supervise" in (ROOT / "deploy" / "run_agent.sh").read_text()
    assert "run_agent.sh" in (ROOT / "deploy" / "automonetize.service").read_text()
