"""Phases 370-374: a product people return stops being promoted."""

from strategies import product_controls as pc
from strategies import refund_guard as rg
from strategies.freshness_guard import promotable
from strategies.marketing_engine import featured
from tests import test_business_ops
from tests.test_catalog_hygiene import made_product
from tests.test_ops_robustness import alerts, run_ops

kit = test_business_ops.kit  # the same live-mode toolkit fixture


def orders(state, aid, ok, bad, prefix="o"):
    for i in range(ok + bad):
        state.record_order("stripe", f"{prefix}{i}", f"b{i}@co.example", 500, None, aid, None,
                           status="refunded" if i < bad else "delivered")


def test_stats_hold_and_release(kit, state, clock, transport):
    made = made_product(kit, state, clock, transport)
    orders(state, made["asset_id"], 7, 2)
    assert rg.stats(state)[made["slug"]] == {"orders": 9, "returns": 2, "rate": 0.222}
    assert rg.evaluate(state)["held"] == []  # 2 returns: not enough to judge
    orders(state, made["asset_id"], 0, 1, prefix="more")
    assert rg.evaluate(state)["held"] == [made["slug"]]
    assert any("stopped promoting it; it's still on sale" in a for a in alerts(state))
    assert made["slug"] not in {(state.get_asset(p["id"]) or {}).get("niche") for p in promotable(state)}
    assert all(a.get("niche") != made["slug"] for a in featured(state, kit.config))
    assert rg.evaluate(state)["held"] == []  # alerted once
    orders(state, made["asset_id"], 30, 0, prefix="good")
    assert rg.evaluate(state)["released"] == [made["slug"]]


def test_release_by_hand_and_report(kit, state, clock, transport):
    made = made_product(kit, state, clock, transport)
    orders(state, made["asset_id"], 0, 3)
    rg.evaluate(state)
    assert rg.weekly_line(state).startswith("Not promoted because buyers returned them:")
    assert pc.apply(kit, made["slug"], "release").endswith("is promoted again.")
    assert pc.apply(kit, made["slug"], "release").endswith("wasn't held back.")


def test_runs_in_ops_checks(kit, state, clock, transport, toolkit):
    made = made_product(kit, state, clock, transport)
    orders(state, made["asset_id"], 0, 3)
    result = run_ops(kit)
    assert "held back: buyers returned them" in result.summary
