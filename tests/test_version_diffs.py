"""Phases 275-279: diff, CHANGES.md, remembered, product page line, What's new."""

import io
import zipfile

from strategies import site_discovery as sd
from strategies import version_diffs as vd
from strategies.inbound_syndicator import site_pages
from strategies.product_types import refresh_due
from tests import test_business_ops
from tests.test_catalog_hygiene import made_product
from tests.test_product_factory import postings
from tools.page_builder import render_product_page

kit = test_business_ops.kit  # the same live-mode toolkit fixture


def test_diff():
    old = [{"company": "A", "url": "u1"}, {"company": "B", "url": "u2"}]
    new = [{"company": "A", "url": "u1"}, {"company": "C", "url": "u3"}, {"company": "C", "url": "u4"}]
    assert vd.diff(old, new) == {"new_rows": 2, "gone_rows": 1, "companies_added": ["C"], "companies_gone": ["B"], "rows": 3}
    cos = vd.diff([{"company": "A", "example_url": "x"}], [{"company": "A", "example_url": "y"}, {"company": "B"}])
    assert cos["new_rows"] == 1 and cos["companies_added"] == ["B"]  # company lists match by company


def test_a_refresh_says_what_changed(kit, state, config, clock, transport):
    made = made_product(kit, state, clock, transport)
    clock.advance(days=8)
    postings(state, clock, 10, ["rust"], prefix="newco")
    assert refresh_due(kit) == [made["slug"]]
    row = state._one("SELECT asset_id FROM factory_products WHERE slug = ?", (made["slug"],))
    zf = zipfile.ZipFile(io.BytesIO(kit.files.read_bytes(state.get_asset(row["asset_id"])["path"])))
    changes = zf.read(f"{made['slug']}/CHANGES.md").decode()
    assert "- New rows: 10" in changes and "Companies added (10): Newco 0, Newco 1" in changes and "Version 2" in changes
    remembered = state.get(vd.KEY + made["slug"])
    assert remembered["new_rows"] == 10 and remembered["companies_added"] == 10 and remembered["version"] == 2
    page = next(p for p in site_pages(kit) if p.slug == made["slug"])
    assert "<b>Updated weekly.</b>" in render_product_page(page) and "+10 new postings, +10 companies" in page.files_html
    whats_new = sd.changes_page(state, lambda t, b, d: b)
    assert "Updated (1):" in whats_new and "+10 new rows, +10 companies" in whats_new


def test_first_versions_have_no_changes_file(kit, state, config, clock, transport):
    made = made_product(kit, state, clock, transport)
    zf = zipfile.ZipFile(io.BytesIO(kit.files.read_bytes(state.get_asset(made["asset_id"])["path"])))
    assert not any(n.endswith("CHANGES.md") for n in zf.namelist()) and vd.update_note(state, made["slug"]) == ""
