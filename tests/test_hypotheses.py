from agent.hypotheses import formulate_next, hypothesis_key


def _create(state, proposal, status="active"):
    hid = state.create_hypothesis(proposal["key"], proposal["strategy"], proposal["description"], proposal["params"])
    if status != "active":
        state.set_hypothesis_status(hid, status, "test")
    return hid


def test_configured_niches_first_in_order(state, config):
    p1 = formulate_next(state, config)
    assert p1["params"]["niche"] == "python-remote" and p1["params"]["generation"] == 1
    _create(state, p1, "deprecated")
    assert formulate_next(state, config)["params"]["niche"] == "rust-systems"


def test_mines_niches_from_collected_tags(state, config):
    for n in config.niches:
        _create(state, formulate_next(state, config), "deprecated")
    for i in range(3):
        state.upsert_lead(f"k{i}", "python-remote", {"tags": ["elixir", "remote"], "stack": []})
    p = formulate_next(state, config)
    assert p["params"]["niche"] == "tag-elixir"
    assert p["params"]["keywords"] == ["elixir"]
    assert "mined" in p["params"]["origin"]


def test_revisits_with_broadened_keywords_then_exhausts(state, config):
    config.max_hypothesis_generations = 2
    state.upsert_lead("k", "x", {"tags": ["graphql"], "stack": []})  # below mining threshold of 2
    for _ in config.niches:
        _create(state, formulate_next(state, config), "deprecated")
    revisit = formulate_next(state, config)
    assert revisit["key"] == hypothesis_key("python-remote", 2)
    assert "graphql" in revisit["params"]["keywords"]
    _create(state, revisit, "deprecated")
    _create(state, formulate_next(state, config), "deprecated")  # rust-systems g2
    assert formulate_next(state, config) is None
