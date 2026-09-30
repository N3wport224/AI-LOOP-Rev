import pytest

from agent.engine import PLAN, Engine, ProcessLock
from strategies.base import Strategy, TaskResult


class ScriptedStrategy(Strategy):
    """Handles every pipeline task with scripted behaviour for engine tests."""

    name = "scripted"
    tasks = ("aggregate_leads", "package_asset", "stage_outreach")

    def __init__(self, behaviour=None):
        self.behaviour = behaviour or {}
        self.calls = []

    def run(self, task, ctx):
        self.calls.append((task, ctx.attempt, dict(ctx.payload), ctx.niche))
        b = self.behaviour.get(task)
        if callable(b):
            return b(ctx)
        return TaskResult(True, f"{task} ok")


@pytest.fixture
def engine_factory(config, state, toolkit):
    def make(strategy=None):
        return Engine(config, state=state, strategies=[strategy or ScriptedStrategy()], toolkit=toolkit, sleep=lambda s: None)

    return make


def test_cycle_formulates_plans_and_executes(engine_factory, state):
    strat = ScriptedStrategy()
    engine = engine_factory(strat)
    report = engine.run_cycle()
    assert report.status == "ran" and report.cycle == 1
    planned = [t for t, _ in engine.planned_tasks()]
    assert planned == ["aggregate_leads", "package_asset", "stage_outreach", "sync_revenue"]  # handlers only
    assert [a["task"] for a in report.actions] == planned
    assert all(a["status"] == "ok" for a in report.actions)
    hyp = state.active_hypothesis()
    assert hyp["key"] == "lead_directory:python-remote:g1" and hyp["iterations"] == 1
    assert state.get("objective") == hyp["description"]
    names = [a["name"] for a in state.recent_actions(20)]
    assert "formulate_hypothesis" in names
    # next cycle re-plans because the backlog drained
    engine.run_cycle()
    assert len(strat.calls) == 6


def test_pivots_after_n_iterations_without_revenue(engine_factory, state, config):
    engine = engine_factory()
    for _ in range(config.pivot_after_iterations):
        engine.run_cycle()
    first = state.active_hypothesis()
    assert first["iterations"] == config.pivot_after_iterations
    report = engine.run_cycle()
    assert report.pivots and "zero verified revenue" in report.pivots[0]
    assert state.get_hypothesis(first["id"])["status"] == "deprecated"
    # pivots to a cluster adjacent to the deprecated one
    assert state.active_hypothesis()["params"]["niche"] == "ai-infrastructure"
    assert report.hypothesis_key.startswith("lead_directory:ai-infrastructure")


def test_revenue_prevents_pivot(engine_factory, state, config):
    engine = engine_factory()
    engine.run_cycle()
    hyp = state.active_hypothesis()
    state.record_revenue("gumroad", "sale1", 900, 140, 760, True, hypothesis_id=hyp["id"])
    for _ in range(config.pivot_after_iterations + 2):
        engine.run_cycle()
    assert state.active_hypothesis()["id"] == hyp["id"]


def test_unverified_revenue_does_not_count_as_traction(engine_factory, state, config):
    engine = engine_factory()
    engine.run_cycle()
    hyp = state.active_hypothesis()
    state.record_revenue("manual", "m1", 900, 0, 900, False, hypothesis_id=hyp["id"])
    for _ in range(config.pivot_after_iterations):
        engine.run_cycle()
    assert state.get_hypothesis(hyp["id"])["status"] == "deprecated"


def test_invalidating_result_pivots_immediately(engine_factory, state):
    strat = ScriptedStrategy({"aggregate_leads": lambda ctx: TaskResult(True, "no data", invalidates_hypothesis=True)})
    engine = engine_factory(strat)
    report = engine.run_cycle()
    assert len(report.actions) == 1 and report.pivots
    first = state.list_hypotheses()[0]
    assert first["status"] == "deprecated" and "disproved" in first["reason"]
    assert state.pending_tasks(first["id"]) == []
    engine.run_cycle()
    second = state.list_hypotheses()[1]
    assert second["params"]["niche"] == "ai-infrastructure"


def test_retries_with_adjusted_payload_then_succeeds(engine_factory, state):
    def flaky(ctx):
        if ctx.attempt < 3:
            raise ConnectionError("transient")
        return TaskResult(True, "recovered")

    strat = ScriptedStrategy({"aggregate_leads": flaky})
    report = engine_factory(strat).run_cycle()
    agg = report.actions[0]
    assert agg["status"] == "ok" and agg["attempts"] == 3
    attempts = [c for c in strat.calls if c[0] == "aggregate_leads"]
    assert [c[1] for c in attempts] == [1, 2, 3]
    assert attempts[1][2]["retry"] == 1 and "transient" in attempts[1][2]["last_error"]
    errs = state.recent_errors(10)
    assert len(errs) == 2 and all("Traceback" in e["traceback"] for e in errs)


def test_operational_failure_after_three_attempts(engine_factory, state):
    def always(ctx):
        raise TimeoutError("api down")

    strat = ScriptedStrategy({"aggregate_leads": always})
    engine = engine_factory(strat)
    report = engine.run_cycle()
    assert report.actions[0]["status"] == "failed"
    assert len([c for c in strat.calls if c[0] == "aggregate_leads"]) == 3
    assert state.count_errors("operational_failure") == 1
    assert engine.breaker.total_failures == 1
    # the rest of the plan still ran and succeeded, resetting the streak
    assert [a["status"] for a in report.actions[1:]] == ["ok", "ok", "ok"]
    assert engine.breaker.consecutive_errors == 0


def test_consecutive_failures_trigger_emergency_stop(engine_factory, state, config):
    def always(ctx):
        raise RuntimeError("broken")

    strat = ScriptedStrategy({t: always for t in ScriptedStrategy.tasks})
    engine = engine_factory(strat)
    report = engine.run_cycle()
    assert "emergency stop" in report.message
    assert engine.breaker.tripped
    assert len(report.actions) == config.max_consecutive_errors
    assert config.stop_file.exists()
    calls_before = len(strat.calls)

    stopped = engine.run_cycle()
    assert stopped.status == "stopped" and len(strat.calls) == calls_before

    # survives a restart
    fresh = engine_factory(ScriptedStrategy())
    assert fresh.is_stopped()[0]
    fresh.resume()
    assert not fresh.is_stopped()[0] and not config.stop_file.exists()
    assert fresh.run_cycle().status == "ran"


def test_stop_file_halts_loop(engine_factory, config):
    engine = engine_factory()
    config.stop_file.write_text("operator says stop")
    assert engine.run_cycle().status == "stopped"


def test_manual_emergency_stop(engine_factory, state):
    engine = engine_factory()
    engine.emergency_stop("testing")
    assert engine.run_cycle().status == "stopped"
    assert state.recent_errors(1, kind="circuit")[0]["message"].endswith("testing")


def test_action_budget_caps_cycle(engine_factory, config):
    config_budget = 2
    engine = engine_factory()
    engine.breaker.max_actions_per_cycle = config_budget
    report = engine.run_cycle()
    assert len(report.actions) == config_budget
    # remaining tasks carry over to the next cycle instead of being re-planned
    report2 = engine.run_cycle()
    assert [a["task"] for a in report2.actions] == ["stage_outreach", "sync_revenue"]


def test_api_budget_exhaustion_requeues_task(engine_factory, state, transport, toolkit):
    from strategies.b2b_lead_aggregator import LeadAggregator

    toolkit.breaker.max_api_calls_per_cycle = 1
    engine = Engine(toolkit.config, state=state, strategies=[LeadAggregator(), ScriptedStrategy()], toolkit=toolkit, sleep=lambda s: None)
    engine.handlers["aggregate_leads"] = LeadAggregator()
    report = engine.run_cycle()
    assert report.actions[0]["status"] == "circuit_open"
    assert len(report.actions) == 1
    assert state.pending_tasks(report.hypothesis_id)[0]["task"] == "aggregate_leads"
    assert not engine.breaker.tripped


def test_hypothesis_space_exhausted_goes_idle(engine_factory, state, config):
    config.niches = []
    config.max_hypothesis_generations = 1
    report = engine_factory().run_cycle()
    assert report.status == "idle"


def test_run_forever_respects_max_cycles_and_survives_crash(engine_factory, state, monkeypatch):
    engine = engine_factory()
    real = engine.run_cycle
    calls = {"n": 0}

    def sometimes_crash():
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("bug in cycle")
        return real()

    monkeypatch.setattr(engine, "run_cycle", sometimes_crash)
    reports = []
    assert engine.run_forever(interval=0, max_cycles=3, on_cycle=reports.append, install_signal_handlers=False) == 3
    assert [r.status for r in reports] == ["ran", "crashed", "ran"]
    assert any("cycle crashed" in e["message"] for e in state.recent_errors(20))
    assert state.get("started_at") is not None and state.get("pid") is None


def test_request_stop_ends_loop(engine_factory):
    engine = engine_factory()
    engine.request_stop()
    assert engine.run_forever(interval=0, install_signal_handlers=False) == 0


def test_full_pipeline_with_real_strategies(config, state, toolkit, transport):
    engine = Engine(config, state=state, toolkit=toolkit, sleep=lambda s: None)
    report = engine.run_cycle()
    statuses = {a["task"]: a["status"] for a in report.actions}
    assert statuses == {t: "ok" for t, _ in PLAN}
    assert state.count_leads("python-remote") == 4
    assert state.list_assets()[0]["version"] == 1
    assert state.outreach_counts()["pending_review"] == 2
    assert toolkit.files.exists("site/python-remote/index.html")
    assert toolkit.files.exists("exports/intel/python-remote/EXECUTIVE_TECH_RADAR.md")
    assert toolkit.files.exists("showcase/python-remote/README.md")


def test_full_pipeline_syncs_gumroad_revenue(config, state, toolkit, transport):
    toolkit.revenue.gumroad_token = "tok"
    transport.add_json("https://api.gumroad.com/v2/products", {"success": True, "products": [{"id": "p1", "name": "Python Remote Tech Stack Intel"}]})
    transport.add_json("https://api.gumroad.com/v2/sales", {"success": True, "sales": [{"id": "s1", "price": 1500, "product_id": "p1", "created_at": "2026-09-30T11:00:00Z"}]})
    engine = Engine(config, state=state, toolkit=toolkit, sleep=lambda s: None)
    engine.run_cycle()
    hyp = state.active_hypothesis()
    assert state.revenue_for_hypothesis(hyp["id"]) == 1300
    assert toolkit.revenue.daily_summary()["target_met"]


def test_process_lock(tmp_path):
    a, b = ProcessLock(tmp_path / "lock"), ProcessLock(tmp_path / "lock")
    assert a.acquire()
    assert not b.acquire()
    a.release()
    assert b.acquire()
    b.release()


def test_uptime_freezes_after_loop_exit(engine_factory, state, clock):
    from agent.engine import uptime_seconds

    engine = engine_factory()

    def tick(report):
        clock.advance(seconds=30)

    engine.run_forever(interval=0, max_cycles=2, on_cycle=tick, install_signal_handlers=False)
    assert uptime_seconds(state) == 60
    clock.advance(hours=5)
    assert uptime_seconds(state) == 60
