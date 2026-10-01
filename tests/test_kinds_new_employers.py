"""Phases 265-269: who's new, new-employer lists, the page, the feed, Monday's line."""

import xml.etree.ElementTree as ET

import pytest

from strategies import kinds_new_employers as kn
from strategies import product_factory as pf
from tests.test_kinds_roles import tagged
from tests.test_product_factory import postings


@pytest.fixture(autouse=True)
def every_kind(config):
    config.factory_types = []


def history(state, clock):
    postings(state, clock, 24, ["rust"], prefix="old", days_ago=70)  # established companies: 2+ months of data


def test_nothing_is_new_while_the_data_is_young(state, clock):
    postings(state, clock, 30, ["rust"], prefix="fresh", days_ago=3)
    assert kn.new_employers(state.leads_for_niche("__all__"), clock()) == {}


def test_new_employers_and_their_product(state, config, clock):
    history(state, clock)
    postings(state, clock, 20, ["rust"], prefix="newco", days_ago=4)   # 12 new companies
    postings(state, clock, 3, ["rust"], prefix="staffing", days_ago=4)  # "Staffing 0..2": agencies, left out
    fresh = kn.new_employers(state.leads_for_niche("__all__"), clock())
    assert len(fresh) == 12 and all(e["name"].startswith("Newco") for e in fresh.values())
    cands = {c["slug"]: c for c in kn.candidates(tagged(state), config, state)}
    assert {"new-employers", "new-employers-rust"} <= set(cands) and cands["new-employers"]["companies"] == 12


def test_page_feed_and_weekly_line(state, config, clock):
    history(state, clock)
    postings(state, clock, 12, ["rust"], prefix="newco", days_ago=2)
    files = kn.site_files(state, config, lambda t, b, d: b)
    assert "12 companies posted their first engineering role" in files[kn.PAGE] and "<b>Newco 0</b>" in files[kn.PAGE]
    root = ET.fromstring(files[kn.FEED].split("\n", 1)[1])
    assert len(root.findall("./channel/item")) == 12
    assert kn.weekly_line(state).startswith("12 companies started hiring this week, e.g. Newco")
    assert pf.fresh_leads(state, 90)  # unaffected
