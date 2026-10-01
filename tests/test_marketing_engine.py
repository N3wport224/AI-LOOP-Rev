"""Phases 170-174: marketing playbook, scheduler, scoring, calendar, report line."""

import random

import pytest

from cli import growth
from strategies import marketing_engine as me
from strategies import product_factory as pf
from strategies.base import TaskContext
from strategies.owner_reports import OwnerReports
from tests import test_business_ops
from tests.test_product_factory import postings, unique_links
from tools.attribution import decode_ref, encode_ref, parse_utm

kit = test_business_ops.kit  # the same live-mode toolkit fixture


@pytest.fixture
def catalog(kit, state, clock, transport, config):
    config.pages_base_url = "https://me.github.io/site"
    unique_links(transport)
    postings(state, clock, 30, ["rust"])
    return pf.tick(kit, force=True)["made"]


def run(kit):
    return me.MarketingEngine().run("plan_marketing", TaskContext(kit, {"id": 1, "params": {}}, {}))


# ------------------------------------------------------------------ Phases 170-171
def test_drafts_are_queued_with_tracked_links_and_capped(kit, state, config, catalog):
    res = run(kit)
    drafts = me.queue(state)
    assert res.metrics["drafted"] == len(drafts) == config.marketing_drafts_per_day
    for d in drafts:
        utm = parse_utm(d["link"])
        assert utm["source"] == me.PLAYBOOK[d["strategy"]]["channel"] and utm["campaign"] == f"mkt{d['id']}"
        assert d["link"].startswith(f"https://me.github.io/site/{catalog['slug']}/") and catalog["title"] in d["body"]
    assert run(kit).summary.startswith("marketing planned")  # once a day
    assert me.mark(state, drafts[0]["id"], "posted") and not me.mark(state, drafts[0]["id"], "posted")


def test_nothing_to_promote_means_no_drafts(kit, state):
    assert run(kit).metrics["drafted"] == 0 and me.queue(state) == []


def test_auto_strategies_registered_by_later_phases_run_when_due(kit, state, monkeypatch, catalog):
    calls = []
    monkeypatch.setitem(me.PLAYBOOK, "test_auto", {"name": "Test", "channel": "test", "mode": "auto", "every_days": 2,
                                                   "make": lambda tools, pid: calls.append(pid) or {"title": "did it", "body": "did it", "link": ""}})
    me.plan(kit)
    me.plan(kit)
    assert len(calls) == 1  # every 2 days
    assert state._one("SELECT status FROM marketing_plays WHERE strategy = 'test_auto'")["status"] == "done"


# ------------------------------------------------------------------ Phases 172 + 192
def test_scores_follow_sales_and_losers_rest(kit, state, clock, catalog):
    me.plan(kit, random.Random(1))
    linkedin = me.add_play(state, "linkedin_post", "draft", "posted")
    channel, campaign = decode_ref(encode_ref("linkedin", f"mkt{linkedin}"))
    state.record_order("stripe", "cs_li", "a@co.example", 900, None, catalog["asset_id"], None, status="delivered",
                       channel=channel, campaign=campaign)
    board = me.scores(state)
    assert board["linkedin_post"]["orders"] == 1 and board["linkedin_post"]["revenue_cents"] == 900
    for _ in range(me.PAUSE_AFTER_PLAYS):
        me.add_play(state, "x_post", "draft", "posted")
    assert "x_post" in me.pause_losers(state) and not me.due(state, "x_post")
    assert "linkedin_post" not in me.paused(state)
    clock.advance(days=me.PAUSE_DAYS + 1)
    assert me.due(state, "x_post")


# ------------------------------------------------------------------ Phases 173-174
def test_calendar_and_report_lines(kit, state, config, clock, catalog, monkeypatch, capsys):
    week = me.calendar(state)
    days = {d for d, _ in week}
    assert any("X post (you post)" == name for _, name in week) and len(days) == 7
    run(kit)
    line = me.describe(state)
    assert "draft(s) waiting for you" in line
    monday = OwnerReports.digest_body(kit, clock().replace(day=28))
    assert "Marketing:" in monday
    monkeypatch.setattr(growth, "_setup", lambda: (config, state, None))
    growth.marketing_main([])
    out = capsys.readouterr().out
    assert "automonetize marketing done" in out
    growth.marketing_main(["scores"])
    assert "Reddit data post" in capsys.readouterr().out
