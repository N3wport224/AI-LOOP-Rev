"""Phases 355-359: adaptive pace, never slower than asked."""

from strategies import factory_cadence as fc
from strategies import product_factory as pf
from tests import test_business_ops
from tests.test_catalog_hygiene import made_product, sell
from tests.test_product_factory import postings

kit = test_business_ops.kit  # the same live-mode toolkit fixture


def test_never_slower_than_asked(state, config):
    config.factory_interval_seconds = 600
    assert fc.effective(state, config)["seconds"] == 600
    fc.note_queue(state, 1000)
    assert fc.effective(state, config)["seconds"] == 600  # nothing selling: no reason to hurry
    config.factory_adaptive = False
    assert fc.effective(state, config) == {"seconds": 600, "why": "fixed pace (factory_adaptive is off)"}


def test_faster_when_it_pays(kit, state, config, clock, transport):
    made = made_product(kit, state, clock, transport)
    sell(state, made["asset_id"], 1)
    fc.note_queue(state, 25)
    e = fc.effective(state, config)
    assert e["seconds"] == 300 and "25 good products waiting" in e["why"]
    config.factory_interval_seconds = 400
    assert fc.effective(state, config)["seconds"] == 300  # floor of 5 minutes
    config.factory_max_live = 1
    assert fc.effective(state, config)["seconds"] == 400 and "near its cap" in fc.effective(state, config)["why"]
    config.factory_interval_seconds = 120
    assert fc.effective(state, config)["seconds"] == 120  # never slower than asked, even below the floor


def test_the_queue_is_noted_and_used_by_the_tick(kit, state, config, clock, transport):
    made = made_product(kit, state, clock, transport)
    assert (state.get(fc.QUEUE) or {}).get("waiting") is not None
    sell(state, made["asset_id"], 1)
    fc.note_queue(state, 50)
    postings(state, clock, 25, ["rust"], location="Austin, TX, United States", prefix="us")
    clock.advance(seconds=350)  # past the fast pace (300 s), before the configured one (600 s)
    assert pf.tick(kit)["made"] is not None  # made on the faster pace


def test_doctor_and_panel_show_the_pace(config, state):
    from cli.doctor import Doctor
    from tests.test_autopilot import Ctl, runner

    findings = {f.name: f for f in Doctor(config, state, Ctl(pid=1), run=runner({}), system="Linux").checks(deep=False)}
    assert findings["Factory pace"].detail.startswith("one product every 10 min: your pace")
