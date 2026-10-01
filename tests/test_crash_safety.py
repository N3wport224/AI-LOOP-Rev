"""Phases 395-399: leftover temp files, one factory run at a time, half-made products."""

import os
import threading
import time
from pathlib import Path

from strategies import crash_safety as cs
from strategies import product_factory as pf
from tests import test_business_ops
from tests.test_product_factory import postings, unique_links

kit = test_business_ops.kit  # the same live-mode toolkit fixture


def test_leftover_temp_files_are_swept(tmp_path):
    old, new = tmp_path / "a" / ".tmp-old", tmp_path / ".tmp-new"
    old.parent.mkdir()
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    stamp = time.time() - (cs.TMP_MAX_AGE_HOURS + 1) * 3600
    os.utime(old, (stamp, stamp))
    assert cs.sweep_tmp(tmp_path) == 1 and not old.exists() and new.exists()


def test_the_lease(state):
    assert cs.claim(state, "factory", "a", now=1000)
    assert not cs.claim(state, "factory", "b", now=1001)          # held
    assert cs.claim(state, "factory", "a", now=1002)              # ours: renewed
    assert cs.claim(state, "factory", "b", now=1002 + cs.LEASE_SECONDS + 1)  # expired: taken over
    cs.release(state, "factory", "a")                             # not ours any more: nothing happens
    assert not cs.claim(state, "factory", "c", now=1003 + cs.LEASE_SECONDS)


def test_two_factory_runs_never_overlap(kit, state, clock, transport, monkeypatch):
    unique_links(transport)
    postings(state, clock, 30, ["rust"])
    inside, overlaps, results = [], [], []
    real = pf._tick

    def slow_tick(tools, force=False):
        if inside:
            overlaps.append(True)
        inside.append(1)
        time.sleep(0.2)
        try:
            return real(tools, force)
        finally:
            inside.pop()

    monkeypatch.setattr(pf, "_tick", slow_tick)
    threads = [threading.Thread(target=lambda: results.append(pf.tick(kit, force=True))) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not overlaps
    assert sum(1 for r in results if r.get("why") == "another factory run is in progress") >= 1
    assert state._one("SELECT COUNT(*) AS n FROM factory_products")["n"] == 1


def test_half_made_products_are_cleaned_up(kit, state, clock, transport):
    unique_links(transport)
    postings(state, clock, 30, ["rust"])
    made = pf.tick(kit, force=True)["made"]
    kit.files.write_bytes("assets/ghost-product/ghost-v1.zip", b"x")  # a build interrupted before it was recorded
    stamp = time.time() - (cs.ORPHAN_HOURS + 1) * 3600
    os.utime(kit.files.resolve("assets/ghost-product"), (stamp, stamp))
    state._exec("UPDATE factory_products SET status = 'staged' WHERE slug = ?", (made["slug"],))
    Path(kit.files.resolve(state.get_asset(made["asset_id"])["path"])).unlink()
    out = cs.recover(state, kit.files)
    assert out == {"half-made product folders": 1, "waiting products without a download": 1}
    assert not kit.files.exists("assets/ghost-product")
    assert state._one("SELECT COUNT(*) AS n FROM factory_products")["n"] == 0
    assert kit.files.exists(f"assets/{made['slug']}")  # a known product's folder stays
