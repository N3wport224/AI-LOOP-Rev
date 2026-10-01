"""Phases 130-134: failing-task cooldown, log rotation and trimming, backup restore test, secret redaction."""

import logging
from pathlib import Path

from agent import task_cooldown
from agent.backup import make_backup, verify_backup
from agent.engine import Engine
from agent.housekeeping import LAUNCHD_LOG_KEEP, LAUNCHD_LOG_MAX, trim_launchd_logs
from tools.redact import redact


# ------------------------------------------------------------------ Phase 130: cooldown
def test_a_task_that_keeps_failing_rests_for_a_day(state, clock):
    for i in range(2):
        task_cooldown.record(state, "build_site", False, f"boom {i}")
    assert task_cooldown.cooling(state, "build_site") is None
    task_cooldown.record(state, "build_site", False, "HttpError 502 sk_live_ABCDEFGHIJKL")
    assert task_cooldown.cooling(state, "build_site")
    alert = [e for e in state.recent_errors(5) if e["kind"] == "alert"][0]["message"]
    assert "build_site failed 3 times" in alert and "sk_live_" not in alert  # Phase 134 at work
    clock.advance(hours=25)
    assert task_cooldown.cooling(state, "build_site") is None
    task_cooldown.record(state, "build_site", True)
    assert "build_site" not in (state.get(task_cooldown.KEY) or {})
    for _ in range(5):
        task_cooldown.record(state, "deliver_orders", False, "smtp down")
    assert task_cooldown.cooling(state, "deliver_orders") is None  # customers' tasks never rest


def test_the_engine_skips_a_resting_task(config, state, toolkit, make_hypothesis):
    eng = Engine(config, state=state, toolkit=toolkit, online_check=lambda: True)
    hyp = make_hypothesis()
    for _ in range(3):
        task_cooldown.record(state, "build_site", False, "x")
    tid = state.add_task(hyp["id"], "build_site", {}, 32)
    task = next(t for t in state.pending_tasks(hyp["id"]) if t["id"] == tid)
    assert eng._execute(1, state.get_hypothesis(hyp["id"]), task)["summary"] == "resting"


# ------------------------------------------------------------------ Phases 131-132: logs
def test_agent_log_rotates(config, tmp_path):
    config.ensure_dirs()
    from dashboard.cli import _setup_logging

    root = logging.getLogger()
    before = list(root.handlers)
    try:
        for h in list(root.handlers):
            root.removeHandler(h)
        _setup_logging(config, headless=False, verbose=False)
        handler = next(h for h in root.handlers if h not in before)
        assert type(handler).__name__ == "RotatingFileHandler" and handler.maxBytes == 10 * 1024 * 1024
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
            h.close()
        for h in before:
            root.addHandler(h)


def test_big_service_logs_keep_their_tail(tmp_path):
    big = tmp_path / "agent.stdout.log"
    line = b"x" * 99 + b"\n"
    big.write_bytes(line * (LAUNCHD_LOG_MAX // 100 + 10) + b"LAST LINE\n")
    small = tmp_path / "agent.stderr.log"
    small.write_bytes(b"short\n")
    assert trim_launchd_logs(tmp_path) == 1
    data = big.read_bytes()
    assert data.startswith(b"[older lines trimmed") and data.endswith(b"LAST LINE\n") and len(data) <= LAUNCHD_LOG_KEEP + 100
    assert small.read_bytes() == b"short\n" and trim_launchd_logs(tmp_path / "missing") == 0


# ------------------------------------------------------------------ Phase 133: restore test
def test_the_newest_backup_really_opens(config, state, clock, tmp_path):
    config.backup_dir = str(tmp_path / "backups")
    assert verify_backup(config)["detail"] == "no backup yet"
    state.record_order("stripe", "cs_1", "a@co.example", 1900, None, None, None)
    path = make_backup(config, None, None, clock())
    result = verify_backup(config)
    assert result["ok"] and result["name"] == path.name and "1 orders" in result["detail"]
    (Path(path) / "agent_state.db").write_bytes(b"not a database at all" * 100)
    bad = verify_backup(config)
    assert not bad["ok"] and "agent_state.db" in bad["detail"]


# ------------------------------------------------------------------ Phase 134: redaction
def test_secrets_never_reach_the_errors_table(state):
    state.log_error("x", "bad key sk_live_0123456789abcdef and password=hunter2", "Bearer abcdefghijkl ghp_0123456789abcdef")
    row = state.recent_errors(1)[0]
    assert "sk_live_" not in row["message"] and "hunter2" not in row["message"] and "password=[redacted]" in row["message"]
    assert "abcdefghijkl" not in row["traceback"] and "ghp_" not in row["traceback"]
    assert redact("") == "" and redact("nothing secret here") == "nothing secret here"
