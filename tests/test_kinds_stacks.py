"""Phases 260-264: co-occurrence, stack maps, technology pairs, pairs on the trends page, "often used with"."""

import io
import json
import zipfile

import pytest

from strategies import kinds_stacks as ks
from strategies import market_trends as mt
from strategies import product_factory as pf
from strategies import product_types as pt
from tests import test_business_ops
from tests.test_kinds_roles import tagged
from tests.test_product_factory import postings, unique_links

kit = test_business_ops.kit  # the same live-mode toolkit fixture


@pytest.fixture(autouse=True)
def every_kind(config):
    config.factory_types = []


def test_cooccurrence():
    shares = ks.cooccurrence([{"rust", "aws"}, {"rust", "aws", "postgres"}, {"rust"}, {"python"}])
    assert shares["rust"] == {"aws": 0.67, "postgres": 0.33} and shares["python"] == {}


def test_stack_map_product(kit, state, config, clock, transport):
    unique_links(transport)
    postings(state, clock, 24, ["rust", "aws"])
    postings(state, clock, 6, ["rust", "postgres"], prefix="pg")
    cands = {c["slug"]: c for c in ks.stack_candidates(tagged(state), config, state)}
    assert "stack-map-rust" in cands and cands["stack-map-rust"]["title"] == "Rust Stack Map: What Hiring Companies Use"
    state.set(pt.TURN, pt.TYPES.index("stack_map"))
    made = pf.tick(kit, force=True)["made"]
    zf = zipfile.ZipFile(io.BytesIO(kit.files.read_bytes(state.get_asset(made["asset_id"])["path"])))
    slug = made["slug"]
    assert slug in ("stack-map-rust", "stack-map-aws")
    other = "AWS: 80%" if slug == "stack-map-rust" else "Rust: 100%"
    assert f"- {other}" in zf.read(f"{slug}/STACK.md").decode()
    assert json.loads(zf.read(f"{slug}/stack.json"))["postings"] in (30, 24)
    assert made["price_cents"] == ks.STACK_PRICE


def test_pairs(state, config, clock):
    postings(state, clock, 22, ["rust", "aws"])
    postings(state, clock, 5, ["rust", "kafka"], prefix="k")
    cands = {c["slug"]: c for c in ks.pair_candidates(tagged(state), config, state)}
    assert list(cands) == ["pair-aws-rust"] and cands["pair-aws-rust"]["title"] == "Companies Using AWS and Rust Together"
    page = mt.trends_page(state, lambda t, b, d: b)
    assert "<h2>Often used together</h2>" in page and "<li>AWS + Rust: 22 postings</li>" in page


def test_often_used_with_in_the_readme(kit, state, config, clock, transport):
    unique_links(transport)
    postings(state, clock, 30, ["rust", "aws"])
    config.factory_types = ["slice"]
    made = pf.tick(kit, force=True)["made"]
    readme = zipfile.ZipFile(io.BytesIO(kit.files.read_bytes(state.get_asset(made["asset_id"])["path"]))).read(
        f"{made['slug']}/README.md").decode()
    assert "Also mentioned in these postings: " in readme and "(100%)" in readme
