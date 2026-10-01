"""Phases 235-239: results by product type, AM CATALOG, catalog CSV, weekly line, choosing product types."""

import csv
import io

from agent.owner_commands import COMMANDS, execute, help_text
from strategies import catalog_insight as ci
from strategies import product_factory as pf
from strategies import product_types as pt
from tests import test_business_ops, test_gui
from tests.test_product_factory import postings, salaried, unique_links

kit = test_business_ops.kit  # the same live-mode toolkit fixture
gui = test_gui.gui  # the control-panel fixture


def two_products(kit, state, clock, transport):
    unique_links(transport)
    salaried(state, clock, 90, ["rust"], "r")
    a = pf.tick(kit, force=True)["made"]   # slice
    b = pf.tick(kit, force=True)["made"]   # salary
    state.record_order("stripe", "cs_1", "a@co.example", 700, None, b["asset_id"], None, status="delivered")
    return a, b


# ------------------------------------------------------------------ Phase 235
def test_results_by_product_type(kit, state, clock, transport):
    a, b = two_products(kit, state, clock, transport)
    types = ci.by_type(state)
    assert [t["type"] for t in types] == ["salary", "slice"]
    assert types[0] == {"type": "salary", "name": "Salary benchmarks", "live": 1, "retired": 0, "orders": 1,
                        "revenue_cents": 700, "per_product_cents": 700}
    assert ci.describe_types(state)[0].startswith("Salary benchmarks: 1 on sale, 0 retired; 1 order(s), $7.00 in 90 days")


def test_catalog_csv(kit, state, clock, transport):
    a, b = two_products(kit, state, clock, transport)
    rows = list(csv.DictReader(io.StringIO(ci.catalog_csv(state))))
    assert [r["slug"] for r in rows] == [a["slug"], b["slug"]] and rows[1]["orders_90d"] == "1"
    assert list(rows[0]) == ci.CSV_FIELDS and rows[0]["technology"] == "rust"


def test_cli_flags_are_wired():
    from dashboard.cli import build_parser

    args = build_parser().parse_args(["factory", "--types", "--csv"])
    assert args.types and args.csv and not args.now


# ------------------------------------------------------------------ Phase 236
def test_catalog_by_email(kit, state, config, clock, transport):
    assert "catalog" in COMMANDS and "CATALOG" in help_text(config, state)
    two_products(kit, state, clock, transport)
    reply = execute(kit, "catalog")
    assert reply.startswith("Catalog: 2 on sale") and "- Salary benchmarks: 1 on sale" in reply
    assert "Best sellers:\n- " in reply and "Next: " in reply


# ------------------------------------------------------------------ Phase 237
def test_panel_downloads_the_catalog(gui, state, clock):
    postings(state, clock, 5, ["rust"])

    async def scenario(client):
        r = await client.get("/api/products.csv")
        assert r.status in (302, 401, 403)  # signed out: no catalog
        await test_gui.login(client)
        r = await client.get("/api/products.csv")
        assert r.status == 200 and r.headers["Content-Type"].startswith("text/csv")
        assert (await r.text()).startswith("slug,title,type") and "attachment" in r.headers["Content-Disposition"]
        html = await (await client.get("/")).text()
        assert 'href="/api/products.csv"' in html

    test_gui.run(gui, scenario)


# ------------------------------------------------------------------ Phase 238
def test_weekly_line(kit, state, clock, transport):
    assert ci.weekly_line(state) == ""
    two_products(kit, state, clock, transport)
    line = ci.weekly_line(state)
    assert line.startswith("Catalog this week: 2 new, 0 retired, 0 price change(s).")
    assert "Earning most per product: Salary benchmarks ($7.00)" in line


# ------------------------------------------------------------------ Phase 239
def test_product_types_can_be_switched_off(kit, state, config, clock, transport):
    assert ci.enabled_types(config) == list(pt.TYPES)
    config.factory_types = ["salary", "nonsense"]
    assert ci.enabled_types(config) == ["salary"]
    unique_links(transport)
    salaried(state, clock, 90, ["rust"], "r")
    made = [pf.tick(kit, force=True)["made"] for _ in range(2)]
    assert made[0]["slug"] == "salary-rust" and made[1] is None  # only salary products, and there's one
