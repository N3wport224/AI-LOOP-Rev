from datetime import timedelta

from strategies.b2b_lead_aggregator import LeadAggregator
from strategies.base import TaskContext
from strategies.tech_stack_intel import (
    TechStackIntel, build_company_records, fingerprint, intent_signals, render_tech_radar_md, urgency_score,
)
from tests.conftest import NOW
from tools.http_client import Response


def test_fingerprint_categories_and_boundaries():
    fp = fingerprint("We run Django on AWS with PostgreSQL, Snowflake and dbt; deploy with Kubernetes (k8s) and Terraform. JavaScript/React frontend.")
    assert fp["frameworks"] == ["Django", "React"]
    assert fp["cloud"] == ["AWS"]
    assert fp["databases"] == ["PostgreSQL"]
    assert set(fp["data_platform"]) == {"Snowflake", "dbt"}
    assert set(fp["infrastructure"]) == {"Kubernetes", "Terraform"}
    assert "Java" not in fp.get("languages", [])  # "JavaScript" must not match Java
    assert fingerprint("Go-getters welcome, let's go!").get("languages") is None
    assert fingerprint("Senior Go engineer")["languages"] == ["Go"]


def test_intent_signals():
    text = ("We're migrating our legacy monolith to microservices and building a new team. "
            "Integration with SAP ERP. Series B. Start ASAP.")
    assert set(intent_signals(text)) == {"migration", "legacy_refactor", "new_team", "erp_integration", "recent_funding", "urgent_hire"}
    assert intent_signals("Maintain our stable product.") == []


def test_urgency_score_components_and_clamp():
    assert urgency_score([], 1, None, False) == 0
    assert urgency_score(["migration"], 1, 3, False) == 20 + 15
    assert urgency_score(["migration", "migration"], 3, 10, True) == 20 + 10 + 8 + 5
    assert urgency_score(list(["migration", "legacy_refactor", "erp_integration", "new_team", "urgent_hire"]), 10, 1, True) == 100


def _lead(company, title, desc="", days=1, **kw):
    d = {"company": company, "title": title, "description": desc, "tags": [], "url": f"https://remoteok.com/{company}",
         "posted_at": (NOW - timedelta(days=days)).isoformat(), "source": "remoteok"}
    d.update(kw)
    return d


def test_company_aggregation_and_ranking():
    leads = [
        _lead("Acme Inc", "Senior Python Engineer", "Migrating from legacy PHP to Django on AWS", company_domain="acme.example"),
        _lead("ACME", "Data Engineer", "Snowflake + dbt", apply_url="https://acme.example/jobs/2"),
        _lead("Quiet Co", "Backend Engineer", "Postgres", days=30),
    ]
    records = build_company_records(leads, NOW)
    assert [r["company"] for r in records] == ["Acme Inc", "Quiet Co"]
    acme = records[0]
    assert acme["openings"] == 2 and acme["domain"] == "acme.example"
    assert {"migration", "legacy_refactor"} <= set(acme["intent_signals"])
    assert {"Django", "AWS", "Snowflake", "dbt"} <= set(acme["stack"])
    assert acme["careers_candidates"][0] == "https://acme.example/jobs/2"
    assert "https://acme.example/careers" in acme["careers_candidates"]
    assert records[1]["urgency_score"] == 0


def test_strategy_builds_outputs_and_verifies_careers_urls(toolkit, transport, make_hypothesis):
    hyp = make_hypothesis()
    LeadAggregator().run("aggregate_leads", TaskContext(toolkit, hyp, {}))
    transport.add("https://acme.example/careers", Response(200, "u", b"ok"))
    transport.add("https://acme.example/jobs", Response(200, "u", b"ok"))
    res = TechStackIntel().run("build_intel", TaskContext(toolkit, hyp, {}))
    assert res.ok and res.metrics["companies"] == 4
    records = toolkit.files.read_json("exports/intel/python-remote/tech_radar.json")
    acme = next(r for r in records if r["company"] == "Acme Inc")
    assert acme["careers_url_verified"] and acme["careers_url"].startswith("https://acme.example/")
    assert "careers_candidates" not in acme
    beta = next(r for r in records if r["company"] == "Beta LLC")
    assert not beta["careers_url_verified"] and beta["careers_url"]  # best guess, flagged
    rows = toolkit.files.read_csv("exports/intel/python-remote/tech_radar.csv")
    assert set(rows[0]) >= {"company", "domain", "urgency_score", "careers_url", "careers_url_verified", "stack"}
    md = toolkit.files.read_text("exports/intel/python-remote/EXECUTIVE_TECH_RADAR.md")
    assert "# Executive Tech Radar: Python Remote" in md and "## Top 10 by hiring urgency" in md
    # results cached: a second run makes no new careers requests
    before = len(transport.calls)
    TechStackIntel().run("build_intel", TaskContext(toolkit, hyp, {}))
    assert len(transport.calls) == before


def test_url_check_budget_and_degraded_mode(toolkit, transport, make_hypothesis):
    hyp = make_hypothesis()
    LeadAggregator().run("aggregate_leads", TaskContext(toolkit, hyp, {}))
    toolkit.config.intel_max_url_checks = 1
    before = len(transport.calls)
    TechStackIntel().run("build_intel", TaskContext(toolkit, hyp, {}))
    assert len(transport.calls) - before == 1
    toolkit.state.set("careers_url_cache", {})
    before = len(transport.calls)
    TechStackIntel().run("build_intel", TaskContext(toolkit, hyp, {}, attempt=2))
    assert len(transport.calls) == before  # degraded retries skip verification entirely


def test_radar_markdown_watchlist():
    recs = build_company_records([_lead("Mig", "Engineer", "migration to GCP")], NOW)
    md = render_tech_radar_md("X", recs, "now", 60)
    assert "## Migration watchlist" in md and "**Mig**" in md


def test_packager_bundles_intel(toolkit, make_hypothesis):
    import io
    import zipfile

    from strategies.digital_asset_packager import DigitalAssetPackager

    hyp = make_hypothesis()
    LeadAggregator().run("aggregate_leads", TaskContext(toolkit, hyp, {}))
    TechStackIntel().run("build_intel", TaskContext(toolkit, hyp, {}))
    res = DigitalAssetPackager().run("package_asset", TaskContext(toolkit, hyp, {}))
    names = zipfile.ZipFile(io.BytesIO(toolkit.files.read_bytes(res.metrics["zip"]))).namelist()
    assert "python-remote-intel/EXECUTIVE_TECH_RADAR.md" in names and "python-remote-intel/tech_radar.csv" in names
    sample = toolkit.files.read_json("assets/python-remote/v1/sample.json")
    assert sample["fields"][:3] == ["company", "domain", "urgency_score"]
    assert all(set(r) == set(sample["fields"]) for r in sample["rows"])
    listing = toolkit.files.read_json("assets/python-remote/v1/listing.json")
    assert listing["description_markdown"].startswith("# Executive Tech Radar")
