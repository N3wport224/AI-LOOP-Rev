import io
import zipfile
from datetime import timedelta

from strategies.b2b_lead_aggregator import LeadAggregator
from strategies.base import TaskContext
from strategies.digital_asset_packager import DigitalAssetPackager, listing_title, niche_title
from strategies.outreach_stager import OPT_OUT, OutreachStager, draft_pitch, score_message
from tests.conftest import NOW


def _seed(toolkit, hyp):
    LeadAggregator().run("aggregate_leads", TaskContext(toolkit, hyp, {}))


def test_titles():
    assert niche_title("python-remote") == "Python Remote"
    assert niche_title("ml-ai") == "ML AI"
    assert listing_title("devops-sre") == "Devops SRE Tech Stack Intel"


def test_packager_skips_below_minimum(toolkit, make_hypothesis):
    result = DigitalAssetPackager().run("package_asset", TaskContext(toolkit, make_hypothesis(), {}))
    assert result.ok and result.metrics["built"] is False


def test_packager_builds_versioned_bundle(toolkit, make_hypothesis):
    hyp = make_hypothesis()
    _seed(toolkit, hyp)
    packager = DigitalAssetPackager()
    result = packager.run("package_asset", TaskContext(toolkit, hyp, {}))
    assert result.metrics["built"] and result.metrics["version"] == 1
    zip_bytes = toolkit.files.read_bytes(result.metrics["zip"])
    names = zipfile.ZipFile(io.BytesIO(zip_bytes)).namelist()
    assert {f"python-remote-intel/{n}" for n in ("README.md", "directory.md", "leads.csv", "leads.json", "ATTRIBUTION.md")} == set(names)
    readme = toolkit.files.read_text("assets/python-remote/v1/README.md")
    assert "4 roles across 4 companies" in readme
    listing = toolkit.files.read_json("assets/python-remote/v1/listing.json")
    assert listing["name"] == "Python Remote Tech Stack Intel"
    assert listing["price_cents"] == 500  # 4 companies -> lowest tier
    sample = toolkit.files.read_json("assets/python-remote/v1/sample.json")
    assert len(sample["rows"]) == 4 and all("contact_email" not in r for r in sample["rows"])

    # unchanged data -> no new version
    again = packager.run("package_asset", TaskContext(toolkit, hyp, {}))
    assert again.metrics["built"] is False and again.metrics["version"] == 1

    # new lead -> v2
    toolkit.state.upsert_lead("newkey", "python-remote", {"company": "New Co", "title": "Python Dev", "url": "https://n", "stack": ["python"]})
    v2 = packager.run("package_asset", TaskContext(toolkit, hyp, {}))
    assert v2.metrics["version"] == 2
    assert len(toolkit.state.list_assets(hyp["id"])) == 2


def test_packager_links_gumroad_product_by_title(toolkit, transport, make_hypothesis):
    toolkit.revenue.gumroad_token = "tok"
    transport.add_json(
        "https://api.gumroad.com/v2/products",
        {"success": True, "products": [{"id": "prod_42", "name": "Python Remote Tech Stack Intel"}]},
    )
    hyp = make_hypothesis()
    _seed(toolkit, hyp)
    result = DigitalAssetPackager().run("package_asset", TaskContext(toolkit, hyp, {}))
    assert result.metrics["product_linked"]
    assert toolkit.state.hypothesis_for_product("prod_42") == hyp["id"]


def test_score_message():
    lead = {"company": "Acme", "title": "Python Engineer", "stack": ["python"]}
    subject, body = draft_pitch(lead, sender_name="Sam", sender_email="s@x", skills=["python"], offer="contract help", now=NOW)
    assert score_message(body, lead) >= 0.8
    assert OPT_OUT in body
    assert score_message(body + " Act now!!!", lead) == 0.0


def test_outreach_stages_for_review_only(toolkit, make_hypothesis):
    hyp = make_hypothesis()
    _seed(toolkit, hyp)
    DigitalAssetPackager().run("package_asset", TaskContext(toolkit, hyp, {}))
    result = OutreachStager().run("stage_outreach", TaskContext(toolkit, hyp, {}))
    queue = toolkit.state.list_outreach()
    assert result.metrics["staged"] == len(queue) == 2  # 1 submission package + Zeta (only lead with a public email)
    assert {q["status"] for q in queue} == {"pending_review"}
    email = next(q for q in queue if q["channel"] == "email")
    assert email["recipient"] == "jobs@zeta.example"
    assert "Zeta Labs" in email["body"] and "Staff Python Engineer" in email["body"] and "Sam Dev" in email["body"]
    assert next(q for q in queue if q["channel"] == "submission")["recipient"] == "community"

    # Idempotent: rerun stages nothing new
    assert OutreachStager().run("stage_outreach", TaskContext(toolkit, hyp, {})).metrics["staged"] == 0


def test_outreach_respects_cooldown_across_hypotheses(toolkit, make_hypothesis):
    hyp = make_hypothesis()
    _seed(toolkit, hyp)
    OutreachStager().run("stage_outreach", TaskContext(toolkit, hyp, {}))
    other = make_hypothesis("python-alt")
    for lead in toolkit.state.leads_for_niche("python-remote"):
        toolkit.state.upsert_lead(lead["dedupe_key"], "python-alt", lead)
    res = OutreachStager().run("stage_outreach", TaskContext(toolkit, other, {}))
    assert res.metrics["staged"] == 0 and res.metrics["skipped"] >= 1


def test_outreach_daily_cap_and_stale_filter(toolkit, make_hypothesis, clock):
    toolkit.config.outreach_daily_cap = 2
    hyp = make_hypothesis()
    toolkit.state.upsert_lead("old", "python-remote", {
        "company": "Old", "title": "Python Engineer", "url": "https://x", "stack": ["python"],
        "contact_email": "old@co.example", "posted_at": (NOW - timedelta(days=90)).isoformat(),
    })
    for i in range(5):
        toolkit.state.upsert_lead(f"k{i}", "python-remote", {
            "company": f"Co{i}", "title": "Python Engineer", "url": "https://x", "stack": ["python"],
            "contact_email": f"hr{i}@co.example", "posted_at": (NOW - timedelta(days=1)).isoformat(),
        })
    res = OutreachStager().run("stage_outreach", TaskContext(toolkit, hyp, {}))
    assert res.metrics["staged"] == 2 and res.metrics["skipped"] == 1
    assert "old@co.example" not in {q["recipient"] for q in toolkit.state.list_outreach()}
    clock.advance(days=1)
    assert OutreachStager().run("stage_outreach", TaskContext(toolkit, hyp, {})).metrics["staged"] == 2
