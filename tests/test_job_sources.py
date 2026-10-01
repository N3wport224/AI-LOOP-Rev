"""Phases 195-199: Remotive, Jobicy, Himalayas, We Work Remotely, cadence and credits."""

from strategies import job_sources as js
from strategies.b2b_lead_aggregator import FETCHERS, LeadAggregator, enrich_lead
from strategies.base import TaskContext
from tools.http_client import Response

REMOTIVE = {"jobs": [{"id": 1, "url": "https://remotive.com/j/1", "title": "Senior Rust Engineer", "company_name": "Acme",
                      "category": "Software Development", "tags": ["rust", "aws"], "publication_date": "2026-09-28T10:00:00",
                      "candidate_required_location": "Europe", "salary": "$80k - $100k", "description": "<p>Rust</p>"},
                     {"id": 2, "title": "", "company_name": "Nope"}]}
JOBICY = {"jobs": [{"id": 9, "url": "https://jobicy.com/j/9", "jobTitle": "Python Developer", "companyName": "Beta",
                    "jobIndustry": ["Dev"], "jobType": ["full-time"], "jobGeo": "USA", "pubDate": "2026-09-27 08:00:00",
                    "annualSalaryMin": "90000", "annualSalaryMax": "120000", "jobExcerpt": "python django"}]}
HIMALAYAS = {"jobs": [{"title": "Go Engineer", "companyName": "Gamma", "applicationLink": "https://himalayas.app/j/3",
                       "guid": "h3", "locationRestrictions": ["United States"], "categories": ["Software"],
                       "seniority": ["Senior"], "minSalary": 100000, "maxSalary": 140000, "pubDate": 1790000000,
                       "description": "golang kubernetes"}]}
WWR = """<?xml version="1.0"?><rss><channel>
<item><title>Delta Corp: Full-Stack TypeScript Engineer</title><link>https://weworkremotely.com/j/4</link>
<guid>w4</guid><region>Anywhere</region><pubDate>Mon, 28 Sep 2026 10:00:00 +0000</pubDate>
<category>Programming</category><description>&lt;p&gt;react typescript&lt;/p&gt;</description></item>
<item><title>No colon here</title><link>x</link></item></channel></rss>"""


def test_parsers_read_each_board():
    r = js.parse_remotive(REMOTIVE)
    assert len(r) == 1 and (r[0].company, r[0].salary_min, r[0].salary_max, r[0].remote) == ("Acme", 80000, 100000, True)
    j = js.parse_jobicy(JOBICY)[0]
    assert (j.title, j.location, j.salary_max) == ("Python Developer", "USA", 120000)
    h = js.parse_himalayas(HIMALAYAS)[0]
    assert h.location == "United States" and h.posted_at.startswith("2026-09") and "senior" in h.tags
    w = js.parse_wwr(WWR)
    assert len(w) == 1 and (w[0].company, w[0].title) == ("Delta Corp", "Full-Stack TypeScript Engineer")
    assert "typescript" in enrich_lead(w[0]).stack and "rust" in enrich_lead(r[0]).stack
    assert set(js.EXTRA_FETCHERS) <= set(FETCHERS)


def test_rss_with_entities_is_refused():
    import pytest

    with pytest.raises(ValueError):
        js.parse_wwr('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><rss/>')


def test_boards_are_fetched_at_most_every_six_hours(toolkit, state, config, transport, clock, make_hypothesis):
    import json

    config.lead_sources = ["remotive"]
    transport.add("https://remotive.com/api/remote-jobs", Response(200, "u", json.dumps(REMOTIVE).encode(), {}))
    hyp = make_hypothesis(keywords=["rust"])
    agg = LeadAggregator()
    assert agg.run("aggregate_leads", TaskContext(toolkit, hyp, {})).metrics["fetched"] == 1
    calls = len(transport.calls_to("https://remotive.com/"))
    res = agg.run("aggregate_leads", TaskContext(toolkit, hyp, {}))
    assert res.ok and len(transport.calls_to("https://remotive.com/")) == calls  # waits its turn, not a failure
    clock.advance(hours=6 * 1.11)  # the cadence, with the board's fixed offset (Phase 298)
    agg.run("aggregate_leads", TaskContext(toolkit, hyp, {}))
    assert len(transport.calls_to("https://remotive.com/")) == calls + 1


def test_money_strings():
    assert js._money("$80k - $100k") == [80000, 100000]
    assert js._money("80,000-100,000 USD") == [80000, 100000]
    assert js._money("competitive") == []


def test_sources_page_credits_every_board_in_use(config):
    config.lead_sources = ["remoteok", "remotive"]
    page = js.sources_page(config, lambda t, b, d: b)
    assert 'href="https://remotive.com"' in page and "Remote OK" in page and "Jobicy" not in page


def test_a_failing_board_also_waits_its_turn(toolkit, state, config, transport, clock, make_hypothesis):
    config.lead_sources = ["remoteok", "remotive"]
    transport.add("https://remotive.com/api/remote-jobs", Response(500, "u", b"down", {}))
    hyp = make_hypothesis(keywords=["rust"])
    agg = LeadAggregator()
    agg.run("aggregate_leads", TaskContext(toolkit, hyp, {}))
    calls = len(transport.calls_to("https://remotive.com/"))
    assert calls >= 1
    agg.run("aggregate_leads", TaskContext(toolkit, hyp, {}))
    assert len(transport.calls_to("https://remotive.com/")) == calls  # not hammered every cycle
