"""Phases 295-299: conditional requests, timings, back-off, spreading, contribution."""

import json

from strategies import source_efficiency as se
from strategies.b2b_lead_aggregator import LeadAggregator
from strategies.base import TaskContext
from tests.test_job_sources import REMOTIVE
from tests.test_product_factory import postings
from tools.http_client import Response

URL = "https://remotive.com/api/remote-jobs"


def test_unchanged_feeds_are_not_downloaded_again(toolkit, state, transport):
    body = json.dumps(REMOTIVE).encode()
    seen = []

    def route(method, url, headers):
        seen.append(headers)
        if headers.get("If-None-Match") == '"v1"':
            return Response(304, url, b"", {})
        return Response(200, url, body, {"ETag": '"v1"'})

    transport.add(URL, route)
    http = se.ConditionalHttp(toolkit.http, state, toolkit.files)
    assert http.get_json(URL, params={"category": "software-dev"}) == REMOTIVE
    second = http.get(URL, params={"category": "software-dev"})
    assert second.json() == REMOTIVE and second.headers == {"x-from-cache": "1"}
    assert "If-None-Match" not in seen[0] and seen[1]["If-None-Match"] == '"v1"'
    assert list((state.get(se.VALIDATORS) or {}).values())[0]["not_modified"] == 1


def test_timings_and_back_off(state, clock):
    se.record(state, "remotive", True, 0.2)
    se.record(state, "remotive", True, 0.4)
    assert state.get(se.TIMINGS)["remotive"] == {"runs": 2, "avg_ms": 300, "last_ms": 400, "last_ok": True}
    assert se.backoff_hours(state, "remotive") == 0
    for _ in range(4):
        se.record(state, "remotive", False, 0)
    assert se.backoff_hours(state, "remotive") == 8
    last = state.now()
    assert not se.due(state, "remotive", 0, last)
    clock.advance(hours=8)
    assert se.due(state, "remotive", 0, last)
    se.record(state, "remotive", True, 0.1)
    assert se.backoff_hours(state, "remotive") == 0


def test_boards_are_spread_out():
    factors = {s: se.jitter(s) for s in ("remotive", "jobicy", "himalayas", "weworkremotely")}
    assert all(0.9 <= f <= 1.1 for f in factors.values()) and len(set(factors.values())) > 1
    assert se.jitter("remotive") == se.jitter("remotive")


def test_failing_board_backs_off_in_the_aggregator(toolkit, state, config, transport, clock, make_hypothesis):
    config.lead_sources = ["remoteok", "arbeitnow"]
    transport.add("https://remoteok.com/api", Response(404, "u", b"gone", {}))  # raises: a failure
    hyp = make_hypothesis(keywords=["rust"])
    agg = LeadAggregator()
    for _ in range(2):
        agg.run("aggregate_leads", TaskContext(toolkit, hyp, {}))
        clock.advance(minutes=30)
    calls = len(transport.calls_to("https://remoteok.com/api"))
    agg.run("aggregate_leads", TaskContext(toolkit, hyp, {}))
    assert len(transport.calls_to("https://remoteok.com/api")) == calls  # backing off after 2 failures
    assert state.get(se.FAILURES)["remoteok"] == 2 and state.get(se.TIMINGS)["arbeitnow"]["runs"] == 3


def test_what_each_board_brings(state, clock):
    postings(state, clock, 4, ["rust"])
    state._exec("UPDATE leads SET data = json_set(data, '$.source', 'remotive') WHERE id = 1")
    rows = {r["source"]: r for r in se.contribution(state)}
    assert rows["remoteok"]["new"] == 3 and rows["remotive"] == {"source": "remotive", "new": 1, "only_here": 1, "avg_ms": None}
    assert se.describe(state).startswith("Job boards, last 30 days: Remote OK 3 new (3 only there)")


def test_cli_parses():
    from dashboard.cli import build_parser

    assert build_parser().parse_args(["sources"]).func.__name__ == "cmd_sources"
