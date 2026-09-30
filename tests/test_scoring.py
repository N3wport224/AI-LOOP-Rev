
from agent.engine import Engine
from agent.hypotheses import ADJACENT, CLUSTERS, formulate_next, pivot_reason, score_hypothesis
from strategies.base import Strategy, TaskResult


def test_score_hypothesis_funnel(state, make_hypothesis):
    hyp = make_hypothesis(iterations=4)
    state.set_metric(hyp["id"], "impressions", "outreach_sent", 20)
    state.set_metric(hyp["id"], "impressions", "surfaces", 3)
    state.set_metric(hyp["id"], "views", "github_traffic", 40)
    state.set_metric(hyp["id"], "views", "github_traffic", 10)  # snapshots never go down
    state.record_order("stripe", "cs_1", "a@b.com", 900, "p", None, hyp["id"])
    state.record_order("stripe", "cs_2", "c@d.com", 900, "p", None, hyp["id"])
    state.record_revenue("stripe", "cs_1", 900, 56, 844, True, hypothesis_id=hyp["id"])
    s = score_hypothesis(state, state.get_hypothesis(hyp["id"]))
    assert (s["impressions"], s["views"], s["purchases"]) == (23, 40, 2)
    assert s["conversion"] == 0.05 and s["velocity"] == 0.5 and s["view_rate"] == round(40 / 23, 4)
    assert s["score"] > 0


def test_pivot_rules(state, config, make_hypothesis):
    config.signal_window_iterations, config.pivot_after_iterations = 12, 24
    hyp = make_hypothesis(iterations=12)
    h = state.get_hypothesis(hyp["id"])
    # views rule only applies once view tracking works
    assert pivot_reason(state, config, h) is None
    state.set("view_tracking", True)
    assert pivot_reason(state, config, h) == "no views or sales after 12 iterations"
    state.set_metric(hyp["id"], "views", "github_traffic", 3)
    assert pivot_reason(state, config, h) is None  # views but no sales: keep going until 24
    for _ in range(12):
        state.increment_hypothesis_iterations(hyp["id"])
    h = state.get_hypothesis(hyp["id"])
    assert pivot_reason(state, config, h) == "zero verified revenue after 24 iterations"
    state.record_order("lemonsqueezy", "1", "x@y.com", 900, None, None, hyp["id"])
    assert pivot_reason(state, config, h) is None  # a purchase is traction even before revenue sync


def test_adjacent_pivot_ranked_by_recent_demand(state, config, clock, make_hypothesis):
    hyp = make_hypothesis("python-remote")
    state.set_hypothesis_status(hyp["id"], "deprecated", "no views")
    # data-engineering has the most fresh demand in the pooled leads
    for i in range(3):
        state.upsert_lead(f"d{i}", "__all__", {"title": "Data Engineer", "tags": ["snowflake"], "stack": []})
    state.upsert_lead("a0", "__all__", {"title": "MLOps Engineer", "tags": ["llm"], "stack": []})
    p = formulate_next(state, config)
    assert p["params"]["niche"] == "data-engineering"
    assert p["params"]["keywords"] == CLUSTERS["data-engineering"]
    assert "adjacent to python-remote (3 matching roles" in p["params"]["origin"]
    state.create_hypothesis(p["key"], p["strategy"], p["description"], p["params"])
    # old demand ages out of the window
    clock.advance(days=8)
    state.set_hypothesis_status(state.active_hypothesis()["id"], "deprecated", "x")
    nxt = formulate_next(state, config)
    assert nxt["params"]["niche"] in ADJACENT["data-engineering"]


def test_every_adjacent_cluster_is_defined():
    for source, targets in ADJACENT.items():
        assert all(t in CLUSTERS for t in targets), source


class Noop(Strategy):
    name = "noop"
    tasks = ("aggregate_leads",)

    def run(self, task, ctx):
        return TaskResult(True, "ok")


def test_engine_pivots_on_no_views_within_signal_window(config, state, toolkit):
    config.signal_window_iterations, config.pivot_after_iterations = 12, 100
    state.set("view_tracking", True)
    engine = Engine(config, state=state, strategies=[Noop()], toolkit=toolkit, sleep=lambda s: None)
    for _ in range(12):
        engine.run_cycle()
    first = state.active_hypothesis()
    assert first["params"]["niche"] == "python-remote" and first["iterations"] == 12
    report = engine.run_cycle()
    assert report.pivots and "no views or sales" in report.pivots[0]
    assert state.active_hypothesis()["params"]["niche"] in ADJACENT["python-remote"]
    assert state.get(f"score:{state.active_hypothesis()['id']}")["views"] == 0


def test_engine_stores_score_each_cycle(config, state, toolkit):
    engine = Engine(config, state=state, strategies=[Noop()], toolkit=toolkit, sleep=lambda s: None)
    engine.run_cycle()
    hid = state.active_hypothesis()["id"]
    assert set(state.get(f"score:{hid}")) >= {"impressions", "views", "purchases", "velocity", "score"}


def test_state_migrates_old_databases(tmp_path):
    import sqlite3

    from agent.state import StateStore

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE assets (id INTEGER PRIMARY KEY, hypothesis_id INTEGER, kind TEXT NOT NULL, title TEXT NOT NULL, "
        "path TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, lead_count INTEGER NOT NULL DEFAULT 0, "
        "price_cents INTEGER NOT NULL DEFAULT 0, product_ref TEXT, status TEXT NOT NULL DEFAULT 'staged', created_at TEXT NOT NULL);"
    )
    conn.close()
    s = StateStore(db)
    aid = s.add_asset(1, "k", "t", "p", 1, 1, 900)
    s.update_asset(aid, checkout_url="https://c", provider="stripe")
    assert s.get_asset(aid)["checkout_url"] == "https://c"
    s.close()
