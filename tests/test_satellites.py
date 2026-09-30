"""Satellite niches: allocation maths, lifecycle, capacity split across syndication and SEO."""


import pytest

from strategies.base import TaskContext
from strategies.satellite_orchestrator import (
    SATELLITE, SatelliteOrchestrator, allocation, choose_by_deficit, niche_of, satellites, shares_for, split_quota,
    working_set,
)
from tests.conftest import NOW


def test_shares_follow_revenue_with_a_floor():
    assert shares_for(["a", "b", "c"], {}, 0.15) == {"a": pytest.approx(1 / 3), "b": pytest.approx(1 / 3), "c": pytest.approx(1 / 3)}
    s = shares_for(["a", "b", "c"], {"a": 9000, "b": 1000}, 0.15)
    assert s == {"a": pytest.approx(0.15 + 0.55 * 0.9), "b": pytest.approx(0.15 + 0.55 * 0.1), "c": 0.15}
    assert sum(s.values()) == pytest.approx(1.0) and s["c"] == 0.15  # no revenue yet, still a chance
    assert shares_for(["a", "b"], {"a": 100}, 0.9) == {"a": 0.5, "b": 0.5}  # the floor can't exceed an equal split
    assert shares_for([], {}, 0.1) == {}


def test_quota_split_and_deficit_order():
    assert split_quota(60, {"a": 0.66, "b": 0.19, "c": 0.15}) == {"a": 40, "b": 11, "c": 9}
    assert sum(split_quota(7, {"a": 1 / 3, "b": 1 / 3, "c": 1 / 3}).values()) == 7
    order = choose_by_deficit({"a": 0.6, "b": 0.3, "c": 0.1}, {"a": 6, "b": 1, "c": 3})
    assert order[0] == "b" and order[-1] == "c"  # b is furthest below its share, c furthest above
    assert choose_by_deficit({"a": 0.6, "b": 0.4}, {})[0] == "a"


@pytest.fixture
def orchestra(config, state, toolkit, make_hypothesis):
    config.max_active_niches = 3
    config.niches = [
        {"name": "python-remote", "keywords": ["python", "django"]},
        {"name": "frontend-react", "keywords": ["react", "typescript"]},
        {"name": "rust-systems", "keywords": ["rust"]},
        {"name": "devops-sre", "keywords": ["kubernetes", "terraform"]},
    ]
    primary = make_hypothesis()
    return toolkit, TaskContext(toolkit, primary, {})


def test_satellites_start_run_the_pipeline_and_respect_the_primary(orchestra, state, config):
    kit, ctx = orchestra
    res = SatelliteOrchestrator().run("run_satellites", ctx)
    sats = satellites(state)
    assert len(sats) == 2 and all(h["status"] == SATELLITE for h in sats)
    niches = {niche_of(h) for h in working_set(state)}
    assert len(niches) == 3 and "python-remote" in niches  # no duplicate of the primary
    assert state.active_hypothesis()["params"]["niche"] == "python-remote"  # the engine's primary is untouched
    assert set(res.metrics["refreshed"]) == {niche_of(h) for h in sats}
    for h in sats:  # each satellite has its own radar and a packaged dataset
        assert kit.files.exists(f"exports/intel/{niche_of(h)}/tech_radar.json") or state.count_leads(niche_of(h)) == 0
        assert state.get(f"satellite_last_run:{h['id']}")
    assert res.metrics["shares"] == {n: pytest.approx(1 / 3) for n in niches}
    again = SatelliteOrchestrator().run("run_satellites", ctx)
    assert again.metrics["refreshed"] == [] and again.metrics["started"] == []  # not due yet, slots full


def test_revenue_moves_capacity_and_refresh_frequency(orchestra, state, config, clock):
    kit, ctx = orchestra
    SatelliteOrchestrator().run("run_satellites", ctx)
    rich, poor = satellites(state)
    state.record_revenue("stripe", "o1", 3000, 100, 2900, True, hypothesis_id=rich["id"], occurred_at=NOW.isoformat())
    alloc = allocation(state, config, clock())
    assert alloc["shares"][niche_of(rich)] > 0.6 and alloc["shares"][niche_of(poor)] == config.satellite_min_share
    assert alloc["revenue_cents"][niche_of(rich)] == 2900
    clock.advance(hours=3)  # the rich niche refreshes every 6h / (0.7 × 3) ≈ 2.9h; the poor one every 13h
    res = SatelliteOrchestrator().run("run_satellites", ctx)
    assert res.metrics["refreshed"] == [niche_of(rich)]
    clock.advance(days=15)  # the sale ages out of the 14-day window: back to equal shares
    assert allocation(state, config, clock())["shares"][niche_of(rich)] == pytest.approx(1 / 3)


def test_satellites_without_revenue_are_replaced(orchestra, state, config, clock):
    kit, ctx = orchestra
    SatelliteOrchestrator().run("run_satellites", ctx)
    first = {niche_of(h) for h in satellites(state)}
    keeper = satellites(state)[0]
    state.upsert_subscriber("stripe", "sub_k", niche=niche_of(keeper), price_cents=1000)  # a subscriber keeps it alive
    clock.advance(days=config.satellite_max_days_without_revenue + 1)
    res = SatelliteOrchestrator().run("run_satellites", ctx)
    assert len(res.metrics["retired"]) == 1 and niche_of(keeper) not in res.metrics["retired"]
    now = {niche_of(h) for h in satellites(state)}
    assert len(now) == 2 and niche_of(keeper) in now and now != first
    retired = [h for h in state.list_hypotheses() if niche_of(h) == res.metrics["retired"][0]][0]
    assert retired["status"] == "deprecated" and "satellite: no verified revenue" in retired["reason"]


def test_budget_exhaustion_stops_satellites_gracefully(orchestra, state, config):
    kit, ctx = orchestra
    kit.breaker.max_api_calls_per_cycle = 0
    kit.breaker.begin_cycle()
    res = SatelliteOrchestrator().run("run_satellites", ctx)
    assert res.ok and res.metrics["refreshed"] == [] and "waiting" in res.summary


def test_disabled_with_one_niche(config, toolkit, make_hypothesis):
    config.max_active_niches = 1
    res = SatelliteOrchestrator().run("run_satellites", TaskContext(toolkit, make_hypothesis(), {}))
    assert "disabled" in res.summary


def test_syndication_and_seo_budget_follow_the_shares(orchestra, state, config, transport):
    from strategies.inbound_syndicator import InboundSyndicator, matrix_pages
    from tools.page_builder import ProductPage

    kit, ctx = orchestra
    SatelliteOrchestrator().run("run_satellites", ctx)
    niches = [niche_of(h) for h in working_set(state)]
    rec = lambda i, stack: {"company": f"C{i}", "stack": stack, "openings": 1, "intent_score": 50, "urgency_score": 50,  # noqa: E731
                            "latest_posted_at": NOW.isoformat()}
    for j, n in enumerate(niches):  # 12 companies each, distinct technologies per niche
        kit.files.write_json(f"exports/intel/{n}/tech_radar.json",
                             [rec(f"{n}{i}", [f"Tech{j}{k}" for k in range(i % 6 + 1)]) for i in range(12)])
    state.set("niche_allocation", {"shares": {niches[0]: 0.7, niches[1]: 0.15, niches[2]: 0.15}})
    config.seo_min_companies, config.seo_max_pages = 2, 10
    pages = [ProductPage(n, n, "s", 900, "usd", f"https://buy.stripe.com/{n}", [], []) for n in niches]
    chosen = matrix_pages(kit, pages)
    by_niche = {n: sum(1 for p in chosen if p.offer.get("niche") == n) for n in niches}
    # quotas 7 / 2 / 1 (largest remainder); the 70% niche only has 6 pages that clear the
    # thin-content threshold, so its unused slot goes to the next strongest pages
    assert len(chosen) == 10 and by_niche[niches[0]] == 6 and by_niche[niches[1]] + by_niche[niches[2]] == 4
    assert all(p.related for p in chosen)  # internal links recomputed for the chosen set
    # syndication: the next article goes to the niche furthest below its share of past articles
    state.set("syndication_niche_counts", {niches[0]: 1, niches[1]: 0, niches[2]: 0})
    res = InboundSyndicator().run("syndicate", ctx)
    assert res.metrics.get("syndicated")
    counts = state.get("syndication_niche_counts")
    expected = sorted(niches[1:])[0]  # tie at 0.15 each: alphabetical
    assert counts[expected] == 1 and counts[niches[0]] == 1
    assert res.metrics["guid"].startswith(f"radar-{expected}-")
