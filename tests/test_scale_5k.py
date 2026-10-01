"""Phases 365-369: one pool read, remembered facets, 5,000 products, timed runs, the bench."""

import json
import time
from datetime import timedelta

from agent import scale_checks as sc
from strategies import catalog_insight, factory_insights, growth_plan, pool_cache
from strategies import product_factory as pf
from tests.test_product_factory import postings


def test_the_pool_is_read_once_until_it_changes(state, clock, monkeypatch):
    postings(state, clock, 5, ["rust"])
    calls = []
    real = state.leads_for_niche
    monkeypatch.setattr(state, "leads_for_niche", lambda niche: calls.append(niche) or real(niche))
    pool_cache.clear()
    first = pool_cache.pool(state)
    assert pool_cache.pool(state) == first and len(calls) == 1
    postings(state, clock, 1, ["golang"], prefix="new")
    assert len(pool_cache.pool(state)) == 6 and len(calls) == 2  # a change is noticed
    state._exec("UPDATE leads SET data = json_set(data, '$.title', 'Staff Rust Engineer') WHERE id = 1")
    assert any(r["title"] == "Staff Rust Engineer" for r in pool_cache.pool(state))  # edits too


def test_facets_are_remembered():
    a = pf.facets({"stack": ["Rust", "AWS"], "location": "Berlin, Germany", "seniority": "senior"})
    b = pf.facets({"stack": ["Rust", "AWS"], "location": "Berlin, Germany", "seniority": "senior"})
    assert a is b and a["techs"] == {"rust", "aws"} and "europe" in a["regions"]


def test_five_thousand_products_stay_fast(state, config, clock):
    pf.ensure(state)
    old = (clock() - timedelta(days=90)).isoformat(timespec="seconds")
    techs = ["rust", "golang", "python", "java", "kotlin", "react", "aws", "postgres"]
    for i in range(5000):
        slug = f"p-{i}"
        aid = state.add_asset(None, pf.KIND, slug, f"assets/{slug}/{slug}-v1.zip", 1, 50, 500, product_ref=f"plink_{i}")
        state.update_asset(aid, niche=slug, status="published", checkout_url=f"https://buy.stripe.com/{i}")
        state._exec("INSERT INTO factory_products (slug, asset_id, title, filters, rows, companies, keys_sample, status, "
                    "created_at, published_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (slug, aid, slug, json.dumps({"tech": techs[i % 8], "type": "slice"}), 50, 10, "[]", "live", old, old))
        if i % 10 == 0:
            state.record_order("stripe", f"cs_{i}", "b@co.example", 500, f"plink_{i}", aid, None, status="delivered")
    from gui.routes.business import catalog_list

    class G:
        pass

    g = G()
    g.state = state
    started = time.monotonic()
    assert len(catalog_insight.rows(state)) == 5000
    assert catalog_insight.by_type(state)[0]["live"] == 5000
    assert len(catalog_list(g, "rust")["items"]) == 200
    factory_insights.insights(state)
    growth_plan.plan(state, config)
    took = time.monotonic() - started
    assert took < 10, f"catalog views took {took:.1f}s at 5,000 products"


def test_factory_runs_are_timed_and_slow_ones_flagged(state, config):
    config.product_factory = True
    for took in (1.0, 2.5):
        sc.record_factory_tick(state, True, "made one", took)
    assert state.get(sc.FACTORY_KEY)["took"] == [1.0, 2.5]
    assert sc.factory_health(state, config)["ok"]
    sc.record_factory_tick(state, True, "made one", sc.SLOW_TICK_SECONDS + 5)
    h = sc.factory_health(state, config)
    assert not h["ok"] and "factory runs are slow" in h["detail"] and "automonetize bench" in h["fix"]


def test_the_bench_runs_in_a_sandbox():
    from cli.bench import run

    r = run(products=3, postings=300)
    assert r["products"] >= 1 and r["max_tick_s"] < 30 and r["postings"] == 300
