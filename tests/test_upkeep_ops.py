"""Phases 45-49: CI, pre-change backups and integrity, housekeeping, disk guard, config check."""

import gzip
import io
import zipfile
from collections import namedtuple
from datetime import timedelta
from pathlib import Path

import pytest

from agent.backup import Backups, integrity, list_backups, safety_backup
from agent.config_check import problems
from agent.disk_guard import DiskGuard, builds_paused
from agent.housekeeping import Housekeeping, prune_versions, rotate_audit
from strategies.base import TaskContext

Usage = namedtuple("Usage", "total used free")
GB = 1024 ** 3
ROOT = Path(__file__).resolve().parents[1]


def tctx(toolkit):
    return TaskContext(toolkit, {"id": 1, "params": {"niche": "py"}, "iterations": 0}, {})


# ------------------------------------------------------------------ Phase 45: CI
def test_ci_runs_the_suite_on_macos_and_linux():
    wf = (ROOT / ".github/workflows/tests.yml").read_text()
    assert "macos-latest" in wf and "ubuntu-latest" in wf and "AM_NO_SELF_UPDATE" in wf
    assert "pytest -W error" in wf and "cli.test_loop" in wf


# ------------------------------------------------------------------ Phase 46: integrity and pre-change backups
def test_a_corrupt_database_alerts_and_keeps_old_backups(config, state, toolkit, tmp_path):
    safety_backup(config, tmp_path, state.clock(), "pre-update")
    assert [b["name"].endswith("pre-update") for b in list_backups(config)] == [True]
    assert integrity(Path(config.data_dir) / "agent_state.db") == "ok"
    bad = Path(config.data_dir) / "evolution_log.db"
    bad.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200 + b"garbage" * 500)
    res = Backups(workdir=tmp_path).run("backup_data", tctx(toolkit))
    assert res.metrics == {"integrity": "failed"}
    alert = state.recent_errors(1, kind="alert")[0]["message"]
    assert "automonetize restore" in alert and "pre-update" in alert
    assert len(list_backups(config)) == 1  # nothing pruned, nothing overwritten


def test_safety_backup_never_raises(config, tmp_path, state):
    config.backup_dir = "/proc/definitely/not/writable"
    assert safety_backup(config, tmp_path, state.clock(), "pre-update") is None


def test_self_update_backs_up_before_switching(monkeypatch):
    import inspect

    from agent import self_update

    src = inspect.getsource(self_update.SelfUpdate.run)
    assert src.index('safety_backup(cfg, repo_root(cfg), state.clock(), "pre-update")') < src.index("up.switch(head, target)")


# ------------------------------------------------------------------ Phase 47: housekeeping
def zip_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.csv", "x\n")
    return buf.getvalue()


def test_old_unsold_versions_go_sold_and_recent_ones_stay(state, toolkit):
    hid = state.create_hypothesis("lead_directory:py:g1", "lead_directory", "py", {"niche": "py"})
    ids = []
    for v in range(1, 6):
        rel = f"assets/py/py-intel-v{v}.zip"
        toolkit.files.write_bytes(rel, zip_bytes())
        toolkit.files.write_text(f"assets/py/v{v}/README.md", "x")
        ids.append(state.add_asset(hid, "lead_directory", "Py", rel, v, 10 * v, 1900))
    state.record_order("stripe", "cs_1", "a@co.example", 1900, None, ids[0], hid, status="delivered")  # v1 was sold
    removed = prune_versions(state, toolkit.files, keep=3)
    assert removed == ["assets/py/py-intel-v2.zip"]
    assert toolkit.files.exists("assets/py/py-intel-v1.zip") and not toolkit.files.exists("assets/py/v2")
    assert all(toolkit.files.exists(f"assets/py/py-intel-v{v}.zip") for v in (3, 4, 5))
    assert len(state.list_assets()) == 5  # history stays


def test_logs_are_pruned_and_the_audit_log_archived(state, toolkit, config, clock):
    state.log_action(1, None, "old", "ok", "x")
    state.log_error("x", "old error")
    clock.advance(days=100)
    state.log_action(2, None, "new", "ok", "y")
    (Path(config.data_dir) / "dispatched_audit.log").write_bytes(b"x" * (5 * 1024 * 1024 + 10))
    res = Housekeeping().run("housekeeping", tctx(toolkit))
    assert res.metrics["actions"] == 1 and res.metrics["errors"] == 0  # errors are kept twice as long
    archives = list(Path(config.data_dir).glob("dispatched_audit-*.log.gz"))
    assert len(archives) == 1 and len(gzip.decompress(archives[0].read_bytes())) > 5 * 1024 * 1024
    assert (Path(config.data_dir) / "dispatched_audit.log").stat().st_size == 0
    assert "housekeeping done" in Housekeeping().run("housekeeping", tctx(toolkit)).summary
    assert rotate_audit(config, clock()) == 0


# ------------------------------------------------------------------ Phase 48: disk guard
def test_low_disk_alerts_daily_and_critical_pauses_builds(state, toolkit, clock, config):
    DiskGuard(usage=lambda p: Usage(500 * GB, 499 * GB, int(1.5 * GB))).run("check_disk", tctx(toolkit))
    assert len(state.recent_errors(5, kind="alert")) == 1 and not builds_paused(state)
    DiskGuard(usage=lambda p: Usage(500 * GB, 499 * GB, int(1.5 * GB))).run("check_disk", tctx(toolkit))
    assert len(state.recent_errors(5, kind="alert")) == 1  # once a day
    res = DiskGuard(usage=lambda p: Usage(500 * GB, 500 * GB, int(0.2 * GB))).run("check_disk", tctx(toolkit))
    assert "critical" in res.summary and builds_paused(state) and state.get("housekeeping", {}).get("at")
    from strategies.digital_asset_packager import DigitalAssetPackager

    state.upsert_lead("k1", "py", {"company": "A", "title": "Dev"})
    state.upsert_lead("k2", "py", {"company": "B", "title": "Dev"})
    hyp = {"id": state.create_hypothesis("lead_directory:py:g1", "lead_directory", "py", {"niche": "py"}), "params": {"niche": "py"},
           "iterations": 0}
    assert DigitalAssetPackager().run("package_asset", TaskContext(toolkit, hyp, {})).metrics.get("paused")
    DiskGuard(usage=lambda p: Usage(500 * GB, 400 * GB, 100 * GB)).run("check_disk", tctx(toolkit))
    assert not builds_paused(state)


# ------------------------------------------------------------------ Phase 49: config check
def test_typos_and_bad_values_are_explained(config, tmp_path):
    toml = tmp_path / "automonetize.toml"
    toml.write_text('[automonetize]\ndaily_target_cent = 2000\nsale_pct = 95\n')
    config.sale_pct, config.owner_email, config.heartbeat_url = 95, "me-at-gmail", "http://hc-ping.com/x"
    found = problems(config, toml)
    assert any("daily_target_cent" in p and "did you mean 'daily_target_cents'" in p for p in found)
    assert any(p.startswith("sale_pct = 95") for p in found)
    assert any("owner_email" in p for p in found) and any("heartbeat_url" in p for p in found)
    config.sale_pct, config.owner_email, config.heartbeat_url = 25, "", ""
    toml.write_text("daily_target_cents = 1000\n")
    assert problems(config, toml) == []


@pytest.mark.parametrize("task", ["housekeeping", "check_disk"])
def test_new_tasks_are_planned_and_handled(config, state, toolkit, task):
    from agent.engine import PLAN, Engine

    engine = Engine(config, state=state, sleep=lambda s: None)
    assert task in dict(PLAN) and task in engine.handlers
    assert engine.breaker.max_actions_per_cycle >= len(PLAN) + 10


def test_stores_close_themselves_when_dropped(tmp_path):
    import gc
    import warnings

    from agent.state import StateStore

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        s = StateStore(tmp_path / "x.db")
        conn = s.conn
        del s
        gc.collect()
    with pytest.raises(Exception):
        conn.execute("SELECT 1")  # closed by the finalizer
    assert timedelta(0) == timedelta(0)
