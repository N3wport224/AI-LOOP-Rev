"""Phases 190-194: product funnel, title tests, paused strategies, return on time, marketing report."""

from cli import growth
from strategies import marketing_engine as me
from strategies import marketing_optimizer as mo
from strategies import product_factory as pf
from strategies.inbound_syndicator import site_pages
from strategies.owner_todo import todo
from tests import test_business_ops
from tests.test_product_factory import postings, unique_links

kit = test_business_ops.kit  # the same live-mode toolkit fixture


def product(kit, state, clock, transport):
    unique_links(transport)
    postings(state, clock, 30, ["rust"], location="Berlin, Germany")
    made = pf.tick(kit, force=True)["made"]
    return made, state.get_asset(made["asset_id"])["product_ref"]


def checkouts(state, ref, n, start=0):
    for i in range(start, start + n):
        state.upsert_checkout_session(f"cs_{ref}_{i}", ref, None, "expired", "unpaid", None, 900)


# ------------------------------------------------------------------ Phase 190
def test_a_product_with_checkouts_but_no_sales_is_flagged(kit, state, config, clock, transport):
    made, ref = product(kit, state, clock, transport)
    checkouts(state, ref, 6)
    row = mo.funnel(state)[0]
    assert (row["checkouts"], row["orders"], row["leaky"]) == (6, 0, True)
    assert any(i["title"] == f"Look at {made['title']}" for i in todo(state, config))
    state.record_order("stripe", "cs_ok", "a@co.example", 500, ref, made["asset_id"], None, status="delivered")
    assert not mo.leaky(state) and mo.funnel(state)[0]["conversion"] == round(1 / 6, 3)


# ------------------------------------------------------------------ Phase 191
def test_title_tests_run_only_with_traffic_and_keep_the_winner(kit, state, config, clock, transport):
    made, ref = product(kit, state, clock, transport)
    checkouts(state, ref, 3)
    assert mo.step_title_tests(state) == {}  # too little traffic: no test
    checkouts(state, ref, 8, start=3)
    assert mo.step_title_tests(state) == {made["slug"]: "testing the variant title"}
    variant = mo.variant_title(state, made["slug"], made["title"])
    assert variant == "Rust Jobs: 12 Companies Hiring Now"
    assert next(p for p in site_pages(kit) if p.kind == "micro").title == variant
    clock.advance(days=7, hours=1)
    checkouts(state, ref, 15, start=20)  # the variant week does better
    change = mo.step_title_tests(state)[made["slug"]]
    assert change.startswith("kept the variant title")
    assert next(p for p in site_pages(kit) if p.kind == "micro").title == variant
    assert mo.step_title_tests(state) == {}  # done


# ------------------------------------------------------------------ Phases 192-194
def test_return_on_time_paused_strategies_and_the_report(kit, state, config, clock, transport, monkeypatch, capsys):
    from tools.attribution import decode_ref, encode_ref

    made, ref = product(kit, state, clock, transport)
    pid = me.add_play(state, "linkedin_post", "draft", "posted")
    channel, campaign = decode_ref(encode_ref("linkedin", f"mkt{pid}"))
    state.record_order("stripe", "cs_li", "a@co.example", 900, ref, made["asset_id"], None, status="delivered",
                       channel=channel, campaign=campaign)
    roi = {r["name"]: r for r in mo.return_on_time(state)}
    assert roi["LinkedIn post"]["minutes"] == 3 and roi["LinkedIn post"]["per_minute_cents"] == 300
    for _ in range(me.PAUSE_AFTER_PLAYS):
        me.add_play(state, "reddit_data_post", "draft", "posted")
    me.pause_losers(state)
    lines = mo.describe(state)
    assert any(line.startswith("Best use of your time: LinkedIn post ($3.00 per minute") for line in lines)
    assert any("Resting" in line and "Reddit data post" in line for line in lines)
    monkeypatch.setattr(growth, "_setup", lambda: (config, state, None))
    growth.marketing_main(["report"])
    out = capsys.readouterr().out
    assert "Product funnel (30 days):" in out and "Return on your time" in out and "LinkedIn post" in out
