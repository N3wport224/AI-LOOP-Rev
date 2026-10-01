"""Phases 230-234: still-listed postings, one name per company, no agencies in company lists, sources, currency."""

import io
import zipfile

from strategies import posting_quality as pq
from strategies import product_factory as pf
from strategies import product_types as pt
from tests import test_business_ops
from tests.test_product_factory import postings, unique_links

kit = test_business_ops.kit  # the same live-mode toolkit fixture


# ------------------------------------------------------------------ Phase 230
def test_postings_no_longer_listed_are_left_out(state, clock):
    postings(state, clock, 5, ["rust"], prefix="gone")
    clock.advance(days=pq.STALE_DAYS + 1)
    postings(state, clock, 5, ["rust"], prefix="here")  # the boards are still being read
    keys = {lead["company"].split()[0] for lead in pf.fresh_leads(state, 90)}
    assert keys == {"Here"}


def test_an_agent_that_was_off_keeps_its_data(state, clock):
    postings(state, clock, 5, ["rust"])
    clock.advance(days=40)  # nothing fetched since: measured against the newest posting, not today
    assert len(pf.fresh_leads(state, 90)) == 5


# ------------------------------------------------------------------ Phase 231
def test_company_name_variants_are_one_company():
    assert pq.company_key("Acme, Inc.") == pq.company_key("ACME Inc") == pq.company_key("Acme GmbH") == "acme"
    assert pq.company_key("AT&T") == "at&t" and pq.company_key("Co") == "co"
    rows = [{"company": n, "title": "Engineer", "location": "Berlin", "stack": ["rust"], "posted_at": "2026-09-30"}
            for n in ("Acme Inc", "Acme Inc", "Acme, Inc.", "Beta GmbH")]
    out = pt.companies(rows)
    assert [(c["company"], c["open_roles"]) for c in out] == [("Acme Inc", 3), ("Beta GmbH", 1)]


# ------------------------------------------------------------------ Phase 232
def test_agencies_stay_out_of_company_lists():
    for name in ("Hays Recruitment", "Apex Staffing LLC", "TechHeadhunters", "Bright Talent Solutions"):
        assert pq.is_agency(name), name
    for name in ("Recurly", "Talentful Labs", "Acme"):
        assert not pq.is_agency(name), name
    rows = [{"company": n, "title": "Engineer", "stack": ["rust"], "posted_at": "2026-09-30"}
            for n in ("Apex Staffing", "Acme")]
    assert [c["company"] for c in pt.companies(rows)] == ["Acme"]


# ------------------------------------------------------------------ Phases 233-234
def test_readme_names_the_boards_and_how_current_it_is(kit, state, config, clock, transport):
    unique_links(transport)
    postings(state, clock, 30, ["rust"])
    state._exec("UPDATE leads SET data = json_set(data, '$.source', 'remotive') WHERE id % 3 = 0")
    clock.advance(days=8)
    postings(state, clock, 10, ["rust"], prefix="new")
    made = pf.tick(kit, force=True)["made"]
    asset = state.get_asset(made["asset_id"])
    readme = zipfile.ZipFile(io.BytesIO(kit.files.read_bytes(asset["path"]))).read(f"{made['slug']}/README.md").decode()
    assert "Job boards: Remote OK" in readme and "Remotive" in readme
    assert f"10 of {made['rows']} postings were seen on their job board in the last 7 days." in readme
    assert pq.sources_line([]) == "" and pq.listed_line([{}], clock()) == ""
