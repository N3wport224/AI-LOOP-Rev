"""Phases 335-339: what the catalog teaches."""

from strategies import factory_insights as fi
from strategies import product_controls as pc
from strategies import product_factory as pf
from strategies.money_insight import record_price_change
from tests import test_business_ops
from tests.test_catalog_hygiene import made_product, sell

kit = test_business_ops.kit  # the same live-mode toolkit fixture


def test_sales_by_age_and_first_sale(kit, state, clock, transport):
    made = made_product(kit, state, clock, transport)
    clock.advance(days=3)
    sell(state, made["asset_id"], 1, prefix="early")
    clock.advance(days=40)
    sell(state, made["asset_id"], 2, prefix="late")
    ages = {b["age"]: b["orders"] for b in fi.by_age(state)}
    assert ages == {"first week": 1, "weeks 2-4": 0, "month 2": 2, "older": 0}
    fs = fi.first_sale(state)
    assert fs["median_days"] == 3.0 and fs["sold"] == 1 and fs["within_14_pct"] == 100


def test_why_products_left(kit, state, config, clock, transport):
    a = made_product(kit, state, clock, transport)
    pc.apply(kit, a["slug"], "retire")
    assert fi.retirements(state) == {"retired by you": 1}


def test_price_effects(kit, state, clock, transport):
    made = made_product(kit, state, clock, transport)
    sell(state, made["asset_id"], 2, prefix="before")
    clock.advance(days=1)
    record_price_change(state, made["asset_id"], 500, 700)
    assert fi.price_effects(state)[0]["after"] is None  # too early
    clock.advance(days=5)
    sell(state, made["asset_id"], 1, prefix="after")
    clock.advance(days=30)
    effect = fi.price_effects(state)[0]
    assert effect["before"] == 2 and effect["after"] == 1


def test_grid_and_lines(kit, state, clock, transport):
    made = made_product(kit, state, clock, transport)
    sell(state, made["asset_id"], 2)
    g = fi.grid(state)
    assert g["techs"] == ["rust"] and g["cents"]["slice"]["rust"] == 1000
    lines = fi.describe(fi.insights(state))
    assert lines[0].startswith("Sales by product age: first week 2") and "slice: Rust $10" in lines


def test_cli_flag():
    from dashboard.cli import build_parser

    assert build_parser().parse_args(["factory", "--insights"]).insights
    assert pf.KIND == "micro"
