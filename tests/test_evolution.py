"""Phase 11: autonomous self-evolution: boundaries, worktree workflow, canary/rollback, hot reload."""

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest

from agent.config import Config
from agent.engine import PLAN, Engine
from agent.evolution import hot_reload
from agent.evolution.diagnostics import (
    Finding, block_value, diagnose, diagnose_parsers, diagnose_revenue_plateau, diagnose_unknown_tags, with_block,
)
from agent.evolution.evolver import (
    Check, EvolutionHypothesis, Evolver, FileChange, content_violation, patch_violations, scope_violation,
)
from agent.evolution.log import EvolutionLog
from agent.evolution.task import EvolutionStrategy, blocked
from strategies.base import Strategy, TaskContext, TaskResult

ROOT = Path(__file__).resolve().parents[1]
HEUR = '''"""A heuristic module with one evolvable block."""

# <evolved:TABLE> auto-evolution may rewrite this block (one literal assignment)
TABLE: dict[str, int] = {"a": 1}
# </evolved:TABLE>


def value(key: str) -> int:
    return TABLE.get(key, 0)
'''
TEST_HEUR = '''from strategies.heur import TABLE, value


def test_value():
    assert value("a") == 1


def test_bounded():
    assert all(v < 100 for v in TABLE.values())
'''


def git(repo, *args):
    return subprocess.run(["git", "-c", "user.name=Human", "-c", "user.email=human@example.com", "-c", "commit.gpgsign=false", *args],
                          cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    files = {
        "strategies/__init__.py": "", "strategies/heur.py": HEUR, "tests/test_heur.py": TEST_HEUR,
        "seeds/copy_variants.json": '{\n  "headline": {}\n}\n', "agent/__init__.py": "",
        "agent/supervisor.py": "# core\n", "agent/recovery.py": "# core\n", "tools/__init__.py": "",
        "tools/storefront/__init__.py": "", "tools/storefront/webhook_listener.py": "# core\n", ".gitignore": "data/\n",
    }
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "initial")
    return root


CHECKS = [Check("pytest", [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"], 120)]


@pytest.fixture
def elog(config, clock):
    return EvolutionLog(config.data_dir / "evolution_log.db", clock=clock)


def table_patch(repo, value, title="Improved heuristics for a"):
    before = (repo / "strategies/heur.py").read_text()
    return EvolutionHypothesis("parser_heuristics", title, "because", "leads parsed", 0.0, 10.0,
                               [FileChange("strategies/heur.py", before, with_block(before, "TABLE", value))])


def evolver(repo, elog, config, clock, **kw):
    config.evolution_repo = str(repo)
    return Evolver(repo, elog, config, checks=kw.pop("checks", CHECKS), clock=clock, **kw)


def branches(repo):
    return [b.strip("* ").strip() for b in git(repo, "branch", "--list").splitlines()]


# ------------------------------------------------------------------ boundaries
@pytest.mark.parametrize("path", [
    "agent/supervisor.py", "tools/storefront/webhook_listener.py", "agent/recovery.py", "tests/test_api.py", "tests/conftest.py",
    "strategies/test_sneaky.py", "strategies/conftest.py", "agent/evolution/evolver.py", "agent/evolution/task.py",
    "agent/engine.py", "tools/copy_bandit.py", "api/server.py", "../outside.py", "/etc/passwd", "strategies/../agent/engine.py",
    "strategies//x.py", ".github/workflows/ci.yml", "seeds/evil.py", "strategies/data.json", "strategies/.hidden.py", "",
])
def test_immutable_core_and_everything_outside_scope_is_refused(path):
    assert scope_violation(path)


@pytest.mark.parametrize("path", ["strategies/tech_stack_intel.py", "strategies/b2b_lead_aggregator.py",
                                  "tools/syndication/devto_publisher.py", "tools/page_builder.py", "seeds/copy_variants.json"])
def test_allowed_targets(path):
    assert scope_violation(path) is None


def test_forbidden_patch_is_rejected_before_anything_runs(repo, elog, config, clock):
    calls = []
    ev = evolver(repo, elog, config, clock, run=lambda *a, **k: calls.append(a) or subprocess.run(*a, **k))
    hyp = EvolutionHypothesis("x", "Disable the supervisor", "", "m", 0, 1,
                              [FileChange("agent/supervisor.py", "# core\n", "import os\nos._exit(0)\n")])
    out = ev.attempt(hyp)
    assert out.status == "rejected" and "immutable core" in out.detail
    assert calls == []  # no git, no worktree, no tests
    row = elog.get(out.attempt_id)
    assert row["status"] == "rejected" and elog.blacklisted(hyp.fingerprint) and elog.cooldown_until() is None
    mixed = EvolutionHypothesis("x", "sneak a test edit in", "", "m", 0, 1, [
        FileChange("strategies/heur.py", HEUR, with_block(HEUR, "TABLE", {"a": 1, "b": 2})),
        FileChange("tests/test_heur.py", TEST_HEUR, "def test_value():\n    pass\n")])
    assert any("tests/test_heur.py" in p for p in patch_violations(mixed))


def test_only_literal_evolvable_blocks_may_change():
    ok = with_block(HEUR, "TABLE", {"a": 1, "b": 2})
    assert content_violation(FileChange("strategies/heur.py", HEUR, ok)) is None
    outside = ok.replace("return TABLE.get(key, 0)", "return 99")
    assert "outside the evolvable blocks" in content_violation(FileChange("strategies/heur.py", HEUR, outside))
    code = HEUR.replace('TABLE: dict[str, int] = {"a": 1}', 'TABLE: dict[str, int] = __import__("os").system("id")')
    assert "pure literal" in content_violation(FileChange("strategies/heur.py", HEUR, code))
    two = HEUR.replace('TABLE: dict[str, int] = {"a": 1}', 'import os\nTABLE: dict[str, int] = {"a": 1}')
    assert "exactly one assignment" in content_violation(FileChange("strategies/heur.py", HEUR, two))
    renamed = HEUR.replace('TABLE: dict[str, int] = {"a": 1}', 'OTHER: dict[str, int] = {"a": 1}')
    assert "name and its type annotation" in content_violation(FileChange("strategies/heur.py", HEUR, renamed))
    broken = HEUR.replace('{"a": 1}', '{"a": 1')
    assert "not valid Python" in content_violation(FileChange("strategies/heur.py", HEUR, broken))
    dropped = HEUR.replace("# </evolved:TABLE>\n", "")
    assert "added, removed or renamed" in content_violation(FileChange("strategies/heur.py", HEUR, dropped))
    assert "no evolvable blocks" in content_violation(FileChange("strategies/x.py", "X = 1\n", "X = 2\n"))
    assert "can't create Python modules" in content_violation(FileChange("strategies/new.py", None, "X = 1\n"))
    assert "JSON object" in content_violation(FileChange("seeds/a.json", None, "[1, 2]"))
    assert content_violation(FileChange("seeds/a.json", None, '{"headline": {}}')) is None


def test_the_real_evolvable_blocks_accept_evolution():
    probe = {"__evolution_probe__": ["never-a-real-value"]}  # can't coincide with anything learned
    for rel, block in (("strategies/tech_stack_intel.py", "FINGERPRINTS"), ("strategies/b2b_lead_aggregator.py", "FIELD_ALIASES")):
        text = (ROOT / rel).read_text()
        assert isinstance(block_value(text, block), dict)
        assert content_violation(FileChange(rel, text, with_block(text, block, probe))) is None


COMMON_WORDS = "the and for with you our are team work will this that from have your new build help data code role job remote"


@pytest.mark.evolved_data
def test_live_evolved_data_honours_its_contract():
    """Whatever the agent has learned so far must stay sane (runs on the evolved values)."""
    import re

    import strategies.b2b_lead_aggregator as agg
    import strategies.tech_stack_intel as intel
    from agent.evolution.diagnostics import PARSER_FIELDS
    from tools import copy_bandit

    text = (ROOT / "strategies/tech_stack_intel.py").read_text()
    for name, patterns in block_value(text, "FINGERPRINTS").items():
        assert isinstance(name, str) and 1 <= len(name) <= 40 and patterns
        for pat in patterns:
            rx = re.compile(r"(?<![\w+#.])(" + pat + r")(?![\w+#])", re.I)
            assert not rx.search(COMMON_WORDS), f"{name}: {pat!r} matches everyday words"
    assert set(intel.FINGERPRINTS.get("other", {})) <= set(block_value(text, "FINGERPRINTS"))
    for source, fields in agg.FIELD_ALIASES.items():
        assert source in PARSER_FIELDS and set(fields) <= set(PARSER_FIELDS[source])
        assert all(isinstance(a, str) and a for aliases in fields.values() for a in aliases)
    facts = {"label": "Python", "companies": 12, "hot": 3, "verified": 2, "price": "$14.00"}
    for name, arm in copy_bandit.SLOTS["headline"].items():
        assert copy_bandit.render_copy("headline", name, facts)
    codes = [a["code"] for a in copy_bandit.SLOTS["headline"].values()]
    assert len(codes) == len(set(codes))


# ------------------------------------------------------------------ worktree workflow
def test_passing_patch_is_tested_in_a_worktree_and_fast_forwarded(repo, elog, config, clock):
    base = git(repo, "rev-parse", "HEAD")
    hyp = table_patch(repo, {"a": 1, "b": 7})
    out = evolver(repo, elog, config, clock).attempt(hyp)
    assert out.status == "merged", out.output
    assert git(repo, "rev-parse", "HEAD~1") == base and git(repo, "rev-parse", "HEAD") == out.commit_sha  # fast-forward
    assert git(repo, "log", "-1", "--format=%s").startswith("[Auto-Evolution] Improved heuristics for a")
    assert git(repo, "log", "-1", "--format=%an <%ae>") == "AutoMonetize Agent <agent@automonetize.invalid>"
    assert f"Evolution-Fingerprint: {hyp.fingerprint}" in git(repo, "log", "-1", "--format=%b")
    assert '"b": 7' in (repo / "strategies/heur.py").read_text()
    assert len(git(repo, "worktree", "list").splitlines()) == 1 and branches(repo) == ["main"]
    row = elog.get(out.attempt_id)
    assert row["status"] == "merged" and row["commit_sha"] == out.commit_sha and "pytest: ok" in row["output"]
    assert "+TABLE" in row["diff"] and row["branch"].startswith("auto/evolution-")


def test_failing_patch_is_discarded_logged_blacklisted_and_cools_down(repo, elog, config, clock):
    base = git(repo, "rev-parse", "HEAD")
    hyp = table_patch(repo, {"a": 1, "b": 1000})  # breaks test_bounded
    out = evolver(repo, elog, config, clock).attempt(hyp)
    assert out.status == "failed" and out.detail == "pytest failed"
    assert git(repo, "rev-parse", "HEAD") == base and (repo / "strategies/heur.py").read_text() == HEUR
    assert len(git(repo, "worktree", "list").splitlines()) == 1 and branches(repo) == ["main"]
    row = elog.get(out.attempt_id)
    assert row["status"] == "failed" and "test_bounded" in row["output"] and "assert" in row["output"]
    assert elog.blacklisted(hyp.fingerprint) and elog.tried(hyp.fingerprint)
    assert elog.cooldown_until() == clock() + timedelta(hours=24)
    assert "blacklisted" in json.dumps(elog.summary()) and elog.summary()["failed"] == 1


def test_timeout_and_simulator_failures_count_as_failures(repo, elog, config, clock):
    slow = [Check("simulator", [sys.executable, "-c", "import time; time.sleep(5)"], 0.5)]
    out = evolver(repo, elog, config, clock, checks=slow).attempt(table_patch(repo, {"a": 1, "c": 2}))
    assert out.status == "failed" and "timed out" in elog.get(out.attempt_id)["output"]
    assert branches(repo) == ["main"]


def test_dirty_checkout_or_detached_head_aborts_without_blame(repo, elog, config, clock):
    (repo / "strategies/heur.py").write_text(HEUR + "# local edit\n")
    hyp = table_patch(repo, {"a": 1, "d": 3})
    out = evolver(repo, elog, config, clock).attempt(hyp)
    assert out.status == "aborted" and "uncommitted" in out.detail
    assert not elog.blacklisted(hyp.fingerprint) and not elog.tried(hyp.fingerprint) and elog.cooldown_until() is None
    git(repo, "checkout", "--", ".")
    git(repo, "checkout", "-q", "--detach")
    assert evolver(repo, elog, config, clock).attempt(hyp).status == "aborted"


def test_stale_patch_is_rejected(repo, elog, config, clock):
    hyp = table_patch(repo, {"a": 1, "e": 4})
    (repo / "strategies/heur.py").write_text(HEUR.replace('{"a": 1}', '{"a": 1, "z": 9}'))
    git(repo, "commit", "-qam", "human change")
    out = evolver(repo, elog, config, clock).attempt(hyp)
    assert out.status == "rejected" and "stale" in out.detail and branches(repo) == ["main"]


def test_checkout_moving_during_the_checks_aborts_the_merge(repo, elog, config, clock):
    commit = [sys.executable, "-c", "import subprocess, sys; subprocess.run(['git', '-C', sys.argv[1], '-c', 'user.name=H', '-c', "
              "'user.email=h@example.com', 'commit', '-q', '--allow-empty', '-m', 'human'], check=True)", str(repo)]
    out = evolver(repo, elog, config, clock, checks=[Check("human commits meanwhile", commit, 60)]).attempt(table_patch(repo, {"a": 1, "f": 5}))
    assert out.status == "aborted", elog.get(out.attempt_id)["output"]
    assert git(repo, "log", "-1", "--format=%s") == "human" and branches(repo) == ["main"]


# ------------------------------------------------------------------ canary and rollback
def merged(repo, elog, config, clock, state):
    hyp = table_patch(repo, {"a": 1, "g": 6})
    out = evolver(repo, elog, config, clock).attempt(hyp)
    assert out.status == "merged"
    hot_reload.start_canary(elog, state, config, out.attempt_id, out.commit_sha, ["strategies/heur.py"], hyp.title)
    return hyp, out


def test_exception_in_evolved_code_rolls_back_blacklists_and_reloads(repo, elog, config, clock, state):
    hyp, out = merged(repo, elog, config, clock, state)
    reloads = []
    assert hot_reload.canary_tick(state, config, reload=reloads.append) == "monitoring"
    state.log_error("task:build_intel", "boom", 'Traceback...\n  File "/x/strategies/heur.py", line 9, in value\nKeyError')
    assert hot_reload.canary_tick(state, config, reload=reloads.append) == "rolled_back"
    assert git(repo, "log", "-1", "--format=%s").startswith("[Auto-Evolution] Rollback: Improved heuristics for a")
    assert f"Reverts {out.commit_sha}" in git(repo, "log", "-1", "--format=%b")
    assert (repo / "strategies/heur.py").read_text() == HEUR
    assert elog.get(out.attempt_id)["status"] == "rolled_back" and elog.blacklisted(hyp.fingerprint)
    assert elog.cooldown_until() is not None and reloads and reloads[0].startswith("rollback:")
    assert any("canary failed" in e["message"] for e in state.recent_errors(5, kind="alert"))
    summary = elog.summary()
    assert summary["rolled_back"] == 1 and summary["canary"]["status"] == "rolled_back"


def test_canary_passes_after_the_window_and_a_clean_cycle(repo, elog, config, clock, state):
    merged(repo, elog, config, clock, state)
    state.log_error("task:sync_revenue", "network blip")  # not in evolved code, not an operational failure
    clock.advance(minutes=61)
    assert hot_reload.canary_tick(state, config) == "monitoring"  # no cycle has run on the new code yet
    state.incr("iteration")
    assert hot_reload.canary_tick(state, config, cycle_done=int(state.get("iteration"))) == "passed"
    assert git(repo, "log", "-1", "--format=%s").startswith("[Auto-Evolution] Improved")


def test_only_new_operational_failures_trip_the_canary(repo, elog, config, clock, state):
    state.log_error("task:syndicate", "was already failing", kind="operational_failure")
    merged(repo, elog, config, clock, state)
    state.log_error("task:syndicate", "still failing", kind="operational_failure")
    assert hot_reload.canary_tick(state, config, reload=lambda r: None) == "monitoring"
    state.log_error("task:build_intel", "new failure", kind="operational_failure")
    assert hot_reload.canary_tick(state, config, reload=lambda r: None) == "rolled_back"


def test_engine_crash_trips_the_canary(repo, elog, config, clock, state):
    merged(repo, elog, config, clock, state)
    state.log_error("engine", "cycle crashed: RuntimeError('x')")
    assert hot_reload.canary_tick(state, config, reload=lambda r: None) == "rolled_back"


def test_failed_rollback_halts_evolution(repo, elog, config, clock, state):
    merged(repo, elog, config, clock, state)
    (repo / "strategies/heur.py").write_text("# someone is editing\n")
    state.log_error("engine", "cycle crashed: boom")
    assert hot_reload.canary_tick(state, config, reload=lambda r: None) == "rollback_failed"
    assert "Evolution halted" in elog.halted()
    config.enable_autonomous_code_evolution = True
    assert blocked(config, elog, state).startswith("halted")
    from dashboard import cli
    from rich.console import Console

    cfg = Path(config.data_dir).parent / "a.toml"
    cfg.write_text(f'[automonetize]\ndata_dir = "{config.data_dir}"\nnetwork_check_hosts = []\n')
    assert cli.main(["-c", str(cfg), "evolution", "resume"], console=Console(record=True)) == 0
    assert elog.halted() is None


# ------------------------------------------------------------------ hot reload
def test_listening_socket_is_inherited_across_exec():
    first = hot_reload.listening_socket("127.0.0.1", 0)
    port = first.getsockname()[1]
    env = {hot_reload.LISTEN_FD_ENV: str(os.dup(first.fileno()))}
    again = hot_reload.listening_socket("127.0.0.1", port, env)
    assert again.getsockname()[1] == port and hot_reload.LISTEN_FD_ENV not in env
    assert not os.get_inheritable(again.fileno())
    calls = []
    hot_reload.reexec(again, exec_fn=lambda path, argv, env: calls.append((path, argv, env)))
    path, argv, env = calls[0]
    assert path == sys.executable and argv[0] == sys.executable
    assert env[hot_reload.LISTEN_FD_ENV] == str(again.fileno()) and os.get_inheritable(again.fileno())
    first.close()
    again.close()


class Noop(Strategy):
    name = "noop"
    tasks = ("aggregate_leads",)

    def run(self, task, ctx):
        return TaskResult(True, "ok")


def http_get(port, path="/healthz"):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode())
        return s.recv(200).decode(errors="replace")


def test_reload_drains_then_reexecs_keeping_the_port_open(config, state, toolkit):
    from agent.supervisor import Supervisor
    from tools.storefront.webhook_listener import WebhookServer

    config.stripe_webhook_secret, config.webhook_port, config.shutdown_timeout_seconds = "whsec_x", 0, 5
    engine = Engine(config, state=state, strategies=[Noop()], toolkit=toolkit, sleep=lambda s: None)
    execs = []
    sup = Supervisor(config, engine=engine, webhook=True, interval=3600, poll_seconds=0.01,
                     exec_fn=lambda path, argv, env: execs.append(env))
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("reason", sup.run()))
    t.start()
    assert sup.webhook_server.started.wait(5)
    port = sup.webhook_server.bound_port
    assert http_get(port).startswith("HTTP/1.1 200")
    hot_reload.request_reload("evolution #1 merged")
    t.join(10)
    assert result["reason"] == "reload: evolution #1 merged" and len(execs) == 1
    assert state.get("last_shutdown")["reason"].startswith("reload") and state.get("supervisor") is None
    fd = int(execs[0][hot_reload.LISTEN_FD_ENV])
    # The old listener is gone, the socket is not: a request sent now waits in the backlog...
    pending = socket.create_connection(("127.0.0.1", port), timeout=5)
    pending.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    # ...and the "new process" (a fresh server on the inherited fd) answers it.
    inherited = hot_reload.listening_socket("127.0.0.1", port, {hot_reload.LISTEN_FD_ENV: str(os.dup(fd))})
    server = WebhookServer(toolkit, port=port)
    server.sock = inherited
    stop = threading.Event()
    threading.Thread(target=server.run, args=(stop,), daemon=True).start()
    try:
        assert pending.recv(200).startswith(b"HTTP/1.1 200")
    finally:
        stop.set()
        pending.close()
        time.sleep(0.3)
        inherited.close()
        sup.listen_socket.close()
    assert not hot_reload.RELOAD.is_set()


def test_sigusr1_requests_a_reload_and_sighup_still_stops(config, state, toolkit):
    from agent.supervisor import Supervisor

    engine = Engine(config, state=state, strategies=[Noop()], toolkit=toolkit, sleep=lambda s: None)
    sup = Supervisor(config, engine=engine, webhook=False)
    hot_reload.RELOAD.clear()
    sup._on_signal(signal.SIGUSR1, None)
    assert hot_reload.RELOAD.is_set() and not sup.stop_event.is_set() and hot_reload.reload_reason() == "SIGUSR1"
    hot_reload.RELOAD.clear()
    sup._on_signal(signal.SIGHUP, None)
    assert sup.stop_event.is_set() and sup.stop_reason == "SIGHUP" and not sup.reloading


# ------------------------------------------------------------------ the engine task
def test_evolve_code_is_registered_last_and_off_by_default(config, state, toolkit):
    assert PLAN[-1] == ("evolve_code", 99) and Config().enable_autonomous_code_evolution is False
    assert Config.load(env={"ENABLE_AUTONOMOUS_CODE_EVOLUTION": "true"}).enable_autonomous_code_evolution is True
    assert Config.load(env={"ENABLE_AUTONOMOUS_CODE_EVOLUTION": "maybe"}).enable_autonomous_code_evolution is False
    engine = Engine(config, state=state, toolkit=toolkit, sleep=lambda s: None)
    assert "evolve_code" in engine.handlers
    res = engine.handlers["evolve_code"].run("evolve_code", TaskContext(toolkit, {"id": 1, "params": {}}, {}))
    assert res.ok and "is off" in res.summary


def test_strategy_merges_starts_the_canary_and_reloads(repo, config, state, toolkit, clock, monkeypatch):
    config.enable_autonomous_code_evolution, config.evolution_repo = True, str(repo)
    hyp = table_patch(repo, {"a": 1, "h": 8})
    monkeypatch.setattr("agent.evolution.task.diagnose", lambda *a, **k: [Finding("parser_failure", "critical", "x", {}, hyp)])
    reloads = []
    strat = EvolutionStrategy(checks=CHECKS, reload=reloads.append)
    ctx = TaskContext(toolkit, {"id": 1, "params": {}}, {})
    res = strat.run("evolve_code", ctx)
    assert res.metrics["outcome"] == "merged" and reloads and "merged" in reloads[0]
    elog = hot_reload.evolution_log(config, clock)
    assert elog.get_kv("canary")["status"] == "monitoring"
    assert "canary watching" in strat.run("evolve_code", ctx).summary  # no second change while the canary runs
    elog.set_kv("canary", {**elog.get_kv("canary"), "status": "passed"})
    assert "next attempt after" in strat.run("evolve_code", ctx).summary  # interval gate
    clock.advance(hours=7)
    assert "nothing to evolve" in strat.run("evolve_code", ctx).summary  # same patch is never re-applied
    assert any(a["name"] == "evolution:merged" for a in state.recent_actions(10))


# ------------------------------------------------------------------ diagnostics
def health(state, source, runs):
    state.set(f"parser_health:{source}", runs)


@pytest.fixture
def pristine(tmp_path):
    """A copy of the evolvable files with nothing learned yet (the live ones may have evolved)."""
    root = tmp_path / "pristine"
    for rel, block in (("strategies/tech_stack_intel.py", "FINGERPRINTS"), ("strategies/b2b_lead_aggregator.py", "FIELD_ALIASES")):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(with_block((ROOT / rel).read_text(), block, {}))
    (root / "seeds").mkdir()
    (root / "seeds/copy_variants.json").write_text('{\n  "headline": {}\n}\n')
    return root


def test_renamed_feed_field_yields_a_parser_patch(state, monkeypatch, pristine):
    import strategies.b2b_lead_aggregator as agg

    keys = {"legal": "short_text", "title": "short_text", "company": "short_text", "url": "url", "tags": "list", "epoch": "epoch"}
    health(state, "remoteok", [{"leads": 0, "items": 40, "keys": keys, "error": ""}] * 3)
    [finding] = diagnose_parsers(state, pristine)
    hyp = finding.hypothesis
    assert hyp.title == "Improved parser heuristics for remoteok" and hyp.changes[0].path == "strategies/b2b_lead_aggregator.py"
    assert patch_violations(hyp) == [] and hyp.expected == 40.0
    aliases = block_value(hyp.changes[0].after, "FIELD_ALIASES")
    assert aliases == {"remoteok": {"position": ["title"]}}
    monkeypatch.setattr(agg, "FIELD_ALIASES", aliases)
    leads = agg.parse_remoteok([{"legal": "x"}, {"id": 1, "title": "Python Engineer", "company": "Acme", "tags": ["python"], "epoch": 1}])
    assert len(leads) == 1 and leads[0].title == "Python Engineer"
    assert agg.LAST_SHAPE["remoteok"]["parsed"] == 1 and agg.LAST_SHAPE["remoteok"]["keys"]["title"] == "short_text"


def test_unreachable_or_healthy_feeds(state, pristine):
    health(state, "arbeitnow", [{"leads": 0, "items": None, "keys": {}, "error": "ConnectionError()"}] * 3)
    health(state, "remoteok", [{"leads": 0, "items": 5, "keys": {}, "error": ""}, {"leads": 12, "items": 12, "keys": {}, "error": ""},
                               {"leads": 0, "items": 5, "keys": {}, "error": ""}])
    findings = diagnose_parsers(state, pristine)
    assert len(findings) == 1 and findings[0].hypothesis is None and "ConnectionError" in findings[0].summary


def test_parse_health_is_recorded_per_run(state):
    from strategies.b2b_lead_aggregator import record_parse

    for i in range(12):
        record_parse(state, "remoteok", i, {"items": i, "parsed": i, "keys": {"position": "short_text"}})
    runs = state.get("parser_health:remoteok")
    assert len(runs) == 10 and runs[-1]["leads"] == 11 and runs[-1]["keys"] == {"position": "short_text"}


def seed_leads(state, n_svelte=6, companies=4, filler=20):
    from strategies.b2b_lead_aggregator import POOL_NICHE

    for i in range(n_svelte):
        state.upsert_lead(f"s{i}", POOL_NICHE, {"title": "Senior Python Engineer", "company": f"Co{i % companies}",
                                                "tags": ["svelte", "python"], "description": "Svelte and Django"})
    for i in range(6):
        state.upsert_lead(f"m{i}", POOL_NICHE, {"title": "Growth Lead", "company": f"M{i}", "tags": ["marketing", "hubspotx"],
                                                "description": "campaigns"})
    for i in range(filler):
        state.upsert_lead(f"f{i}", POOL_NICHE, {"title": "Backend Engineer", "company": f"F{i}", "tags": ["go"], "description": "Go, AWS"})


def test_unknown_technology_tags_are_learned(state, config, pristine):
    seed_leads(state)
    [finding] = diagnose_unknown_tags(state, config, pristine, state.clock())
    hyp = finding.hypothesis
    learned = block_value(hyp.changes[0].after, "FINGERPRINTS")
    assert learned == {"Svelte": ["svelte"]}  # "hubspotx" appears on non-tech postings only, "marketing" is a stopword
    assert patch_violations(hyp) == [] and hyp.baseline == round(100 * 6 / 32, 1) and hyp.expected == 0.0
    import re

    assert re.search(r"(?<![\w+#.])(svelte)(?![\w+#])", "we use Svelte daily", re.I)


def test_roles_and_fields_are_never_learned_as_technologies(state, config, pristine):
    from strategies.b2b_lead_aggregator import POOL_NICHE

    seed_leads(state, n_svelte=0)
    for i in range(8):  # seen in the wild: these sit next to real tech on many postings
        state.upsert_lead(f"r{i}", POOL_NICHE, {"title": "Python Engineer", "company": f"R{i}", "description": "Python, AWS",
                                                "tags": ["web dev", "math", "product manager", "software engineering", "vfx"]})
    assert diagnose_unknown_tags(state, config, pristine, state.clock()) == []


def test_rare_tags_are_not_learned(state, config, pristine):
    seed_leads(state, n_svelte=3)
    assert diagnose_unknown_tags(state, config, pristine, state.clock()) == []


def plateau(state, n=5):
    for c in range(1, n + 1):
        state.log_action(c, 1, "aggregate_leads", "ok", "")


def test_revenue_plateau_proposes_one_factual_headline(state, config, make_hypothesis, tmp_path, pristine):
    from tools import copy_bandit

    plateau(state)
    assert "nothing is on sale" in diagnose_revenue_plateau(state, config, pristine, state.clock())[0].summary
    hyp_row = make_hypothesis()
    aid = state.add_asset(hyp_row["id"], "lead_directory", "T", "x.zip", 1, 10, 1400)
    state.update_asset(aid, status="published", checkout_url="https://buy.stripe.com/x")
    [finding] = diagnose_revenue_plateau(state, config, pristine, state.clock())
    hyp = finding.hypothesis
    assert hyp.changes[0].path == "seeds/copy_variants.json" and patch_violations(hyp) == []
    seed = tmp_path / "copy.json"
    seed.write_text(hyp.changes[0].after)
    arms = copy_bandit.evolved_arms(seed)
    [(name, arm)] = arms.items()
    assert name == "scored_intent" and arm["code"] not in {"m", "h", "v"}
    state.record_order("stripe", "cs_1", "a@b.co", 1400, "plink", aid, hyp_row["id"])
    assert diagnose_revenue_plateau(state, config, pristine, state.clock()) == []


def test_copy_seed_loader_drops_unsafe_arms(tmp_path, monkeypatch):
    from tools import copy_bandit

    seed = tmp_path / "copy.json"
    seed.write_text(json.dumps({"headline": {
        "good_one": {"code": "q", "text": "{companies} {label} companies, each scored for buying intent"},
        "needs_hot": {"code": "r", "text": "{hot} {label} companies hiring urgently", "requires": ["hot"]},
        "unbacked": {"code": "s", "text": "{hot} {label} companies hiring urgently"},
        "made_up": {"code": "t", "text": "{revenue} in revenue from {label} leads"},
        "clash": {"code": "h", "text": "A headline reusing the control's code"},
        "hiring_stack": {"code": "u", "text": "Overrides a built-in arm"},
    }}))
    arms = copy_bandit.evolved_arms(seed)
    assert set(arms) == {"good_one", "needs_hot"}
    monkeypatch.setitem(copy_bandit.SLOTS["headline"], "needs_hot", arms["needs_hot"])
    assert copy_bandit.render_copy("headline", "needs_hot", {"hot": 0, "label": "Python"}) is None
    assert copy_bandit.render_copy("headline", "needs_hot", {"hot": 4, "label": "Python"}) == "4 Python companies hiring urgently"


def test_diagnose_ranks_patches_first_and_survives_a_broken_detector(state, config, monkeypatch, pristine):
    seed_leads(state)
    health(state, "arbeitnow", [{"leads": 0, "items": None, "keys": {}, "error": "timeout"}] * 3)
    monkeypatch.setattr("agent.evolution.diagnostics.diagnose_revenue_plateau", lambda *a, **k: 1 / 0)
    findings = diagnose(state, config, pristine)
    assert findings[0].hypothesis is not None and findings[-1].kind == "diagnostics_error"


# ------------------------------------------------------------------ telemetry surfaces
def test_dashboard_gui_and_cli_show_evolution_state(repo, config, state, clock, tmp_path, elog):
    from rich.console import Console

    from dashboard import cli
    from dashboard.render import render_dashboard
    from dashboard.snapshot import collect_snapshot

    merged(repo, elog, config, clock, state)
    evolver(repo, elog, config, clock).attempt(table_patch(repo, {"a": 1, "b": 1000}, "Breaks things"))
    snap = collect_snapshot(state, config)
    ev = snap["evolution"]
    assert ev["attempted"] == 2 and ev["merged"] == 1 and ev["failed"] == 1 and ev["enabled"] is False
    assert ev["last_commit"]["title"] == "Improved heuristics for a" and ev["canary"]["status"] == "monitoring"
    assert ev["cooldown_seconds"] == 24 * 3600
    console = Console(record=True, width=200)
    console.print(render_dashboard(snap))
    text = console.export_text()
    assert "Autonomous Code Evolution" in text and "2 attempted · 1 merged" in text and "Rollback health: monitoring" in text
    cfg = tmp_path / "a.toml"
    cfg.write_text(f'[automonetize]\ndata_dir = "{config.data_dir}"\nnetwork_check_hosts = []\nevolution_repo = "{repo}"\n')
    def run_cli(*args):
        console = Console(record=True, width=200)
        assert cli.main(["-c", str(cfg), "evolution", *args], console=console) == 0
        return console.export_text()

    out = run_cli("log")
    assert "Breaks things" in out and "failed" in out
    out = run_cli("show", "2", "--output")
    assert "test_bounded" in out and '+    "b": 1000' in out
    out = run_cli()
    assert "OFF" in out and "attempted 2" in out and "canary: monitoring" in out
    from gui.routes.settings import BY_KEY

    assert BY_KEY["ENABLE_AUTONOMOUS_CODE_EVOLUTION"].attr == "enable_autonomous_code_evolution"
