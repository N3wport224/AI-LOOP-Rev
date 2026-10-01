"""Phases 250-254: the country of a posting, country datasets, technology by country, where hiring is, country column."""

import csv
import io
import zipfile

import pytest

from strategies import kinds_countries as kc
from strategies import market_trends as mt
from strategies import product_factory as pf
from strategies import product_types as pt
from tests import test_business_ops
from tests.test_kinds_roles import tagged, titled
from tests.test_product_factory import postings, unique_links

kit = test_business_ops.kit  # the same live-mode toolkit fixture


@pytest.fixture(autouse=True)
def every_kind(config):
    config.factory_types = []


# ------------------------------------------------------------------ Phase 250
@pytest.mark.parametrize("location,codes", [
    ("Berlin, Germany", {"de"}), ("Remote", set()), ("Remote, Germany", {"de"}), ("Austin, TX", {"us"}),
    ("Indianapolis, Indiana", set()), ("London, UK", {"gb"}), ("Toronto or Remote (US)", {"ca", "us"}),
    ("München", {"de"}), ("Bengaluru, India", {"in"}), ("Plus Amsterdam", {"nl"}), ("Customer success", set()),
])
def test_country_of_a_posting(location, codes):
    assert kc.country_of({"location": location}) == codes


# ------------------------------------------------------------------ Phases 251-252
def test_country_and_technology_by_country_datasets(state, config, clock):
    postings(state, clock, 30, ["rust"])                                    # Berlin
    titled(state, clock, 10, "Backend Engineer", location="Hamburg, Germany", prefix="h", stack=("golang",))
    cands = {c["slug"]: c for c in kc.country_candidates(tagged(state), config, state)}
    assert cands["country-de"]["title"] == "Companies Hiring Software Engineers in Germany" and len(cands["country-de"]["rows"]) == 40
    assert cands["country-de-rust"]["title"] == "Rust Jobs in Germany: Companies Hiring"
    assert "country-de-golang" not in cands  # 10 postings: below the floor
    assert cands["country-de"]["filters"]["group"] == "Germany"


def test_a_country_product_has_a_country_column(kit, state, config, clock, transport):
    unique_links(transport)
    postings(state, clock, 30, ["rust"])
    state.set(pt.TURN, pt.TYPES.index("country"))
    made = pf.tick(kit, force=True)["made"]
    assert made["slug"] == "country-de"
    asset = state.get_asset(made["asset_id"])
    zf = zipfile.ZipFile(io.BytesIO(kit.files.read_bytes(asset["path"])))
    rows = list(csv.DictReader(io.StringIO(zf.read("country-de/leads.csv").decode())))
    assert rows[0]["country"] == "Germany"  # Phase 254
    assert "filtered to: roles located in Germany" in zf.read("country-de/README.md").decode()


# ------------------------------------------------------------------ Phase 253
def test_where_companies_are_hiring(state, clock):
    postings(state, clock, 6, ["rust"])
    titled(state, clock, 4, "Engineer", location="Paris, France", prefix="p")
    titled(state, clock, 9, "Engineer", location="Lyon, France", prefix="l", days_ago=40)  # too old
    assert [tuple(c) for c in mt.trends(state, force=True)["countries"]] == [("Germany", 6), ("France", 4)]
    page = mt.trends_page(state, lambda t, b, d: b)
    assert "<h2>Where companies are hiring</h2>" in page and "<li>Germany: 6</li>" in page
