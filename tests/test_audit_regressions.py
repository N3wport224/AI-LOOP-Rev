"""Regression tests for flaws found in the operational audit (see AUDIT.md).

Each test describes the correct behaviour; the audit confirmed every one of them failed
before its fix.
"""

import json
from datetime import timedelta

import pytest

from agent.config import Config
from agent.engine import Engine
from agent.hypotheses import formulate_next, pivot_reason
from agent.pricing_engine import PricingEngine, decide
from strategies.base import Strategy, TaskContext
from strategies.digital_asset_packager import DigitalAssetPackager
from strategies.distribution_engine import DistributionEngine
from tests.conftest import NOW
from tests.test_distribution import pipeline
from tests.test_pricing import MATRIX, exp

# --------------------------------------------------------------------------- pricing lifecycle


def test_new_dataset_version_keeps_price_experiment_and_learned_price(live, state, clock, transport):
    """F1: a routine data refresh (new asset version) must not reset the 48h experiment window,
    and must not overwrite the price the pricing engine is testing (the lander would show one
    price while Stripe charges another)."""
    kit, hyp, asset = live
    kit.config.allow_manual_fulfillment = True  # dry run: let publish_listing reach the lander step
    eng = PricingEngine(kit)
    eng.run(hyp)
    exp1 = state.running_experiment(asset["id"])
    clock.advance(hours=30)
    state.upsert_lead("fresh", "python-remote", {"company": "Newco", "title": "Python Engineer", "url": "https://n", "stack": ["python"]})
    res = DigitalAssetPackager().run("package_asset", TaskContext(kit, hyp, {}))
    assert res.metrics["built"] and res.metrics["version"] == 2
    v2 = state.latest_asset(hyp["id"], "lead_directory")
    assert v2["price_cents"] == 900, "live price must survive a data refresh"
    links_before = len(transport.calls_to("https://api.stripe.com/v1/payment_links", "POST"))
    DistributionEngine().run("publish_listing", TaskContext(kit, hyp, {}))
    assert len(transport.calls_to("https://api.stripe.com/v1/payment_links", "POST")) == links_before
    assert "$9.00" in kit.files.read_text("site/python-remote/index.html")
    eng.run(hyp)
    exp2 = state.running_experiment(v2["id"])
    assert exp2 is not None and exp2["id"] == exp1["id"], "experiment follows the product, not the version"
    assert exp2["started_at"] == exp1["started_at"]
    # and so the window can actually elapse and a step-down happen
    state.set_metric(hyp["id"], "views", "github_traffic", 50)
    clock.advance(hours=20)
    assert any("lower" in d for d in eng.run(hyp)["decisions"])


def test_no_view_tracking_still_steps_down_eventually():
    """F2: without GitHub traffic data, views are always 0, so the > 20 views rule could never
    fire and an unsold price was held forever."""
    e = exp(1400, 97)
    r = decide(e, {"views": 0, "orders": 0, "initiations": 0}, [], NOW, MATRIX, 20, 48, view_tracking=False)
    assert (r.action, r.price_cents) == ("lower", 900) and "no traffic data" in r.reason
    assert decide(exp(1400, 50), {"views": 0, "orders": 0, "initiations": 0}, [], NOW, MATRIX, 20, 48,
                  view_tracking=False).action == "hold"


def test_abandoned_checkouts_are_a_price_signal():
    """F3: checkout initiations were tracked but never used. People who reach checkout and leave
    are the clearest price signal there is."""
    r = decide(exp(1400, 49), {"views": 5, "orders": 0, "initiations": 4}, [], NOW, MATRIX, 20, 48)
    assert (r.action, r.price_cents) == ("lower", 900) and "abandoned" in r.reason


def test_converged_price_reopens_when_demand_disappears():
    """F4: 'converged' was terminal. If sales stop, the engine must re-explore."""
    e = {**exp(1400, 24 * 20), "status": "converged"}
    r = decide(e, {"views": 60, "orders": 5, "initiations": 6, "recent_orders": 0}, [], NOW, MATRIX, 20, 48)
    assert r.action == "lower" and "demand" in r.reason
    held = decide(e, {"views": 60, "orders": 5, "initiations": 6, "recent_orders": 2}, [], NOW, MATRIX, 20, 48)
    assert held.action == "hold"


def test_view_counter_accumulates_rolling_github_window(toolkit, state, config, transport, make_hypothesis):
    """F5: GitHub reports a rolling 14-day count. Storing its running maximum froze the counter
    after the peak, so later experiments never saw new views."""
    from tools.http_client import Response

    hyp = make_hypothesis()
    config.github_token, config.github_showcase_repo = "ghp", "me/showcase"
    toolkit.github.token = "ghp"
    for count in (30, 20, 35):
        transport.add("https://api.github.com/repos/me/showcase/traffic/popular/paths", Response(
            200, "u", json.dumps([{"path": "/me/showcase/tree/main/showcase/python-remote", "count": count}]).encode()))
        DistributionEngine().run("collect_metrics", TaskContext(toolkit, hyp, {}))
    # 30 seen, then at least 15 new ones (20 -> 35) even though 10+ aged out in between
    assert state.metrics_for_hypothesis(hyp["id"])["views"] == 45


# --------------------------------------------------------------------------- hypotheses


def test_market_demand_beats_hardcoded_order(state, config):
    """F6: new niches were only mined from live tags after every configured niche and hard-coded
    cluster had been tried (~11 pivots). With real demand data, the best-evidenced candidate wins."""
    config.niches = [{"name": "cobol-legacy", "keywords": ["cobol"]}, {"name": "fortran-hpc", "keywords": ["fortran"]}]
    for i in range(12):
        state.upsert_lead(f"e{i}", "__all__", {"title": "Elixir Engineer", "tags": ["elixir"], "stack": []})
    p = formulate_next(state, config)
    assert p["params"]["niche"] == "tag-elixir" and "12 matching roles" in p["params"]["origin"]
    for i in range(15):
        state.upsert_lead(f"c{i}", "__all__", {"title": "COBOL Developer", "tags": ["cobol"], "stack": []})
    assert formulate_next(state, config)["params"]["niche"] == "cobol-legacy"


def test_cold_start_still_uses_configured_order(state, config):
    assert formulate_next(state, config)["params"]["niche"] == "python-remote"


def test_faded_traction_triggers_pivot(state, config, clock, make_hypothesis):
    """F7: one early sale made a hypothesis immortal, even if it never sold again."""
    config.pivot_after_iterations = 3
    hyp = make_hypothesis(iterations=5)
    state.record_revenue("stripe", "old", 900, 56, 844, True, occurred_at=(NOW - timedelta(days=40)).isoformat(),
                         hypothesis_id=hyp["id"])
    h = state.get_hypothesis(hyp["id"])
    assert pivot_reason(state, config, h).startswith("traction faded")
    state.record_revenue("stripe", "new", 900, 56, 844, True, occurred_at=(NOW - timedelta(days=2)).isoformat(),
                         hypothesis_id=hyp["id"])
    assert pivot_reason(state, config, h) is None


def test_subscribed_niche_data_refreshed_after_pivot(stripe_kit, state, clock, make_hypothesis, transport):
    """F8: after a pivot nothing refreshed the old niche, so its paying subscribers got
    '0 changes' every Monday until they churned."""
    from strategies.subscription_engine import SubscriptionEngine

    old = make_hypothesis()
    pipeline(stripe_kit, old)
    state.set_hypothesis_status(old["id"], "deprecated", "pivot")
    new = make_hypothesis("ai-infrastructure", ["llm"])
    state.upsert_subscriber("stripe", "sub_1", email="s@x.example", niche="python-remote", price_cents=1000)
    clock.advance(days=12)  # next Monday is 2026-10-12... move to a Monday 09:00
    clock.now = clock.now.replace(hour=9) + timedelta(days=(7 - clock.now.weekday()) % 7)
    transport.add_json("https://www.arbeitnow.com/api/job-board-api", {"data": [{
        "slug": "fresh", "company_name": "Freshco", "title": "Senior Python Engineer", "url": "https://www.arbeitnow.com/view/fresh",
        "tags": ["python"], "location": "Remote", "remote": True, "created_at": int(clock.now.timestamp())}]})
    res = SubscriptionEngine().deliver_subscriptions(TaskContext(stripe_kit, new, {}))
    assert res.metrics["refreshed"] == ["python-remote"]
    period = res.metrics["period"]
    meta = stripe_kit.files.read_json(f"assets/python-remote/weekly/{period}.json")
    assert meta["delta_count"] >= 1, "the fresh posting must appear in this week's delta"


@pytest.fixture
def stripe_kit(config, state, breaker, transport):
    from tools import build_toolkit

    return build_toolkit(config, state, breaker, transport=transport, sleep=lambda s: None)


# --------------------------------------------------------------------------- safety


class DropsNetwork(Strategy):
    name = "drops"
    tasks = ("aggregate_leads",)

    def __init__(self, net):
        self.net, self.calls = net, 0

    def run(self, task, ctx):
        self.calls += 1
        self.net["up"] = False  # the wifi dies mid-request
        raise ConnectionError("network is unreachable")


def test_network_drop_mid_cycle_pauses_instead_of_failing(config, state, toolkit):
    """F9: a drop *during* a cycle burned 3 retries per task and counted each as an operational
    failure; with enough network-bound tasks in a row that tripped the emergency stop."""
    net = {"up": True}
    strat = DropsNetwork(net)
    eng = Engine(config, state=state, strategies=[strat], toolkit=toolkit, sleep=lambda s: None, online_check=lambda: net["up"])
    for _ in range(config.max_consecutive_errors + 2):
        net["up"] = True  # back at cycle start, gone again mid-task
        report = eng.run_cycle()
        assert report.status == "offline"
    assert not eng.breaker.tripped and eng.breaker.consecutive_errors == 0
    assert strat.calls == config.max_consecutive_errors + 2, "no retries against a dead network"
    assert state.count_errors("operational_failure") == 0
    assert state.active_hypothesis()["iterations"] == 0, "outage cycles must not count toward the pivot budget"
    assert [t["task"] for t in state.pending_tasks(state.active_hypothesis()["id"])][0] == "aggregate_leads"


@pytest.mark.parametrize("raw,expected", [("", True), ("  ", True), ("maybe", True), ("true", True),
                                          ("false", False), ("0", False), ("No", False), ("OFF", False)])
def test_dry_run_only_disabled_by_explicit_false(raw, expected):
    """F10: an empty DRY_RUN= (e.g. a blanked .env line) switched live email ON."""
    assert Config.load(None, env={"DRY_RUN": raw}).dry_run is expected


def test_syndication_cadence_survives_state_reset(config, state, toolkit, transport):
    """F11: the 5-day cadence lived only in the local DB; a wiped DB meant an immediate repost.
    The platform's own record of the last post is now the source of truth."""
    from tools.syndication import Syndicator, build_article
    from tools.syndication.devto_publisher import DevToPublisher

    transport.add_json("https://dev.to/api/articles/me/all", [
        {"title": "Older post", "published_at": (NOW - timedelta(days=2)).isoformat().replace("+00:00", "Z")}])
    transport.add_json("https://dev.to/api/articles", {"url": "https://dev.to/x"}, 201)
    syn = Syndicator(config, state, toolkit.files, [DevToPublisher(toolkit.http, "dk")])
    res = syn.syndicate(build_article("p", "P", [{"company": "C", "stack": ["Go"], "intent_signals": []}] * 3, NOW), NOW)
    assert res["devto"] == "not due"
    assert not transport.calls_to("https://dev.to/api/articles", "POST")


def test_syndication_fails_closed_when_platform_history_unreachable(config, state, toolkit, transport):
    from tools.http_client import Response
    from tools.syndication import Syndicator, build_article
    from tools.syndication.devto_publisher import DevToPublisher

    transport.add("https://dev.to/api/articles/me/all", Response(503, "u"))
    syn = Syndicator(config, state, toolkit.files, [DevToPublisher(toolkit.http, "dk")])
    res = syn.syndicate(build_article("p", "P", [{"company": "C", "stack": [], "intent_signals": []}], NOW), NOW)
    assert res["devto"].startswith("skipped") and not transport.calls_to("https://dev.to/api/articles", "POST")


# --------------------------------------------------------------------------- resources


def test_cli_commands_close_their_database(tmp_path, monkeypatch):
    """F12: every CLI command left its SQLite connection open."""
    from agent.state import StateStore
    from dashboard.cli import main
    from rich.console import Console

    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "a.toml"
    main(["--config", str(cfg), "init"], console=Console(record=True))
    before = StateStore.open_count()
    for argv in (["status"], ["hypotheses"], ["orders", "list"], ["analytics"], ["stop"], ["resume"], ["pricing", "status"]):
        main(["--config", str(cfg), *argv], console=Console(record=True))
    assert StateStore.open_count() == before


def test_indexes_cover_hot_paths(state):
    """F13: assets and price_experiments had no indexes; every hot query was a full scan."""
    c = state.conn
    for q, a in [
        ("SELECT COUNT(*) FROM orders WHERE hypothesis_id=? AND occurred_at>=?", (1, "x")),
        ("SELECT * FROM orders WHERE product_ref=?", ("p",)),
        ("SELECT * FROM orders WHERE status='paid'", ()),
        ("SELECT * FROM assets WHERE product_ref=?", ("p",)),
        ("SELECT * FROM assets WHERE hypothesis_id=? AND kind=?", (1, "k")),
        ("SELECT * FROM hypotheses WHERE status='active'", ()),
        ("SELECT * FROM subscribers WHERE subscription_status=?", ("active",)),
        ("SELECT * FROM checkout_sessions WHERE payment_intent=?", ("pi",)),
        ("SELECT * FROM price_experiments WHERE asset_id=? AND status=?", (1, "running")),
        ("SELECT * FROM revenue WHERE hypothesis_id=?", (1,)),
    ]:
        plan = " ".join(r[3] for r in c.execute("EXPLAIN QUERY PLAN " + q, a))
        assert "USING" in plan, f"full scan: {q} -> {plan}"


def test_webhook_executor_threads_released_on_shutdown(toolkit, config):
    import threading

    from tools.storefront.webhook_listener import WebhookServer

    config.stripe_webhook_secret = "whsec_x"
    srv = WebhookServer(toolkit, host="127.0.0.1", port=0)
    stop = threading.Event()
    t = threading.Thread(target=srv.run, args=(stop,))
    t.start()
    assert srv.started.wait(5)
    # force the executor to spin up a worker
    import urllib.request

    from tools.storefront.webhook_listener import sign_payload

    body = b'{"id":"evt_x","type":"customer.created","data":{"object":{}}}'
    req = urllib.request.Request(f"http://127.0.0.1:{srv.bound_port}/webhook", data=body, method="POST",
                                 headers={"Stripe-Signature": sign_payload(body, "whsec_x")})
    urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=5).close()
    stop.set()
    t.join(5)
    assert not [th for th in threading.enumerate() if th.name.startswith("webhook")]


# --------------------------------------------------------------------------- attribution (executed in Node)

NODE_HARNESS = r"""
const [js, scenarios] = [process.argv[1], JSON.parse(process.argv[2])];
const store = {};
const out = [];
for (const sc of scenarios) {
  globalThis.location = { search: sc.search };
  globalThis.Date.now = () => sc.now;
  globalThis.localStorage = sc.brokenStorage
    ? { getItem() { throw new Error("denied"); }, setItem() { throw new Error("denied"); } }
    : { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = v; } };
  const links = [{ href: "https://buy.stripe.com/abc" }, { href: "https://buy.stripe.com/sub?x=1" }];
  globalThis.document = { querySelectorAll: () => links };
  eval(js);
  out.push(links.map(l => new URL(l.href).searchParams.get("client_reference_id")));
}
console.log(JSON.stringify(out));
"""


def _run_lander_js(scenarios):
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    from tools.attribution import LANDER_ATTRIBUTION_JS

    res = subprocess.run([node, "-e", NODE_HARNESS, LANDER_ATTRIBUTION_JS, json.dumps(scenarios)],
                         capture_output=True, text=True, timeout=20, check=True)
    return json.loads(res.stdout)


def test_lander_attribution_persists_first_touch_across_pages():
    """F15: UTM attribution was lost as soon as the visitor navigated to another page."""
    day = 86400000
    got = _run_lander_js([
        {"search": "?utm_source=Dev.to&utm_campaign=Radar W40", "now": 0},  # lands from Dev.to
        {"search": "", "now": 1 * day},                                      # browses another dataset page
        {"search": "?utm_source=rss", "now": 2 * day},                        # later touch: first touch wins
        {"search": "", "now": 31 * day},                                     # after 30 days: expired
    ])
    ref = "am--dev_to--radar_w40"
    assert got[0] == [ref, ref] and got[1] == [ref, ref] and got[2] == [ref, ref]
    assert got[3] == [None, None]
    from tools.attribution import decode_ref, encode_ref

    assert encode_ref("Dev.to", "Radar W40") == ref and decode_ref(ref) == ("dev_to", "radar_w40")  # JS == Python


def test_lander_attribution_works_without_storage():
    got = _run_lander_js([{"search": "?utm_source=hn", "now": 0, "brokenStorage": True},
                          {"search": "", "now": 1, "brokenStorage": True}])
    assert got == [["am--hn--", "am--hn--"], [None, None]]


def test_analytics_window_boundaries_are_exact(state, config, toolkit, clock):
    """Audit check: revenue at the window's first second and at 'now' is counted; one second
    earlier is not; per-day and MRR maths use the same window."""
    from dashboard.analytics import compute

    start = NOW - timedelta(days=30)
    for ext, ts in (("at_start", start), ("at_now", NOW), ("too_old", start - timedelta(seconds=1))):
        state.record_revenue("stripe", ext, 1000, 59, 941, True, occurred_at=ts.isoformat())
    r = compute(state, config, "30d")
    assert r["totals"]["orders"] == 2 and r["totals"]["net_cents"] == 2 * 941
    assert r["days"] == 30 and r["net_per_day_cents"] == round(2 * 941 / 30)
    assert r["today_net_cents"] == 941


def test_default_config_gives_a_product_time_to_be_found(state, clock, make_hypothesis):
    """F16: with the defaults (hourly cycles, 24-cycle pivot), a product was abandoned after
    24 hours: before the pricing engine's first 48h window closed and before a second
    syndication post (5-day cadence) could bring traffic. Pivots now also need wall-clock age."""
    cfg = Config()
    assert cfg.interval_seconds == 3600
    hyp = make_hypothesis(iterations=cfg.pivot_after_iterations + 10)  # plenty of cycles...
    state.set("view_tracking", True)
    h = state.get_hypothesis(hyp["id"])
    assert pivot_reason(state, cfg, h) is None, "...but only minutes old: too early to judge"
    clock.advance(days=cfg.min_hypothesis_days)
    assert pivot_reason(state, cfg, h) is not None
    # the minimum age always covers two pricing windows and two syndication slots
    assert cfg.min_hypothesis_days * 24 >= 2 * cfg.pricing_window_hours
    assert cfg.min_hypothesis_days >= 2 * cfg.syndication_interval_days


def test_data_starvation_still_pivots_immediately(config, state, toolkit):
    """The age rule must not slow down pivots on hard evidence (no data exists for the niche)."""
    from strategies.base import TaskResult

    class Starved(Strategy):
        name = "starved"
        tasks = ("aggregate_leads",)

        def run(self, task, ctx):
            return TaskResult(True, "0 leads", invalidates_hypothesis=True)

    config.min_hypothesis_days = 7
    eng = Engine(config, state=state, strategies=[Starved()], toolkit=toolkit, sleep=lambda s: None)
    assert eng.run_cycle().pivots
