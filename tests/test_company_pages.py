"""Phases 380-384: company pages."""

import json
import re

from strategies import company_pages as cp
from strategies import opt_out as oo
from strategies.inbound_syndicator import InboundSyndicator
from strategies.posting_quality import company_key
from tests import test_business_ops
from tests.test_business_ops import ctx
from tests.test_catalog_hygiene import made_product
from tests.test_kinds_attributes import add
from tests.test_product_factory import postings

kit = test_business_ops.kit  # the same live-mode toolkit fixture
SHELL_AT = lambda title, body, desc, depth: body  # noqa: E731


def test_which_companies(state, config, clock):
    postings(state, clock, 24, ["rust"])                                  # 12 companies, 2 roles each: below the floor
    add(state, clock, 6, "Rust Engineer", prefix="acme", companies=1)      # "Acme Co 0": 6 roles
    add(state, clock, 5, "Recruiter", prefix="hays staffing", companies=1)  # an agency
    names = [c["name"] for c in cp.companies(state, config)]
    assert names == ["Acme Co 0"]
    state.set(oo.EXCLUDED, [company_key("Acme Co 0")])  # an approved opt-out
    assert cp.companies(state, config) == []


def test_page_content(state, config, clock):
    add(state, clock, 6, "Senior Rust Engineer", prefix="acme", companies=1, stack=("rust", "aws"))
    files = cp.site_files(state, config, [], SHELL_AT)
    page = files["company/acme-co-0/index.html"]
    assert "<h1>Acme Co 0 is hiring</h1>" in page and "6 open engineering roles" in page and "AWS, Rust" in page
    assert page.count('rel="nofollow noopener"') == 6 and 'href="../../remove/"' in page
    ld = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', page, re.S).group(1))
    assert ld == {"@context": "https://schema.org", "@type": "Organization", "name": "Acme Co 0"}
    assert 'href="acme-co-0/"' in files["company/index.html"]


def test_on_the_site(kit, state, config, clock, transport):
    config.pages_base_url = "https://me.github.io/d"
    made_product(kit, state, clock, transport)
    add(state, clock, 6, "Rust Engineer", prefix="acme", companies=1, stack=("rust",))
    InboundSyndicator().run("build_site", ctx(kit))
    page = kit.files.read_text("site/company/acme-co-0/index.html")
    assert 'href="../../hiring-rust/"' in page
    assert "/d/company/acme-co-0/" in kit.files.read_text("site/sitemap.xml")
    assert '<a href="company/">Companies hiring</a>' in kit.files.read_text("site/index.html")
    assert state.get("site_audit")["broken_links"] == []
    assert company_key("Acme Co 0") == "acme co 0"
