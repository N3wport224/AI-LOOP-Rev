"""Phases 255-259: visa sponsorship, contract, junior-friendly, work from anywhere, four-day week."""

import pytest

from strategies import kinds_attributes as ka
from strategies import product_factory as pf
from strategies import product_types as pt
from strategies.b2b_lead_aggregator import POOL_NICHE
from tests import test_business_ops
from tests.test_kinds_roles import tagged
from tests.test_product_factory import unique_links

kit = test_business_ops.kit  # the same live-mode toolkit fixture


@pytest.fixture(autouse=True)
def every_kind(config):
    config.factory_types = []


def attrs(**lead):
    return ka.attributes_of(lead)


# ------------------------------------------------------------------ Phase 255
def test_reading_the_posting():
    assert attrs(title="Backend Engineer", description="We offer visa sponsorship and a relocation package.") == {"visa"}
    assert attrs(title="Backend Engineer", description="Unfortunately we are unable to sponsor visas.") == set()
    assert attrs(title="Backend Engineer", description="No relocation; visa sponsorship is not available") == set()
    assert attrs(title="Senior Rust Contractor (6 months)") == {"contract"}
    assert attrs(title="Engineer", description="Our contract with customers is great.") == set()  # not a contract role
    assert attrs(title="Graduate Software Engineer") == {"junior"} and attrs(title="Engineer", seniority="junior") == {"junior"}
    assert attrs(title="Engineer", location="Remote (Anywhere)", remote=True) == {"anywhere"}
    assert attrs(title="Engineer", location="Anywhere in Berlin office") == set()  # not remote
    assert attrs(title="Engineer", description="We work a 4-day week, 32-hour week.") == {"four_day"}


def add(state, clock, n, title, description="", location="Berlin, Germany", prefix="a", companies=10, stack=("python",)):
    from datetime import timedelta

    when = (clock() - timedelta(days=3)).isoformat(timespec="seconds")
    for i in range(n):
        state.upsert_lead(f"{prefix}-{i}", POOL_NICHE, {
            "company": f"{prefix.title()} Co {i % companies}", "title": title, "location": location,
            "remote": "remote" in location.lower(), "stack": list(stack), "seniority": "mid", "description": description,
            "url": f"https://jobs.example/{prefix}/{i}", "posted_at": when, "source": "remoteok"})


# ------------------------------------------------------------------ Phases 256-259
def test_attribute_datasets(state, config, clock):
    add(state, clock, 25, "Rust Engineer", "Visa sponsorship available.", prefix="v", stack=("rust",))
    add(state, clock, 22, "Freelance Python Developer", prefix="c")
    add(state, clock, 12, "Engineer", "We have a four-day week.", prefix="f", companies=6)
    add(state, clock, 5, "Junior Engineer", prefix="j")  # too few
    cands = {c["slug"]: c for c in ka.attribute_candidates(tagged(state), config, state)}
    assert {"visa-sponsorship", "visa-sponsorship-rust", "contract-roles", "contract-roles-python", "four-day-week"} == set(cands)
    assert cands["visa-sponsorship-rust"]["title"] == "Rust Jobs With Visa Sponsorship or Relocation"
    assert cands["four-day-week"]["companies"] == 6 and "four-day-week-python" not in cands


def test_an_attribute_product_is_made(kit, state, config, clock, transport):
    import io
    import zipfile

    unique_links(transport)
    add(state, clock, 25, "Junior Rust Engineer", prefix="j", stack=("rust",))
    state.set(pt.TURN, pt.TYPES.index("attribute"))
    made = pf.tick(kit, force=True)["made"]
    assert made["slug"] in ("junior-friendly-roles", "junior-friendly-roles-rust")
    readme = zipfile.ZipFile(io.BytesIO(kit.files.read_bytes(state.get_asset(made["asset_id"])["path"]))).read(
        f"{made['slug']}/README.md").decode()
    assert "junior, graduate and entry-level roles" in readme
