"""Phases 105-109: job-source health, monthly VACUUM, clock check, battery saver, grouped errors."""

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace

from agent.engine import PLAN, Engine
from agent.housekeeping import maybe_vacuum
from agent.ops_checks import HEAVY_TASKS, OpsChecks, power_source, source_health
from strategies.b2b_lead_aggregator import record_parse
from strategies.base import TaskContext
from strategies.owner_reports import grouped_problems, problem_line
from tools.http_client import Response

BATT = "Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1)\t54%; discharging; 3:10 remaining present: true\n"
AC = "Now drawing from 'AC Power'\n -InternalBattery-0 (id=1)\t100%; charged; 0:00 remaining present: true\n"


def pmset(text):
    return lambda *a, **k: SimpleNamespace(stdout=text, returncode=0)


def date_header(transport, skew_seconds):
    when = datetime.now(timezone.utc) - timedelta(seconds=skew_seconds)
    transport.add("https://api.github.com/", Response(200, "u", b"{}", {"date": format_datetime(when, usegmt=True)}))


def run_ops(toolkit, text=AC):
    return OpsChecks(run=pmset(text), system="Darwin").run("ops_checks", TaskContext(toolkit, {"id": 1, "params": {}}, {}))


def alerts(state):
    return [e["message"] for e in state.recent_errors(50) if e["kind"] == "alert"]


# ------------------------------------------------------------------ Phase 105: job sources
def test_a_silent_job_source_is_reported_once_a_day(toolkit, state, config, transport, clock):
    config.lead_sources = ["remoteok", "arbeitnow"]
    date_header(transport, 0)
    record_parse(state, "remoteok", 40, None)
    for _ in range(3):
        record_parse(state, "arbeitnow", 0, None, error="HttpError(500)")
    assert source_health(state, ["remoteok", "arbeitnow"])["arbeitnow"] == {
        "ok": False, "zero_runs": 3, "runs": 3, "last_leads": 0, "last_error": "HttpError(500)", "last_at": state.now()}
    res = run_ops(toolkit)
    assert res.metrics["sources_down"] == 1 and "arbeitnow" in res.summary
    assert sum("arbeitnow" in m for m in alerts(state)) == 1
    run_ops(toolkit)
    assert sum("arbeitnow" in m for m in alerts(state)) == 1  # once a day
    record_parse(state, "arbeitnow", 12, None)
    assert run_ops(toolkit).metrics["sources_down"] == 0


# ------------------------------------------------------------------ Phase 106: VACUUM
def test_vacuum_runs_monthly_and_needs_room(state, config, clock, tmp_path):
    db = tmp_path / "x.db"
    db.write_bytes(b"x" * 1000)
    assert maybe_vacuum(state, db, free_bytes=1500) == 0 and state.get("vacuum_at") is None  # not enough room
    maybe_vacuum(state, db, free_bytes=10_000)
    assert state.get("vacuum_at") == state.now()
    clock.advance(days=10)
    maybe_vacuum(state, db, free_bytes=10_000)
    assert state.get("vacuum_at") != state.now()  # not again within the month
    clock.advance(days=21)
    maybe_vacuum(state, db, free_bytes=10_000)
    assert state.get("vacuum_at") == state.now()


# ------------------------------------------------------------------ Phase 107: clock
def test_a_wrong_clock_raises_one_alert(toolkit, state, transport, clock):
    date_header(transport, 600)  # the server is 10 minutes behind: we are fast
    run_ops(toolkit)
    assert state.get("ops")["clock_skew_s"] > 500
    assert any("clock is 10 min fast" in m for m in alerts(state))
    calls = len(transport.calls_to("https://api.github.com/"))
    run_ops(toolkit)
    assert len(transport.calls_to("https://api.github.com/")) == calls  # checked once a day


def test_clock_ok_and_unreachable_are_silent(toolkit, state, transport):
    date_header(transport, 3)
    run_ops(toolkit)
    assert abs(state.get("ops")["clock_skew_s"]) < 60 and not alerts(state)


# ------------------------------------------------------------------ Phase 108: battery
def test_power_source_parses_pmset():
    assert power_source(pmset(BATT), "Darwin") == {"on_battery": True, "percent": 54}
    assert power_source(pmset(AC), "Darwin") == {"on_battery": False, "percent": 100}
    assert power_source(pmset(BATT), "Linux")["on_battery"] is False


def test_heavy_builds_wait_on_battery(config, state, toolkit, transport, make_hypothesis):
    date_header(transport, 0)
    res = run_ops(toolkit, BATT)
    assert res.metrics["battery_saving"] and "on battery (54%)" in res.summary
    eng = Engine(config, state=state, toolkit=toolkit, online_check=lambda: True)
    hyp = make_hypothesis()
    tid = state.add_task(hyp["id"], "package_asset", {}, 20)
    task = next(t for t in state.pending_tasks(hyp["id"]) if t["id"] == tid)
    out = eng._execute(1, state.get_hypothesis(hyp["id"]), task)
    assert out["status"] == "skipped" and not state.pending_tasks(hyp["id"])
    config.battery_saver = False
    assert not run_ops(toolkit, BATT).metrics["battery_saving"]
    assert "package_asset" in HEAVY_TASKS and ("ops_checks", 6) in PLAN


# ------------------------------------------------------------------ Phase 109: grouped errors
def test_repeated_errors_become_one_line_with_a_count(state, clock):
    for i in range(5):
        state.log_error("task:build_site", f"attempt {i}/3 failed: HttpError(502, 'https://api.github.com/x{i}')",
                        kind="operational_failure")
    state.log_error("ops_checks", "Job source arbeitnow returned no postings", kind="alert")
    since = (clock() - timedelta(hours=24)).isoformat(timespec="seconds")
    groups = grouped_problems(state, since)
    assert [(g["source"], g["n"]) for g in groups] == [("task:build_site", 5), ("ops_checks", 1)]
    assert problem_line(groups[0]).startswith("- task:build_site (×5): attempt 4/3")
    assert "(×" not in problem_line(groups[1])
