import pytest

from strategies.b2b_lead_aggregator import (
    Lead, LeadAggregator, dedupe, dedupe_key, enrich_lead, matches_niche, parse_arbeitnow, parse_hn_comment,
    parse_remoteok, validate_lead,
)
from strategies.base import TaskContext
from tests.conftest import arbeitnow_payload, hn_items, remoteok_payload
from tools.circuit_breaker import CircuitBreaker
from tools.errors import CircuitOpenError
from tools.http_client import Response


def test_parse_remoteok_skips_legal_notice_and_cleans_html():
    leads = parse_remoteok(remoteok_payload())
    assert len(leads) == 4
    acme = leads[0]
    assert acme.company == "Acme Inc" and acme.remote
    assert acme.description == "Build APIs with Django & Postgres."
    assert acme.posted_at.startswith("2026-09-28")
    assert acme.salary_min == 120000
    assert leads[1].salary_min is None


def test_parse_arbeitnow():
    leads = parse_arbeitnow(arbeitnow_payload())
    assert [l.company for l in leads] == ["Delta GmbH", "ACME, Inc.", "Epsilon"]
    assert "full time" in leads[0].tags


def test_parse_hn_comment():
    _, item = hn_items()
    lead = parse_hn_comment(item["children"][0])
    assert lead.company == "Zeta Labs" and lead.title == "Staff Python Engineer"
    assert lead.remote and lead.contact_email == "jobs@zeta.example"
    assert lead.apply_url == "https://zeta.example/jobs"
    assert parse_hn_comment(item["children"][1]) is None
    assert parse_hn_comment({"text": ""}) is None


def test_validation_flags_bad_records():
    good = Lead("s", "1", "Acme", "Engineer", "https://x.example/job")
    assert validate_lead(good) == []
    assert "missing company" in validate_lead(Lead("s", "1", "", "Engineer", "https://x"))
    assert "invalid url" in validate_lead(Lead("s", "1", "Acme", "Engineer", "not-a-url"))
    assert "invalid email" in validate_lead(Lead("s", "1", "Acme", "Eng", "https://x", contact_email="bad@"))
    assert "salary range inverted" in validate_lead(Lead("s", "1", "Acme", "Eng", "https://x", salary_min=10, salary_max=5))


def test_enrichment():
    lead = enrich_lead(
        Lead("remoteok", "1", "Acme", "Senior Python Engineer", "https://remoteok.com/x",
             apply_url="https://www.acme.example/apply", tags=["django"], description="We use AWS and Postgres. Go-getters welcome.",
             location="Remote US", contact_email="Jobs@Acme.Example")
    )
    assert lead.seniority == "senior"
    assert {"python", "django", "aws", "postgres"} <= set(lead.stack)
    assert "golang" not in lead.stack
    assert lead.company_domain == "acme.example"
    assert lead.remote and lead.contact_email == "jobs@acme.example"
    assert enrich_lead(Lead("s", "1", "A", "Software Engineer", "https://x")).seniority == "mid"


def test_dedupe_normalizes_company_suffixes():
    a = Lead("remoteok", "1", "Acme Inc", "Senior Python Engineer", "https://a", location="Remote - US")
    b = Lead("arbeitnow", "2", "ACME, Inc.", "Senior  Python Engineer", "https://b", location="Remote - US", contact_email="x@acme.example")
    assert dedupe_key(a) == dedupe_key(b)
    result = dedupe([a, b])
    assert len(result) == 1 and result[0][1].contact_email == "x@acme.example"


def test_niche_matching_uses_word_boundaries():
    lead = enrich_lead(Lead("s", "1", "A", "Java Developer", "https://x", tags=["javascript"]))
    assert matches_niche(lead, ["java"])
    other = enrich_lead(Lead("s", "1", "A", "Frontend Dev", "https://x", tags=["javascript"]))
    assert not matches_niche(other, ["java"])


def test_strategy_end_to_end(toolkit, make_hypothesis, config):
    hyp = make_hypothesis()
    result = LeadAggregator().run("aggregate_leads", TaskContext(toolkit, hyp, {}))
    assert result.ok and not result.invalidates_hypothesis
    m = result.metrics
    assert m["failed_sources"] == []
    # Acme (x2 across boards -> 1), Beta, Delta, Zeta match python; Gamma/Eta don't; invalid ones dropped
    assert m["unique"] == 4 and m["new"] == 4 and m["total"] == 4
    companies = {l["company"] for l in toolkit.state.leads_for_niche("python-remote")}
    assert companies == {"Acme Inc", "Beta LLC", "Delta GmbH", "Zeta Labs"}
    assert toolkit.files.exists("exports/python-remote/leads.csv")
    rows = toolkit.files.read_csv("exports/python-remote/leads.csv")
    assert len(rows) == 4 and all(r["source"] for r in rows)

    # Second run: nothing new
    again = LeadAggregator().run("aggregate_leads", TaskContext(toolkit, hyp, {}))
    assert again.metrics["new"] == 0 and again.metrics["total"] == 4


def test_partial_source_failure_still_succeeds(toolkit, transport, make_hypothesis):
    transport.add("https://remoteok.com/api", Response(500, "u"))
    hyp = make_hypothesis()
    payload = {}
    result = LeadAggregator().run("aggregate_leads", TaskContext(toolkit, hyp, payload))
    assert result.ok and result.metrics["failed_sources"] == ["remoteok"]
    assert payload["failed_sources"] == ["remoteok"]
    assert toolkit.state.count_errors() > 0


def test_all_sources_failing_raises(toolkit, make_hypothesis):
    def boom(http):
        raise ConnectionError("down")

    agg = LeadAggregator(fetchers={"remoteok": boom, "arbeitnow": boom})
    with pytest.raises(RuntimeError):
        agg.run("aggregate_leads", TaskContext(toolkit, make_hypothesis(), {}))


def test_circuit_open_propagates(toolkit, make_hypothesis):
    def budget(http):
        raise CircuitOpenError("budget")

    with pytest.raises(CircuitOpenError):
        LeadAggregator(fetchers={"remoteok": budget}).run("aggregate_leads", TaskContext(toolkit, make_hypothesis(), {}))


def test_starving_niche_invalidates_hypothesis(toolkit, make_hypothesis):
    hyp = make_hypothesis("rust-systems", ["haskell"], iterations=2)
    result = LeadAggregator().run("aggregate_leads", TaskContext(toolkit, hyp, {}))
    assert result.metrics["total"] == 0
    assert result.invalidates_hypothesis


def test_new_niche_gets_grace_period(toolkit, make_hypothesis):
    hyp = make_hypothesis("rust-systems", ["haskell"], iterations=0)
    assert not LeadAggregator().run("aggregate_leads", TaskContext(toolkit, hyp, {})).invalidates_hypothesis


def test_degraded_retry_drops_failed_sources(toolkit, make_hypothesis):
    calls = []

    def ok(http):
        calls.append("ok")
        return []

    def bad(http):
        calls.append("bad")
        raise ConnectionError()

    toolkit.config.lead_sources = ["bad", "ok"]
    agg = LeadAggregator(fetchers={"bad": bad, "ok": ok})
    agg.run("aggregate_leads", TaskContext(toolkit, make_hypothesis(), {"failed_sources": ["bad"]}, attempt=2))
    assert calls == ["ok"]
