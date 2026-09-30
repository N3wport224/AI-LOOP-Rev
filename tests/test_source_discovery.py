"""Autonomous source discovery: SSRF guard, adapters, sandbox probes, lifecycle and live ingestion."""

import json
import socket

import pytest

from agent import source_discovery as sd
from strategies.b2b_lead_aggregator import POOL_NICHE, LeadAggregator
from strategies.base import TaskContext
from tools.http_client import Response

PUBLIC = "93.184.216.34"


def fake_dns(mapping=None):
    mapping = mapping or {}

    def resolve(host, port, proto=0):
        ip = mapping.get(host, PUBLIC)
        if ip is None:
            raise socket.gaierror("no such host")
        return [(socket.AF_INET, socket.SOCK_STREAM, proto, "", (ip, port))]
    return resolve


@pytest.fixture(autouse=True)
def dns(monkeypatch):
    monkeypatch.setattr(sd, "RESOLVER", fake_dns({"internal.example.com": "10.0.0.5", "meta.example.com": "169.254.169.254",
                                                  "gone.example.com": None}))


GREENHOUSE = {"jobs": [
    {"id": 1, "title": "Senior Platform Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
     "location": {"name": "Remote - US"}, "updated_at": "2026-09-28T10:00:00Z",
     "content": "We are moving from our on-prem data center to AWS. Kubernetes, Terraform, Python."},
    {"id": 2, "title": "Data Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/2",
     "location": {"name": "NYC"}, "updated_at": "2026-09-27T10:00:00Z", "content": "Snowflake, dbt, Airflow. SOC 2 experience."},
    {"id": 3, "title": "", "absolute_url": "https://boards.greenhouse.io/acme/jobs/3", "location": {"name": "x"}},
]}
LEVER = [{"id": "a1", "text": "Founding Engineer", "hostedUrl": "https://jobs.lever.co/beta/a1", "createdAt": 1790000000000,
          "categories": {"location": "Remote", "team": "Eng"}, "descriptionPlain": "Rust, Go, first DevOps hire vibes"}]
ASHBY = {"jobs": [{"id": "z", "title": "Security Engineer", "jobUrl": "https://jobs.ashbyhq.com/gamma/z", "location": "Berlin",
                   "publishedAt": "2026-09-20T00:00:00Z", "descriptionPlain": "Lead our FedRAMP program", "isRemote": False}]}
RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Jobs</title>
<item><title>Delta Corp: Staff SRE</title><link>https://jobs.example.com/1</link><guid>1</guid>
<pubDate>Mon, 28 Sep 2026 10:00:00 +0000</pubDate><description>Kubernetes migration, Terraform</description></item>
<item><title>Backend Developer at Epsilon</title><link>https://jobs.example.com/2</link><guid>2</guid>
<description>Python and Postgres</description></item></channel></rss>"""


# ------------------------------------------------------------------ safety
@pytest.mark.parametrize("url,problem", [
    ("http://boards-api.greenhouse.io/v1/boards/acme/jobs", "https"),
    ("https://127.0.0.1/jobs.json", "IP-address"),
    ("https://[::1]/jobs.json", "IP-address"),
    ("https://localhost/feed", "internal"),
    ("https://printer.local/feed", "internal"),
    ("https://internal.example.com/feed", "public address"),     # resolves to 10.0.0.5
    ("https://meta.example.com/latest", "public address"),        # resolves to the cloud metadata address
    ("https://gone.example.com/feed", "public address"),
    ("https://user:pw@jobs.example.com/feed", "credentials"),
    ("https://jobs.example.com:8443/feed", "port"),
])
def test_ssrf_guard(url, problem):
    assert problem in (sd.url_problem(url) or "")


def test_public_urls_pass():
    assert sd.url_problem("https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true") is None


# ------------------------------------------------------------------ discovery inputs
def test_ats_boards_are_found_in_collected_urls():
    got = sd.ats_candidates(["https://boards.greenhouse.io/acme/jobs/123", "https://job-boards.greenhouse.io/Acme",
                             "https://jobs.lever.co/beta-co/abc", "https://jobs.ashbyhq.com/gamma/xyz", "https://acme.com/careers", ""])
    assert got == [
        ("ashby:gamma", "ashby", "https://api.ashbyhq.com/posting-api/job-board/gamma", "Gamma"),
        ("greenhouse:acme", "greenhouse", "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true", "Acme"),
        ("lever:beta-co", "lever", "https://api.lever.co/v0/postings/beta-co?mode=json", "Beta Co"),
    ]


def test_rss_autodiscovery():
    html = ('<html><head><link rel="alternate" type="application/rss+xml" href="/jobs.rss">'
            '<link rel="stylesheet" href="x.css"><link rel="alternate" type="application/atom+xml" href="https://x.example/a.atom">'
            "</head></html>")
    assert sd.autodiscover_feeds(html, "https://jobs.example.com/") == ["https://jobs.example.com/jobs.rss", "https://x.example/a.atom"]
    assert sd.autodiscover_feeds("<<<not html", "https://x.example/") == []


# ------------------------------------------------------------------ adapters
def test_adapters():
    gh = sd.parse("greenhouse", json.dumps(GREENHOUSE).encode(), "greenhouse:acme", "Acme")
    assert [lead.title for lead in gh] == ["Senior Platform Engineer", "Data Engineer", ""]
    assert gh[0].company == "Acme" and gh[0].remote and gh[0].posted_at == "2026-09-28T10:00:00+00:00"
    lv = sd.parse("lever", json.dumps(LEVER).encode(), "lever:beta", "Beta")
    assert lv[0].title == "Founding Engineer" and lv[0].posted_at.startswith("2026-09-21")
    ab = sd.parse("ashby", json.dumps(ASHBY).encode(), "ashby:gamma", "Gamma")
    assert ab[0].company == "Gamma" and not ab[0].remote
    rss = sd.parse("rss", RSS, "rss:jobs", "")
    assert [(lead.company, lead.title) for lead in rss] == [("Delta Corp", "Staff SRE"), ("Epsilon", "Backend Developer")]
    assert rss[0].posted_at == "2026-09-28T10:00:00+00:00"  # RFC 2822 pubDate
    generic = sd.parse("json", json.dumps({"jobs": [{"id": 9, "title": "SRE", "company_name": "Zeta", "url": "https://z.example/9",
                                                     "description": "Terraform", "publication_date": "2026-09-29"}]}).encode(), "json:x", "")
    assert generic[0].company == "Zeta" and generic[0].posted_at.startswith("2026-09-29")
    for kind, body in [("greenhouse", b'{"nope": 1}'), ("lever", b"{}"), ("json", b'{"a": 1}'), ("rss", b"<html></html>")]:
        with pytest.raises(ValueError):
            sd.parse(kind, body, "x", "")
    with pytest.raises(ValueError):
        sd.parse("rss", b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]><rss>&lol;</rss>', "x", "")


# ------------------------------------------------------------------ sandbox probe
def source(url, kind="greenhouse", name="greenhouse:acme", company="Acme"):
    return {"id": 1, "url": url, "kind": kind, "name": name, "notes": json.dumps({"company": company})}


API = "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"


def test_probe_measures_yield(toolkit, transport):
    transport.add_json(API, GREENHOUSE)
    r = sd.probe(toolkit.http, source(API))
    assert r["ok"] and (r["items"], r["valid"], r["signals"]) == (3, 2, 2) and r["stacked"] == 2
    assert {lead.source for lead in r["leads"]} == {"greenhouse:acme"}


def test_probe_rejects_unsafe_or_unfriendly_sources(toolkit, transport):
    transport.add("https://boards-api.greenhouse.io/robots.txt", Response(200, "u", b"User-agent: *\nDisallow: /v1/"))
    r = sd.probe(toolkit.http, source(API))
    assert not r["ok"] and r["fatal"] and "robots" in r["error"]
    assert sd.probe(toolkit.http, source("https://internal.example.com/jobs"))["fatal"]
    transport.add("https://api.lever.co/v0/postings/beta", Response(429, "u", b"slow down", {"retry-after": "60"}))
    r = sd.probe(toolkit.http, source("https://api.lever.co/v0/postings/beta?mode=json", "lever", "lever:beta"))
    assert not r["ok"] and ("429" in r["error"] or "rate limited" in r["error"])
    transport.add("https://api.ashbyhq.com/", Response(200, "u", b"x" * (sd.MAX_BYTES + 1)))
    r = sd.probe(toolkit.http, source("https://api.ashbyhq.com/posting-api/job-board/big", "ashby", "ashby:big"))
    assert not r["ok"] and "too large" in r["error"]
    transport.add_json("https://api.ashbyhq.com/posting-api/job-board/odd", {"unexpected": True})
    r = sd.probe(toolkit.http, source("https://api.ashbyhq.com/posting-api/job-board/odd", "ashby", "ashby:odd"))
    assert not r["ok"] and "structure" in r["error"]


# ------------------------------------------------------------------ lifecycle
@pytest.fixture
def discovery(config, toolkit, transport, make_hypothesis):
    config.source_discovery_enabled = True
    toolkit.files.write_json("exports/intel/python-remote/tech_radar.json",
                             [{"company": "Acme", "careers_url": "https://boards.greenhouse.io/acme/jobs/1"}])
    transport.add_json(API, GREENHOUSE)
    return toolkit, TaskContext(toolkit, make_hypothesis(), {})


def test_a_good_source_goes_candidate_trial_active_over_three_days(discovery, state, clock, config):
    kit, ctx = discovery
    reg = sd.SourceRegistry(state, config)
    res = sd.SourceDiscovery().run("discover_sources", ctx)
    assert res.metrics["added"]["ats"] == 1 and res.metrics["added"]["seed"] == len(sd.SEED_FEEDS)
    assert reg.get("greenhouse:acme")["status"] == "trial"
    assert sd.SourceDiscovery().run("discover_sources", ctx).metrics == {"due": False}  # once per interval
    clock.advance(hours=25)
    sd.SourceDiscovery().run("discover_sources", ctx)
    assert reg.get("greenhouse:acme")["status"] == "trial"  # 2 healthy days
    clock.advance(hours=25)
    res = sd.SourceDiscovery().run("discover_sources", ctx)
    assert reg.get("greenhouse:acme")["status"] == "active"
    assert "greenhouse:acme: trial → active" in res.summary
    # the seed feeds answered 404 three days running: rejected, never ingested
    assert {s["status"] for s in reg.all() if s["name"].startswith(("weworkremotely", "remotive", "jobicy"))} == {"rejected"}
    h = reg.health(reg.get("greenhouse:acme")["id"])
    assert h["healthy_days"] == 3 and h["error_rate"] == 0 and h["valid_ratio"] == pytest.approx(2 / 3, abs=1e-3)


def test_active_sources_feed_the_aggregator_without_a_restart(discovery, state, clock, config):
    kit, ctx = discovery
    for _ in range(3):
        sd.SourceDiscovery().run("discover_sources", ctx)
        clock.advance(hours=25)
    assert sd.SourceRegistry(state, config).get("greenhouse:acme")["status"] == "active"
    res = LeadAggregator().run("aggregate_leads", ctx)  # the same process, a new cycle
    assert res.metrics["from_discovered_sources"] == 2
    pooled = [lead for lead in state.leads_for_niche(POOL_NICHE) if lead["source"] == "greenhouse:acme"]
    assert {lead["title"] for lead in pooled} == {"Senior Platform Engineer", "Data Engineer"}


def test_failing_and_degrading_sources(discovery, state, clock, config, transport):
    kit, ctx = discovery
    reg = sd.SourceRegistry(state, config)
    transport.add(API, Response(500, "u", b"down"))
    for _ in range(3):
        sd.SourceDiscovery().run("discover_sources", ctx)
        clock.advance(hours=25)
    assert reg.get("greenhouse:acme")["status"] == "rejected"
    # an active source whose recent runs mostly fail is suspended, and re-trialled a week later
    sid = reg.get("greenhouse:acme")["id"]
    reg.set_status(sid, "active")
    for ok in (True, True, False, False, False, False):
        reg.record_run(sid, "ingest", ok, 3 if ok else 0, 2 if ok else 0, 1 if ok else 0)
    assert sd.evaluate(reg, reg.get("greenhouse:acme"), config) == "suspended"
    clock.advance(days=8)
    state.set("source_discovery_last", None)
    sd.SourceDiscovery().run("discover_sources", ctx)
    assert reg.get("greenhouse:acme")["status"] in ("trial", "rejected")


def test_active_cap_and_no_intent_no_activation(discovery, state, clock, config, transport):
    kit, ctx = discovery
    reg = sd.SourceRegistry(state, config)
    boring = {"jobs": [{"id": i, "title": "Engineer", "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{i}",
                        "location": {"name": "x"}, "content": "Python"} for i in range(5)]}
    transport.add_json(API, boring)
    for _ in range(4):
        sd.SourceDiscovery().run("discover_sources", ctx)
        clock.advance(hours=25)
    assert reg.get("greenhouse:acme")["status"] == "trial"  # healthy, but no buying-intent signal yet
    transport.add_json(API, GREENHOUSE)
    config.source_max_active = 0
    sd.SourceDiscovery().run("discover_sources", ctx)
    assert reg.get("greenhouse:acme")["status"] == "trial"  # qualified, waiting for a slot


def test_disabled(discovery, config):
    kit, ctx = discovery
    config.source_discovery_enabled = False
    assert "disabled" in sd.SourceDiscovery().run("discover_sources", ctx).summary
    assert LeadAggregator().run("aggregate_leads", ctx).metrics["from_discovered_sources"] == 0
