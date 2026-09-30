import io
import json
import zipfile
from datetime import timedelta

import pytest

from agent.pricing_engine import PricingEngine, decide, neighbour, revenue_per_view
from strategies.b2b_lead_aggregator import fetch_arbeitnow
from tests.conftest import NOW
from tests.test_distribution import pipeline
from tools.http_client import Response

MATRIX = [500, 900, 1400, 1900]


def exp(price, hours_old, eid=99, status="running"):
    return {"id": eid, "price_cents": price, "status": status, "started_at": (NOW - timedelta(hours=hours_old)).isoformat()}


def d(e, views, orders, history=(), can_bundle=True):
    return decide(e, {"views": views, "orders": orders, "initiations": orders}, list(history), NOW, MATRIX, 20, 48, can_bundle)


# ------------------------------------------------------------------ decision rules
def test_neighbours():
    assert neighbour(MATRIX, 900, -1) == 500 and neighbour(MATRIX, 900, +1) == 1400
    assert neighbour(MATRIX, 500, -1) is None and neighbour(MATRIX, 1900, +1) is None


def test_lower_only_after_window_and_enough_views():
    assert d(exp(1400, 10), 50, 0).action == "wait"
    assert d(exp(1400, 49), 20, 0).action == "hold"  # needs > 20 views
    r = d(exp(1400, 49), 21, 0)
    assert (r.action, r.price_cents) == ("lower", 900)


def test_bundle_at_floor():
    assert d(exp(500, 49), 30, 0).action == "bundle"
    assert d(exp(500, 49), 30, 0, can_bundle=False).action == "hold"


def test_raise_on_strong_demand_even_early():
    r = d(exp(900, 5), 10, 2)
    assert (r.action, r.price_cents) == ("raise", 1400)
    assert d(exp(1900, 5), 10, 3).action == "converge"


def test_converge_when_higher_tier_already_did_worse():
    worse_up = [{"id": 1, "price_cents": 1400, "orders": 0, "views": 60}]
    r = d(exp(900, 5), 30, 2, worse_up)
    assert r.action == "converge" and "already tested" in r.reason
    worse_down = [{"id": 2, "price_cents": 500, "orders": 0, "views": 40}]
    assert d(exp(900, 49), 30, 1, worse_up + worse_down).action == "converge"
    r = d(exp(900, 49), 30, 1)  # weak conversion, cheaper tier untested: explore it
    assert (r.action, r.price_cents) == ("lower", 500)
    better_down = [{"id": 3, "price_cents": 500, "orders": 6, "views": 30}]
    r = d(exp(900, 49), 30, 1, better_down)
    assert (r.action, r.price_cents) == ("lower", 500) and "earned more" in r.reason
    assert d(exp(900, 49), 15, 1).action == "hold"  # not enough views to judge
    assert d(exp(900, 49, status="converged"), 100, 0).action == "hold"


def test_revenue_per_view_smoothing():
    assert revenue_per_view(1900, 1, 10) < revenue_per_view(900, 5, 30)
    assert revenue_per_view(900, 0, 0) > 0


@pytest.mark.parametrize("true_rates,best", [
    ({500: 0.12, 900: 0.10, 1400: 0.03, 1900: 0.01}, 900),   # demand falls off hard above $9
    ({500: 0.10, 900: 0.09, 1400: 0.08, 1900: 0.075}, 1900),  # inelastic: highest price wins
    ({500: 0.08, 900: 0.0, 1400: 0.0, 1900: 0.0}, 500),       # only the floor sells
])
def test_simulated_convergence(true_rates, best):
    """Drive decide() with a deterministic demand curve; it must settle on the best revenue-per-view tier."""
    price, history = 1400, []
    for step in range(40):
        views = 30
        orders = int(round(true_rates[price] * views))
        e = {"id": step, "price_cents": price, "status": "running", "started_at": (NOW - timedelta(hours=49)).isoformat()}
        r = decide(e, {"views": views, "orders": orders}, history, NOW, MATRIX, 20, 48, can_bundle=False)
        history.append({"id": step, "price_cents": price, "orders": orders, "views": views})
        if r.action == "converge" or (r.action == "hold" and price == 500 and orders == 0):
            break
        if r.action in ("lower", "raise"):
            price = r.price_cents
        elif r.action == "hold":
            # more data at the same price
            continue
    assert price == best, history


# ------------------------------------------------------------------ engine against fake Stripe
STRIPE = "https://api.stripe.com/v1"


def test_high_views_no_sales_lowers_price(live, state, clock, transport):
    kit, hyp, asset = live
    eng = PricingEngine(kit)
    eng.run(hyp)  # starts the experiment at $9
    exp1 = state.running_experiment(asset["id"])
    assert exp1["price_cents"] == 900 and exp1["product_ref"] == "plink_0"
    state.set_metric(hyp["id"], "views", "github_traffic", 25)
    clock.advance(hours=49)
    report = eng.run(hyp)
    assert "lower" in report["decisions"][0] and "$5.00" in report["decisions"][0]
    a = state.get_asset(asset["id"])
    assert (a["price_cents"], a["product_ref"], a["checkout_url"]) == (500, "plink_1", "https://buy.stripe.com/l1")
    assert state.running_experiment(asset["id"])["price_cents"] == 500
    assert transport.calls_to(f"{STRIPE}/payment_links/plink_0", "POST"), "old link deactivated"
    key = transport.calls_to(f"{STRIPE}/payment_links", "POST")[0]["headers"]["Idempotency-Key"]
    assert key.endswith("-x2-link")
    assert "https://buy.stripe.com/l1" in kit.files.read_text("site/python-remote/index.html")
    # an order on the retired $9 link still finds its dataset
    assert state.asset_for_product("plink_0")["id"] == asset["id"]
    # views are measured per experiment, from its own start
    assert state.running_experiment(asset["id"])["views_at_start"] == 25


def test_floor_without_sales_creates_2_for_1_bundle(live, state, clock, make_hypothesis):
    kit, hyp, asset = live
    other = make_hypothesis("ai-infrastructure", ["python"])
    pipeline(kit, other)
    state.update_asset(asset["id"], price_cents=500)
    eng = PricingEngine(kit)
    eng.run(hyp)
    state.set_metric(hyp["id"], "views", "github_traffic", 40)
    clock.advance(hours=49)
    report = eng.run(hyp)
    assert "bundle" in report["decisions"][0]
    bundle = next(a for a in state.list_assets(hyp["id"]) if a["kind"] == "bundle")
    assert "2-for-1" in bundle["title"] and bundle["checkout_url"] and bundle["status"] == "published"
    names = zipfile.ZipFile(io.BytesIO(kit.files.read_bytes(bundle["path"]))).namelist()
    assert any(n.startswith("bundle/python-remote-intel/") for n in names)
    assert any(n.startswith("bundle/ai-infrastructure-intel/") for n in names)
    assert json.loads(bundle["kind_meta"])["parts"][0] == asset["id"]
    # no second bundle while one is live
    clock.advance(hours=49)
    state.set_metric(hyp["id"], "views", "github_traffic", 90)
    eng.run(hyp)
    assert sum(1 for a in state.list_assets(hyp["id"]) if a["kind"] == "bundle") == 1


def test_strong_demand_expands_depth_and_builds_premium(live, state, clock):
    kit, hyp, asset = live
    for i in range(3):
        state.record_order("stripe", f"cs_{i}", "b@x.com", 900, "plink_0", asset["id"], hyp["id"], clock().isoformat())
    report = PricingEngine(kit).run(hyp)
    assert any("scrape depth 1→2" in r for r in report["demand"])
    h = state.get_hypothesis(hyp["id"])
    assert h["params"]["depth"] == 2 and len(h["params"]["keywords"]) > len(hyp["params"]["keywords"])
    premium = next(a for a in state.list_assets(hyp["id"]) if a["kind"] == "premium")
    assert premium["price_cents"] == 1900 and premium["checkout_url"]
    zf = zipfile.ZipFile(io.BytesIO(kit.files.read_bytes(premium["path"])))
    assert "## Acme Inc" in zf.read("python-remote-deep-dive/DEEP_DIVE.md").decode()
    # 3 orders at $9 also raise the price
    assert any("raise" in r for r in report["decisions"])
    again = PricingEngine(kit).run(state.get_hypothesis(hyp["id"]))
    assert again["demand"] == [] or all("depth" not in r and "premium" not in r for r in again["demand"])
    assert sum(1 for a in state.list_assets(hyp["id"]) if a["kind"] == "premium") == 1


def test_initiations_and_dropoff_from_sessions(live, state, clock, transport):
    kit, hyp, asset = live
    transport.add_json(f"{STRIPE}/checkout/sessions", {"has_more": False, "data": [
        {"id": "cs_a", "status": "expired", "payment_status": "unpaid"},
        {"id": "cs_b", "status": "open", "payment_status": "unpaid"},
        {"id": "cs_c", "status": "complete", "payment_status": "paid", "amount_total": 900},
        {"id": "cs_d", "status": "expired", "payment_status": "unpaid"},
    ]})
    state.record_order("stripe", "cs_c", "b@x.com", 900, "plink_0", asset["id"], hyp["id"], clock().isoformat())
    eng = PricingEngine(kit)
    eng.run(hyp)
    stats = eng.refresh(state.running_experiment(asset["id"]))
    assert stats == {"views": 0, "initiations": 4, "orders": 1, "recent_orders": 1, "dropoff_pct": 75}


def test_pricing_needs_an_api_storefront(toolkit, state, make_hypothesis):
    hyp = make_hypothesis()
    report = PricingEngine(toolkit).run(hyp)
    assert "Stripe secret key" in report["decisions"][0]


def test_arbeitnow_depth_pages(toolkit, transport):
    page = {"data": [{"slug": "a", "company_name": "A", "title": "Python Dev", "url": "https://www.arbeitnow.com/view/a",
                      "tags": [], "location": "Remote", "created_at": 0}], "links": {"next": "https://x?page=2"}}
    last = {"data": [{"slug": "b", "company_name": "B", "title": "Python Dev", "url": "https://www.arbeitnow.com/view/b",
                      "tags": [], "location": "Remote", "created_at": 0}], "links": {"next": None}}
    transport.add("https://www.arbeitnow.com/api/job-board-api", [
        Response(200, "u", json.dumps(page).encode()), Response(200, "u", json.dumps(last).encode()),
    ])
    leads = fetch_arbeitnow(toolkit.http, depth=3)
    assert [l.company for l in leads] == ["A", "B"]
    urls = [c["url"] for c in transport.calls_to("https://www.arbeitnow.com")]
    assert urls[1].endswith("?page=2") and len(urls) == 2
