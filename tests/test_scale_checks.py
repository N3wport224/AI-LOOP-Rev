"""Phases 215-219: factory health, indexes, database growth, slow tasks, and a big-catalog check."""

import json
import threading
import time
from datetime import timedelta

from agent import scale_checks as sc
from cli.doctor import Doctor
from strategies import catalog_hygiene as ch
from strategies import product_factory as pf
from tests.test_autopilot import Ctl, runner
from tests.test_ops_robustness import alerts, run_ops


# ------------------------------------------------------------------ Phase 215
def test_factory_health(state, config, clock):
    config.product_factory = False
    assert sc.factory_health(state, config) is None
    config.product_factory, config.factory_interval_seconds = True, 600
    assert sc.factory_health(state, config)["ok"]  # nothing recorded yet: nothing to say
    sc.record_factory_tick(state, True, "made one")
    state.set(pf.MADE, state.now())
    assert sc.factory_health(state, config)["ok"]
    clock.advance(minutes=31)
    stalled = sc.factory_health(state, config)
    assert not stalled["ok"] and "hasn't run since" in stalled["detail"]
    sc.record_factory_tick(state, False, "RuntimeError('boom')")
    assert "last factory run failed: RuntimeError" in sc.factory_health(state, config)["detail"]
    clock.advance(hours=sc.NO_PRODUCT_HOURS)
    sc.record_factory_tick(state, True, "no new slice clears the quality floor yet")
    idle = sc.factory_health(state, config)
    assert not idle["ok"] and "no new product since" in idle["detail"] and "quality floor" in idle["detail"]


def test_the_worker_records_its_ticks(toolkit, state, config):
    config.product_factory, config.factory_interval_seconds = True, 60
    stop = threading.Event()
    t = threading.Thread(target=pf.run_worker, args=(toolkit, stop))
    t.start()
    for _ in range(200):
        if state.get(sc.FACTORY_KEY):
            break
        time.sleep(0.01)
    stop.set()
    t.join(5)
    assert state.get(sc.FACTORY_KEY)["ok"] is True


# ------------------------------------------------------------------ Phase 216
def test_hot_queries_use_indexes(state):
    pf.ensure(state)
    plans = {
        "orders by asset": "SELECT COUNT(*) FROM orders WHERE asset_id = 1",
        "assets by niche": "SELECT id FROM assets WHERE niche = 'x'",
        "factory by status": "SELECT slug FROM factory_products WHERE status = 'live' AND published_at < '2026'",
        "recent actions": "SELECT name FROM actions WHERE created_at >= '2026'",
    }
    for what, sql in plans.items():
        plan = " ".join(str(r["detail"]) for r in state._all("EXPLAIN QUERY PLAN " + sql))
        assert "USING" in plan and "INDEX" in plan, (what, plan)


# ------------------------------------------------------------------ Phase 217
def test_database_growth_is_sampled_daily_and_flagged(state, clock):
    assert sc.sample_db(state, 50_000_000) == {"mb": 50.0, "per_day_mb": 0.0, "warn": ""}
    clock.advance(hours=2)
    sc.sample_db(state, 900_000_000)
    assert len(state.get(sc.GROWTH_KEY)) == 1  # once a day
    clock.advance(hours=22)
    grown = sc.sample_db(state, 450_000_000)
    assert grown["per_day_mb"] == 400.0 and "grows 400 MB a day" in grown["warn"]
    clock.advance(days=8)
    big = sc.sample_db(state, 2_100_000_000)
    assert big["per_day_mb"] == 227.8 and big["warn"] == "the database grows 228 MB a day (2,100 MB now)"
    assert len(state.get(sc.GROWTH_KEY)) == 3


# ------------------------------------------------------------------ Phase 218
def test_slow_tasks_need_three_slow_runs_in_a_row(state):
    for secs in (400, 20, 400, 400):  # oldest first
        state.log_action(1, None, "build_site", "ok", "", secs)
    assert sc.slow_tasks(state) == {}
    state.log_action(1, None, "build_site", "ok", "", 650)
    assert sc.slow_tasks(state) == {"build_site": 650.0}


def test_ops_checks_alert_once_and_doctor_shows_it(toolkit, state, config, clock):
    config.product_factory = True
    for _ in range(3):
        state.log_action(1, None, "build_site", "ok", "", 700)
    sc.record_factory_tick(state, True, "made one")
    clock.advance(hours=2)
    result = run_ops(toolkit)
    assert "build_site slow (12 min)" in result.summary and "factory: the factory hasn't run" in result.summary
    run_ops(toolkit)
    found = alerts(state)
    assert sum("Task build_site took over 5 minutes" in a for a in found) == 1
    assert sum(a.startswith("Product factory: the factory hasn't run") for a in found) == 1
    doc = Doctor(config, state, Ctl(pid=1), run=runner({}), system="Linux")
    findings = {f.name: f for f in doc.checks(deep=False)}
    assert findings["Product factory"].status == "warn" and findings["Task build_site"].status == "warn"
    assert findings["Database size"].status == "ok" and "MB" in findings["Database size"].detail


# ------------------------------------------------------------------ Phase 219
def test_a_big_catalog_stays_fast(state, config, clock, toolkit):
    pf.ensure(state)
    old = (clock() - timedelta(days=90)).isoformat(timespec="seconds")
    for i in range(1500):
        slug = f"hiring-t{i}"
        aid = state.add_asset(None, pf.KIND, slug, f"assets/{slug}/v1.zip", 1, 50, 500, product_ref=f"plink_{i}")
        state.update_asset(aid, niche=slug, status="published")
        status = "retired" if i % 3 == 0 else "live"
        state._exec("INSERT INTO factory_products (slug, asset_id, title, filters, rows, companies, keys_sample, status, "
                    "created_at, published_at, retired_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (slug, aid, slug, json.dumps({"tech": f"t{i}"}), 50, 10, "[]", status, old, old,
                     old if status == "retired" else None))
        for j in range(3):
            state.record_order("stripe", f"cs_{i}_{j}", f"b{j}@co.example", 500, f"plink_{i}", aid, None, status="delivered")
    started = time.monotonic()
    assert sum(pf.catalog(state).values()) == 1500
    assert ch.next_price(state, "hiring-t7", 500) == 700
    assert ch.prune_retired(state, toolkit.files) == 0  # all sold: kept
    assert pf.next_candidate(state, config) is None  # no postings: nothing to make
    clock.advance(days=1)
    assert pf.retire_unsold(toolkit) == []  # all sold
    sc.slow_tasks(state)
    took = time.monotonic() - started
    assert took < 3.0, f"hot paths took {took:.2f}s on 1500 products"
