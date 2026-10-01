"""Phases 220-224: weekly counts, rising and falling, trends page, factory follows demand, fastest-hiring."""

import io
import json
import zipfile

from strategies import market_trends as mt
from strategies import product_factory as pf
from strategies import product_types as pt
from tests import test_business_ops
from tests.test_product_factory import postings, unique_links

kit = test_business_ops.kit  # the same live-mode toolkit fixture


def test_weekly_counts_and_classification(state, clock):
    postings(state, clock, 6, ["rust"], prefix="old", days_ago=20)    # 2 weeks before last: week index 5
    postings(state, clock, 14, ["rust"], prefix="new", days_ago=3)    # this week
    postings(state, clock, 20, ["php"], prefix="php", days_ago=17)
    postings(state, clock, 5, ["php"], prefix="php2", days_ago=2)
    postings(state, clock, 40, ["java"], prefix="j", days_ago=100)    # too old to count
    t = mt.trends(state)
    assert t["counts"]["rust"][-1] == 14 and t["counts"]["rust"][-3] == 6 and sum(t["counts"]["rust"]) == 20
    assert "java" not in t["counts"]
    assert [r["tech"] for r in t["rising"]] == ["rust"] and t["rising"][0]["growth"] == 1.33
    assert [r["tech"] for r in t["falling"]] == ["php"]
    postings(state, clock, 50, ["php"], prefix="later", days_ago=1)
    assert mt.trends(state)["falling"]  # cached for CACHE_HOURS
    clock.advance(hours=mt.CACHE_HOURS)
    assert not mt.trends(state)["falling"]


def test_trends_page(state, clock):
    assert mt.trends_page(state, lambda t, b, d: b) == ""  # no data: no page
    postings(state, clock, 6, ["rust"], prefix="old", days_ago=20)
    postings(state, clock, 14, ["rust"], prefix="new", days_ago=3)
    page = mt.trends_page(state, lambda t, b, d: b)
    assert "<b>Rust</b>: +133% (6 → 14 new postings)" in page and "<th>This week</th>" in page


def test_rising_technologies_are_made_first(state, config, clock):
    cands = [{"slug": "a", "filters": {"tech": "php"}}, {"slug": "b", "filters": {"tech": "rust"}}, {"slug": "c", "filters": {}}]
    assert [c["slug"] for c in mt.favour_rising(cands, {"rust"})] == ["b", "a", "c"]
    assert mt.favour_rising(cands, set()) is cands
    config.factory_min_rows, config.factory_min_companies = 20, 8
    postings(state, clock, 25, ["php"], prefix="p", days_ago=17)
    postings(state, clock, 26, ["php"], prefix="p2", days_ago=3)  # steady, and more of it
    postings(state, clock, 6, ["rust"], prefix="r", days_ago=17)
    postings(state, clock, 20, ["rust"], prefix="r2", days_ago=3)  # rising
    slices = pt.all_candidates(state, config)["slice"]
    assert slices[0]["filters"]["tech"] == "rust"


def test_fastest_hiring_companies_product(kit, state, config, clock, transport):
    unique_links(transport)
    postings(state, clock, 36, ["rust"], prefix="f", days_ago=3)  # 12 companies, 3 roles each, this fortnight
    postings(state, clock, 36, ["rust"], prefix="o", days_ago=30)  # older roles don't count
    tagged = [(lead, pf.facets(lead)) for lead in pf.fresh_leads(state, 60)]
    cands = mt.fast_hiring_candidates(tagged, clock())
    assert [c["slug"] for c in cands] == ["fast-hiring-rust"] and cands[0]["companies"] == 12
    assert all(r["open_roles"] == 3 for r in cands[0]["rows"])
    state.set(pt.TURN, pt.TYPES.index("fast_hiring"))
    made = pf.tick(kit, force=True)["made"]
    assert made["slug"] == "fast-hiring-rust" and made["price_cents"] == mt.FAST_PRICE
    asset = state.get_asset(made["asset_id"])
    zf = zipfile.ZipFile(io.BytesIO(kit.files.read_bytes(asset["path"])))
    assert "3+ new roles in the last 14 days" in zf.read("fast-hiring-rust/README.md").decode()
    assert json.loads(state._one("SELECT filters FROM factory_products WHERE slug = ?", (made["slug"],))["filters"])["type"] == "fast_hiring"


def test_too_few_fast_hirers_make_nothing(state, clock):
    postings(state, clock, 14, ["rust"], prefix="f", days_ago=3)  # 12 companies, but 1-2 roles each
    tagged = [(lead, pf.facets(lead)) for lead in pf.fresh_leads(state, 60)]
    assert mt.fast_hiring_candidates(tagged, clock()) == []
