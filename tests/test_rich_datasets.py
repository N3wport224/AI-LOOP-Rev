"""Phases 95-99: changes file, top 20, regional slices, JSONL + SQL, demand-driven niches."""

import io
import json
import sqlite3
import zipfile

from agent.hypotheses import formulate_next
from strategies.base import TaskContext
from strategies.customer_requests import record, topics
from strategies.dataset_extras import changes_md, jsonl, region_split, schema_sql, top20_md
from strategies.digital_asset_packager import DigitalAssetPackager
from tests import test_business_ops

kit = test_business_ops.kit  # the same live-mode toolkit fixture
FIELDS = ["company", "title", "location", "stack"]


def test_changes_lists_new_and_gone_companies():
    assert "First release" in changes_md("py", 1, [{"company": "A"}], None)
    md = changes_md("py", 3, [{"company": "A"}, {"company": "C"}], [{"company": "A"}, {"company": "B"}])
    assert "## New companies (1)" in md and "- C" in md and "## No longer hiring here (1)" in md and "- B" in md


def test_top20_ranks_by_urgency_or_roles():
    intel = [{"company": "Low", "urgency_score": 10, "open_positions": ["a"]},
             {"company": "Hot", "urgency_score": 90, "open_positions": ["a", "b", "c"], "stack": ["aws", "python"]}]
    md = top20_md("py", [], intel)
    assert md.index("Hot") < md.index("Low") and "| 1 | Hot | 90 | 3 | aws, python |" in md
    md2 = top20_md("py", [{"company": "X"}, {"company": "X"}, {"company": "Y"}], [])
    assert "| 1 | X | 2 |" in md2


def test_regions_split_by_location():
    rows = [{"location": "New York, NY"}, {"location": "Berlin, Germany"}, {"location": "Remote - US", "remote": True},
            {"location": ""}, {"location": "Houston business park"}]
    split = region_split(rows)
    assert len(split["us"]) == 2 and len(split["europe"]) == 1 and len(split["remote"]) == 1


def test_jsonl_and_sql_load_as_is():
    rows = [{"company": "O'Reilly", "title": "Dev", "location": None, "stack": ["python", "aws"]}]
    assert json.loads(jsonl(rows, FIELDS).splitlines()[0])["company"] == "O'Reilly"
    db = sqlite3.connect(":memory:")
    db.executescript(schema_sql(rows, FIELDS))
    assert db.execute("SELECT company, location, stack FROM leads").fetchone() == ("O'Reilly", None, "python, aws")
    db.close()


def test_the_zip_has_the_new_files(kit, state, config):
    config.min_leads_for_asset = 2
    hid = state.create_hypothesis("lead_directory:py:g1", "lead_directory", "py", {"niche": "py"})
    hyp = {"id": hid, "params": {"niche": "py"}, "iterations": 0}
    for i in range(4):
        state.upsert_lead(f"k{i}", "py", {"company": f"Co{i}", "title": "Dev", "location": "London, UK" if i else "Remote",
                                          "remote": i == 0})
    DigitalAssetPackager().run("package_asset", TaskContext(kit, hyp, {}))
    state.upsert_lead("k9", "py", {"company": "Newco", "title": "Dev", "location": "Austin, TX, United States"})
    res = DigitalAssetPackager().run("package_asset", TaskContext(kit, hyp, {}))
    zf = zipfile.ZipFile(io.BytesIO(kit.files.read_bytes(res.metrics["zip"])))
    names = {n.split("/", 1)[1] for n in zf.namelist()}
    assert {"CHANGES.md", "TOP20.md", "leads.jsonl", "schema.sql", "regions/leads-europe.csv", "regions/leads-us.csv",
            "regions/leads-remote.csv"} <= names
    assert "- Newco" in zf.read("py-intel/CHANGES.md").decode()


def test_what_customers_ask_for_becomes_the_next_niche(state, config):
    config.niches = [{"name": "python-remote", "keywords": ["python"]}, {"name": "rust-systems", "keywords": ["rust"]}]
    for i in range(2):
        state.upsert_lead(f"k{i}", "python-remote", {"company": "A", "title": "Dev", "tags": ["svelte", "python"]})
    assert topics(config, "any svelte data?", vocab=["svelte"]) == ["svelte"]
    assert record(state, config, "a@co.example", "Do you have a dataset for Rust?")
    first = formulate_next(state, config)["params"]
    assert first["niche"] == "python-remote"  # one ask isn't enough: the usual order
    record(state, config, "b@co.example", "Could you add Rust please?")
    nxt = formulate_next(state, config)["params"]
    assert nxt["niche"] == "rust-systems" and "asked for by 2 customers" in nxt["origin"]
