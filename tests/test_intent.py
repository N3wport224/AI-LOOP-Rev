"""Commercial buying-intent extraction: migration, compliance and leadership signals, scoring, surfacing."""

import csv
import io

import pytest

from strategies.b2b_lead_aggregator import LeadAggregator
from strategies.base import TaskContext
from strategies.tech_stack_intel import (
    INTEL_FIELDS, TechStackIntel, build_company_records, commercial_intent, render_tech_radar_md, resolve_tech,
)
from tests.conftest import NOW


@pytest.mark.parametrize("text,label,path", [
    ("We are moving from Snowflake to BigQuery this year.", "Database Migration", "Snowflake → BigQuery"),
    ("You'll lead our legacy Oracle to Postgres migration.", "Database Migration", "Oracle → PostgreSQL"),
    ("Own the Kubernetes migration for 40 services.", "Kubernetes Migration", ""),
    ("Help us migrate to AWS by Q3.", "Cloud Migration", ""),
    ("We're migrating off our on-prem data center.", "Cloud Migration", ""),
    ("Transitioning from Heroku to ECS", "Platform Migration", "Heroku → ECS"),
    ("Modernizing legacy systems across the org.", "Legacy Modernization", ""),
    ("Help us achieve SOC 2 Type II this year.", "SOC 2 Compliance", ""),
    ("Experience with HIPAA hardening.", "HIPAA Compliance", ""),
    ("Lead our FedRAMP Moderate authorization.", "FedRAMP Authorization", ""),
    ("Join as a Founding Engineer.", "Founding Engineer", ""),
    ("You'll be our first DevOps hire.", "First DevOps Hire", ""),
    ("Head of Infrastructure (remote)", "Head of Infrastructure", ""),
])
def test_signal_families(text, label, path):
    r = commercial_intent(text)
    assert label in r["commercial_signals"], r
    assert r["migration_path"] == path
    assert r["intent_score"] > 0 and r["intent_tag"].startswith("Urgency: ")


@pytest.mark.parametrize("text", [
    "We are moving from junior to senior roles quickly.",
    "Switching to a new team structure next year.",
    "Maintain our stable, well-tested product.",
    "Migrating birds are our office mascot.",
    "Our first customers love us.",
])
def test_no_false_positives(text):
    assert commercial_intent(text)["intent_score"] == 0
    assert commercial_intent(text)["intent_tag"] == ""


def test_resolve_tech_handles_articles_and_legacy_names():
    assert resolve_tech("our legacy Oracle") == "Oracle"
    assert resolve_tech("the cloud") == "Cloud"
    assert resolve_tech("Postgres") == "PostgreSQL"
    assert resolve_tech("self-hosted") == "On-prem"
    assert resolve_tech("junior") is None


def test_score_rewards_explicit_paths_freshness_openings_and_breadth():
    one_sided = commercial_intent("Help us migrate to AWS")["intent_score"]
    explicit = commercial_intent("Migrating from Oracle to AWS")["intent_score"]
    assert explicit > one_sided
    base = commercial_intent("Migrating from Snowflake to BigQuery")
    fresh = commercial_intent("Migrating from Snowflake to BigQuery", openings=4, freshest_age_days=2)
    assert fresh["intent_score"] > base["intent_score"]
    stacked = commercial_intent("Migrating from Snowflake to BigQuery. Achieve SOC 2. Founding engineer.", 4, 2)
    assert stacked["intent_score"] > fresh["intent_score"]
    assert stacked["intent_level"] == "High" and stacked["intent_tag"] == "Urgency: High (Database Migration)"
    assert stacked["intent_score"] <= 100
    pursuing = commercial_intent("Working toward SOC 2 certification")["intent_score"]
    mention = commercial_intent("Familiarity with SOC 2 controls")["intent_score"]
    assert pursuing > mention


def test_cloud_migration_tag_format():
    r = commercial_intent("We're moving from our on-prem data center to AWS.", openings=3, freshest_age_days=1)
    assert r["intent_tag"] == "Urgency: High (Cloud Migration)"
    assert r["migration_path"] == "On-prem → AWS" and r["intent_category"] == "migration"
    assert r["intent_evidence"] and all(len(e) <= 60 for e in r["intent_evidence"])


def _lead(company, title, desc, days=2):
    return {"company": company, "title": title, "description": desc, "tags": [], "posted_at": (NOW.replace(day=30 - days)).isoformat()}


def test_company_records_carry_intent_and_radar_surfaces_it():
    leads = [
        _lead("Acme", "Data Engineer", "We are moving from Snowflake to BigQuery with dbt."),
        _lead("Acme", "Platform Engineer", "Kubernetes and Terraform."),
        _lead("Beta", "Founding Engineer", "Python and Postgres. Help us achieve SOC 2."),
        _lead("Gamma", "Backend Engineer", "Maintain our Django app."),
    ]
    recs = {r["company"]: r for r in build_company_records(leads, NOW)}
    assert recs["Acme"]["migration_path"] == "Snowflake → BigQuery"
    assert recs["Acme"]["intent_tag"].endswith("(Database Migration)")
    assert recs["Beta"]["intent_category"] in ("leadership", "compliance")
    assert recs["Gamma"]["intent_score"] == 0 and recs["Gamma"]["intent_tag"] == ""
    md = render_tech_radar_md("Python Remote", list(recs.values()), NOW.isoformat(), 60)
    assert "## Commercial buying intent" in md and "Snowflake → BigQuery" in md
    assert "**2 companies** show commercial buying intent" in md
    assert "| Company | Urgency | Intent |" in md


def test_intent_fields_lead_the_csv_export_and_preview(toolkit, make_hypothesis):
    hyp = make_hypothesis()
    LeadAggregator().run("aggregate_leads", TaskContext(toolkit, hyp, {}))
    TechStackIntel().run("build_intel", TaskContext(toolkit, hyp, {}))
    rows = list(csv.DictReader(io.StringIO(toolkit.files.read_text("exports/intel/python-remote/tech_radar.csv"))))
    assert list(rows[0])[:4] == ["company", "domain", "intent_tag", "intent_score"]
    assert INTEL_FIELDS.index("intent_tag") < INTEL_FIELDS.index("urgency_score")
    zeta = next(r for r in rows if r["company"] == "Zeta Labs")  # the HN fixture mentions Kafka and Django only
    assert zeta["intent_score"] == "0"
