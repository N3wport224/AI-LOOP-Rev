"""Phases 145-149: the product factory."""

import io
import itertools
import json
import threading
import zipfile
from datetime import timedelta

from strategies import product_factory as pf
from strategies.b2b_lead_aggregator import POOL_NICHE
from strategies.base import TaskContext
from strategies.inbound_syndicator import site_pages
from tests import test_business_ops
from tests.test_business_ops import STRIPE
from tools.http_client import Response

kit = test_business_ops.kit  # the same live-mode toolkit fixture


def postings(state, clock, n, stack, location="Berlin, Germany", remote=False, seniority="mid", prefix="co", days_ago=3):
    when = (clock() - timedelta(days=days_ago)).isoformat(timespec="seconds")
    for i in range(n):
        state.upsert_lead(f"{prefix}-{stack[0]}-{location}-{seniority}-{i}", POOL_NICHE, {
            "company": f"{prefix.title()} {i % 12}", "title": f"{seniority.title()} Engineer", "location": location,
            "remote": remote, "stack": stack, "seniority": seniority, "url": f"https://jobs.example/{prefix}/{i}",
            "posted_at": when, "source": "remoteok"})


def unique_links(transport):
    ids = itertools.count(1)
    transport.add(f"{STRIPE}/payment_links", lambda m, url, h: Response(
        200, url, json.dumps({"id": f"plink_f{next(ids)}", "url": "https://buy.stripe.com/f"}).encode(), {}))


# ------------------------------------------------------------------ Phase 145: candidates
def test_slices_clear_the_floor_and_rank_by_companies(state, config, clock):
    config.factory_min_rows, config.factory_min_companies = 20, 8
    postings(state, clock, 30, ["rust", "aws"])                       # Berlin: europe
    postings(state, clock, 25, ["rust"], location="Austin, TX, United States", prefix="us")
    postings(state, clock, 10, ["elixir"])                            # too small
    postings(state, clock, 40, ["php"], days_ago=120)                 # too old
    slugs = [c["slug"] for c in pf.candidates(state, config)]
    assert "hiring-rust" in slugs and "hiring-rust-europe" in slugs and "hiring-rust-us" in slugs
    assert not any("elixir" in s or "php" in s for s in slugs)
    assert slugs.index("hiring-rust") < slugs.index("hiring-rust-europe")
    spec = pf.slice_spec("rust", "remote", "senior")
    assert spec["title"] == "Remote Companies Hiring for Senior Rust Engineers"


# ------------------------------------------------------------------ Phases 146-147: build + publish
def test_a_tick_builds_publishes_and_lists_a_product(kit, state, config, clock, transport):
    unique_links(transport)
    postings(state, clock, 30, ["rust", "aws"])
    out = pf.tick(kit, force=True)
    made = out["made"]
    assert out["status"] == "live" and made["title"].startswith("Companies Hiring") and made["price_cents"] == 500
    asset = state.get_asset(made["asset_id"])
    assert asset["kind"] == "micro" and asset["status"] == "published" and asset["product_ref"].startswith("plink_f")
    zf = zipfile.ZipFile(io.BytesIO(kit.files.read_bytes(asset["path"])))
    names = {n.split("/", 1)[1] for n in zf.namelist()}
    assert {"README.md", "leads.csv", "leads-excel.csv", "leads.json", "leads.jsonl", "schema.sql", "QUALITY.md"} <= names
    hyp = state.get_hypothesis(asset["hypothesis_id"])
    assert hyp["status"] == "product" and state.active_hypothesis() is None  # never the engine's main niche
    page = next(p for p in site_pages(kit) if p.kind == "micro")
    assert page.slug == made["slug"] and page.checkout_url == "https://buy.stripe.com/f"


# ------------------------------------------------------------------ Phase 148: cadence
def test_one_product_per_interval(kit, state, config, clock, transport):
    unique_links(transport)
    postings(state, clock, 30, ["rust", "aws"])
    postings(state, clock, 30, ["kotlin"], location="Remote", remote=True, prefix="k")
    assert pf.tick(kit)["made"]
    assert pf.tick(kit)["why"] == "not due yet"
    clock.advance(seconds=config.factory_interval_seconds)
    assert pf.tick(kit)["made"]
    postings(state, clock, 30, ["golang"], location="Austin, TX, United States", prefix="g")
    clock.advance(seconds=3 * config.factory_interval_seconds)
    res = pf.ProductFactory().run("run_factory", TaskContext(kit, {"id": 1, "params": {}, "iterations": 0}, {}))
    assert res.metrics["made"] == 1 and res.metrics["catalog_live"] == 3  # catches up as far as the data allows, no padding


def test_the_worker_ticks_until_stopped(kit, state, config, transport, clock):
    unique_links(transport)
    postings(state, clock, 30, ["rust"])
    stop = threading.Event()
    waits = []

    def wait(seconds):
        waits.append(seconds)
        stop.set()
        return True

    stop.wait = wait
    pf.run_worker(kit, stop)
    assert waits == [600] and pf.catalog(state).get("live") == 1


# ------------------------------------------------------------------ Phase 149: quality floor
def test_near_duplicates_are_skipped_and_the_factory_waits_instead_of_padding(kit, state, config, clock, transport):
    unique_links(transport)
    postings(state, clock, 30, ["rust", "aws"])  # rust and aws slices are the same 30 postings
    assert pf.tick(kit, force=True)["made"]
    out = pf.tick(kit, force=True)
    assert out["made"] is None and "quality floor" in out["why"]
    config.factory_max_live = 1
    postings(state, clock, 30, ["kotlin"], prefix="k")
    assert pf.tick(kit, force=True)["made"] is None  # the catalog is full


def test_unsold_products_retire(kit, state, config, clock, transport):
    unique_links(transport)
    postings(state, clock, 30, ["rust"])
    made = pf.tick(kit, force=True)["made"]
    clock.advance(days=config.factory_retire_days + 1)
    assert pf.retire_unsold(kit) == [made["slug"]]
    assert state.get_asset(made["asset_id"])["status"] == "retired"
    assert not [p for p in site_pages(kit) if p.kind == "micro"]
    assert transport.calls_to(f"{STRIPE}/payment_links/{state.get_asset(made['asset_id'])['product_ref']}", "POST")


def test_without_delivery_products_wait_staged(kit, state, config, clock, transport):
    unique_links(transport)
    config.dry_run = True
    postings(state, clock, 30, ["rust"])
    out = pf.tick(kit, force=True)
    assert out["status"] == "staged" and not [p for p in site_pages(kit) if p.kind == "micro"]
    config.dry_run = False
    assert pf.tick(kit)["published"] == [out["made"]["slug"]]


def test_the_supervisor_runs_a_factory_worker_for_the_real_engine(config, state, toolkit):
    from agent.engine import Engine
    from agent.supervisor import Supervisor

    full = Supervisor(config, engine=Engine(config, state=state, toolkit=toolkit, online_check=lambda: True), webhook=False)
    assert "factory" in [w.name for w in full.workers]
    custom = Supervisor(config, engine=Engine(config, state=state, strategies=[], toolkit=toolkit, online_check=lambda: True),
                        webhook=False)
    assert "factory" not in [w.name for w in custom.workers]
    config.product_factory = False
    off = Supervisor(config, engine=Engine(config, state=state, toolkit=toolkit, online_check=lambda: True), webhook=False)
    assert "factory" not in [w.name for w in off.workers]
