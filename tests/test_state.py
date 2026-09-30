from datetime import timedelta


def test_kv_and_incr(state):
    assert state.get("missing", 5) == 5
    state.set("k", {"a": 1})
    assert state.get("k") == {"a": 1}
    assert state.incr("n") == 1 and state.incr("n") == 2


def test_hypothesis_lifecycle(state):
    hid = state.create_hypothesis("k1", "s", "desc", {"niche": "x"})
    assert state.active_hypothesis()["id"] == hid
    assert state.increment_hypothesis_iterations(hid) == 1
    state.set_hypothesis_status(hid, "deprecated", "no traction")
    assert state.active_hypothesis() is None
    assert state.get_hypothesis(hid)["reason"] == "no traction"
    assert state.hypothesis_keys() == {"k1"}


def test_backlog_ordering_and_cancel(state):
    hid = state.create_hypothesis("k", "s", "d", {})
    state.add_task(hid, "b", priority=20)
    first = state.add_task(hid, "a", {"x": 1}, priority=10)
    task = state.next_task(hid)
    assert task["id"] == first and task["payload"] == {"x": 1}
    state.finish_task(first, "done", "ok")
    assert state.next_task(hid)["task"] == "b"
    assert state.cancel_tasks(hid) == 1
    assert state.next_task(hid) is None


def test_revenue_is_idempotent_and_day_scoped(state, clock):
    assert state.record_revenue("gumroad", "s1", 900, 140, 760, True)
    assert not state.record_revenue("gumroad", "s1", 900, 140, 760, True)
    state.record_revenue("manual", "m1", 500, 0, 500, False)
    assert state.revenue_for_day()["net"] == 760
    assert state.revenue_for_day(verified_only=False)["net"] == 1260
    clock.advance(days=1)
    assert state.revenue_for_day()["net"] == 0


def test_revenue_attribution(state):
    hid = state.create_hypothesis("k", "s", "d", {})
    aid = state.add_asset(hid, "lead_directory", "T", "p.zip", 1, 10, 900)
    state.link_product(aid, "prod_1")
    assert state.hypothesis_for_product("prod_1") == hid
    state.record_revenue("gumroad", "s1", 900, 140, 760, True, hypothesis_id=hid)
    assert state.revenue_for_hypothesis(hid) == 760


def test_leads_dedupe_per_niche(state):
    assert state.upsert_lead("k1", "n", {"company": "A", "tags": ["python"]})
    assert not state.upsert_lead("k1", "n", {"company": "A2", "tags": ["python"]})
    assert state.upsert_lead("k1", "other", {"company": "A", "tags": ["python"]})
    assert state.leads_for_niche("n")[0]["company"] == "A2"
    assert state.count_leads("n") == 1
    assert state.count_leads() == 1  # distinct across niches
    assert state.tag_frequencies() == [("python", 1)]


def test_outreach_queue_dedupes_and_tracks_cooldown(state, clock):
    oid = state.stage_outreach(1, "lead1", "email", "a@x.com", "s", "b", 0.9)
    assert oid is not None
    assert state.stage_outreach(1, "lead1", "email", "a@x.com", "s", "b", 0.9) is None
    assert state.recipient_contacted_since("a@x.com", clock() - timedelta(days=30))
    state.set_outreach_status(oid, "rejected")
    assert not state.recipient_contacted_since("a@x.com", clock() - timedelta(days=30))
    assert state.outreach_counts() == {"rejected": 1}


def test_persistence_across_reopen(config, clock):
    from agent.state import StateStore

    config.ensure_dirs()
    s1 = StateStore(config.db_path, clock=clock)
    s1.set("iteration", 7)
    s1.close()
    s2 = StateStore(config.db_path, clock=clock)
    assert s2.get("iteration") == 7
    s2.close()
